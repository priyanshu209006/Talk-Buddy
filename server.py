"""Dial-a-Browser server: phone audio <-> Prisma STT <-> Groq agent <-> Chromium <-> Timbre TTS."""
import asyncio
import json
import logging
import os
import re
import time
import wave
from pathlib import Path

from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import duplex  # noqa: E402
import gnani  # noqa: E402
import gallery  # noqa: E402
import portal  # noqa: E402
from agent import ACKS, Agent  # noqa: E402
import random  # noqa: E402
from browser import Browser  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

STATIC = Path(__file__).with_name("static")

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_prewarm())
    yield

app = FastAPI(title="Dial-a-Browser", lifespan=lifespan)
app.include_router(gallery.router)
app.include_router(portal.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

BYTES_PER_SEC = gnani.TTS_RATE * 2      # outgoing (Timbre) audio
# Words Prisma should favour (names/terms a general model tends to mishear).
KEYTERMS = ["PNR", "IRCTC", "NTES", "RailSeva", "sleeper", "AC", "3A", "2A", "OTP", "tatkal", "berth",
            "platform", "Rajdhani", "Shatabdi", "Vande Bharat", "Tejas", "Garib Rath", "Sampark Kranti",
            "Duronto", "Humsafar", "yojana", "pension", "vidhwa", "myScheme", "Aadhaar", "BPL",
            "Patna", "Delhi", "Chennai", "Howrah", "Mumbai", "Bengaluru", "Lucknow", "Varanasi",
            "Danapur", "Kanpur", "Prayagraj", "Bihar", "Uttar Pradesh", "mausam", "barish"]


# ---- speed: cached audio for short, repeated lines (greeting, acks, fillers) ---------------
_TTS_CACHE: dict[tuple[str, str], bytes] = {}


async def cached_tts(text: str, voice_kind: str, voice_name: str):
    """Yield PCM chunks; short lines are synthesized once and then replayed instantly."""
    key = (text, voice_name)
    if key in _TTS_CACHE:
        pcm = _TTS_CACHE[key]
        step = gnani.TTS_RATE * 2 // 5          # 200 ms chunks
        for i in range(0, len(pcm), step):
            yield pcm[i:i + step]
        return
    buf = bytearray()
    async for chunk in gnani.synthesize(text, voice_kind, voice_name=voice_name):
        buf.extend(chunk)
        yield chunk
    if len(text) <= 140:
        _TTS_CACHE[key] = bytes(buf)


async def _prewarm():
    """At startup: launch the shared browser and pre-synthesize the fixed lines for both voices."""
    import agent as agent_mod
    from browser import shared_browser
    try:
        await shared_browser()
        log.info("browser warm")
    except Exception as e:
        log.warning("browser prewarm failed: %s", e)
    lines = [agent_mod.GREETING, *agent_mod.ACKS, "बस एक सेकंड, देख रही हूँ…",
             "माफ़ कीजिए, एक सेकंड दिक्कत आ गई। फिर से बोलिए?"]
    for g, vname in gnani.GUIDE_BY_GENDER.items():
        for ln in lines:
            text = gnani.speakable(gnani.fix_self_gender(ln, g))
            try:
                async for _ in cached_tts(text, "guide", vname):
                    pass
            except Exception as e:
                log.warning("tts prewarm failed: %s", e)
                return
    log.info("tts cache warm: %d lines", len(_TTS_CACHE))


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return RedirectResponse("/phone")


@app.api_route("/healthz", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return Response(content="OK", media_type="text/plain", status_code=200)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon", status_code=204)


@app.api_route("/config", methods=["GET", "HEAD"])
async def config():
    return {"stt_rate": gnani.STT_RATE, "tts_rate": gnani.TTS_RATE, "phone_mode": gnani.PHONE_MODE}


@app.api_route("/phone", methods=["GET", "HEAD"])
@app.api_route("/phone/", methods=["GET", "HEAD"])
async def phone():
    return FileResponse(STATIC / "phone.html", headers={"Cache-Control": "no-store"})


class Call:
    """One phone call: owns the STT stream, the agent, the browser and the speech queue."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.browser = Browser()
        self.agent = Agent(self)
        self.stt = gnani.PrismaStream(self.on_stt, keyterms=KEYTERMS)
        self.speech_q: asyncio.Queue = asyncio.Queue()
        self.current = None              # (text, voice) being spoken
        self.speaking_until = 0.0        # wall-clock time when queued audio finishes playing
        self.speak_started = 0.0
        self.notes: list[str] = []
        self.closed = False
        self.barge_in = True             # duplex on: caller can talk over the assistant
        self._user_talking = False
        self._user_talking_until = 0.0
        self._verified: dict[int, float | None] = {}
        self.recent_said: list[str] = []  # last few things the assistant said
        self.ducked = False
        self._unduck_at = 0.0
        self.guide_voice = gnani.VOICES["guide"]["voice"]   # can be switched by the caller
        self.recording = bytearray()     # caller audio, saved to logs/ for STT debugging
        self.transcripts: list[dict] = []
        self._send_lock = asyncio.Lock()
        self._speaker_task = None
        self._otp_listener = None

    # ---- UI / transport ---------------------------------------------------
    async def ui(self, event: dict):
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:
                pass

    async def audio_out(self, pcm: bytes):
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_bytes(pcm)
            except Exception:
                pass

    async def push_screen(self):
        if self.closed:
            return
        try:
            img = await self.browser.screenshot_b64()
            if img:
                raw_url = self.browser.page.url if (self.browser and self.browser.page) else ""
                url = "Ready: Dial-a-Browser" if (not raw_url or raw_url == "about:blank") else raw_url
                await self.ui({"type": "screen", "url": url, "jpeg": img})
        except Exception as e:
            log.warning("push_screen failed: %s", e)

    def pop_notes(self):
        n, self.notes = self.notes, []
        return n

    # ---- speech out ---------------------------------------------------------
    async def speak(self, text: str, voice: str = "guide", verified: float | None = None):
        """Queue speech and wait until it has been sent (not necessarily heard)."""
        text = (text or "").strip()
        if not text:
            return
        done = asyncio.get_running_loop().create_future()
        self._verified[id(done)] = verified
        await self.speech_q.put((text, voice, done))
        try:
            await done
        except asyncio.CancelledError:
            pass

    @property
    def is_speaking(self) -> bool:
        return self.current is not None or time.monotonic() < self.speaking_until

    @property
    def user_talking(self) -> bool:
        if self._user_talking and time.monotonic() > self._user_talking_until:
            self._user_talking = False
        return self._user_talking

    @user_talking.setter
    def user_talking(self, val: bool):
        self._user_talking = val
        self._user_talking_until = time.monotonic() + 2.5 if val else 0.0

    async def _speaker(self):
        while not self.closed:
            text, voice, done = await self.speech_q.get()
            if voice == "guide":
                # The assistant's own Hindi grammar must match the voice it speaks with:
                # a female voice says "देख रही हूँ", a male voice "देख रहा हूँ".
                text = gnani.fix_self_gender(text, gnani.voice_gender(self.guide_voice))
            self.current = (text, voice)
            plain = gnani.strip_tags(text)
            self.recent_said = (self.recent_said + [plain])[-3:]
            await self.ui({"type": "say", "voice": voice, "text": plain,
                           "verified": self._verified.pop(id(done), None)})
            try:
                spoken = gnani.speakable(text)
                start = max(time.monotonic(), self.speaking_until)
                if self.speaking_until < time.monotonic():
                    self.speak_started = time.monotonic()
                sent = 0
                vname = self.guide_voice if voice == "guide" else gnani.VOICES[voice]["voice"]
                async for chunk in cached_tts(spoken, voice, vname):
                    if self.current is None:       # barged in
                        break
                    await self.audio_out(chunk)
                    sent += len(chunk)
                    self.speaking_until = start + sent / BYTES_PER_SEC
            except Exception as e:
                log.warning("TTS failed: %s", e)
                await self.ui({"type": "log", "text": f"TTS error: {e}"})
            finally:
                self.current = None
                if not done.done():
                    done.set_result(True)

    def _duck(self, on: bool):
        """Lower (not stop) the assistant's voice while we find out who the caller is talking to."""
        self._unduck_at = time.monotonic() + 6 if on else 0.0
        if self.ducked != on:
            self.ducked = on
            asyncio.create_task(self.ui({"type": "duck", "on": on}))

    async def _unduck_watchdog(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.ducked and self._unduck_at and time.monotonic() > self._unduck_at:
                self._duck(False)       # speech_start with no transcript (cough, noise)

    def _barge_in(self):
        """Caller started talking over us: stop audio, drop queued speech, tell the agent."""
        self.ducked = False
        interrupted = gnani.strip_tags(self.current[0]) if self.current else None
        self.current = None
        self.speaking_until = 0.0
        while not self.speech_q.empty():
            _, _, done = self.speech_q.get_nowait()
            if not done.done():
                done.set_result(True)
        asyncio.create_task(self.ui({"type": "clear_audio"}))
        if interrupted:
            self.notes.append(f'The caller interrupted you while you were saying: "{interrupted}".')

    # ---- speech in ----------------------------------------------------------
    async def on_stt(self, msg: dict):
        t = msg.get("type")
        if t == "speech_start":
            self.user_talking = True
            await self.ui({"type": "listening"})
            # Duplex: don't cut ourselves off yet, just get quieter until we know
            # whether the caller is talking to us.
            if self.barge_in and self.is_speaking and time.monotonic() - self.speak_started > 0.4:
                self._duck(True)
        elif t == "transcript":
            self.user_talking = False
            text = (msg.get("text") or "").strip()
            if not text:
                self._duck(False)
                return
            lang = msg.get("detected_language_code")
            speaking = self.is_speaking

            # Fast backchannel check: if assistant is speaking and caller just acknowledges ("हम्म/ok"), don't barge in.
            # Defensive: a bug here must never drop the transcript.
            try:
                words = [w.lower() for w in re.findall(r"[\wऀ-෿]+", text)]
            except Exception:
                words = []
            if speaking and words and all(w in duplex._BACKCHANNEL for w in words):
                self._duck(False)
                log.info("backchannel while speaking ignored: %r", text)
                await self.ui({"type": "heard", "text": text, "lang": lang, "latency": msg.get("latency"),
                               "ignored": "backchannel", "why": "acknowledgement while assistant speaking"})
                return

            if speaking and self.barge_in:
                self._barge_in()
            else:
                self._duck(False)

            log.info("heard %r lang=%s (latency=%sms)", text, lang, msg.get("latency"))
            self.transcripts.append({"t": round(time.monotonic(), 2), "text": text, "lang": lang,
                                     "verdict": "RELEVANT", "why": "direct", "latency": msg.get("latency")})
            await self.ui({"type": "heard", "text": text, "lang": lang, "latency": msg.get("latency"),
                           "ignored": None, "why": ""})

            # Instant acknowledgement for a multi-word request:
            # cached audio, plays within ~100 ms while the agent thinks and browses.
            if len(text.split()) >= 3:
                is_en = bool(lang and lang.startswith("en"))
                ack = random.choice(["Sure, one second…", "Checking now…", "Looking that up…"]) if is_en else random.choice(["जी, एक सेकंड…", "अच्छा, अभी देखता हूँ…", "देख रहा हूँ…"])
                asyncio.create_task(self.speak(ack, "guide"))
            self.agent.hear(f"{text}" + (f"  [detected language: {lang}]" if lang else ""))
        elif t == "error":
            await self.ui({"type": "log", "text": f"STT error: {msg.get('message')}"})
        elif t == "stt_closed" and not self.closed:
            await self.ui({"type": "log", "text": "STT stream closed; reconnecting"})
            try:
                await self.stt.connect()
            except Exception as e:
                await self.ui({"type": "log", "text": f"STT reconnect failed: {e}"})

    # ---- lifecycle ----------------------------------------------------------
    async def hangup(self, after_speech: bool = False):
        if after_speech:
            while not self.speech_q.empty() or self.current:
                await asyncio.sleep(0.1)
            remaining = self.speaking_until - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining + 0.3)
        await self.ui({"type": "hangup"})

    def _save_recording(self):
        """logs/call-<time>.wav + .json: what the mic sent and what Prisma heard, for tuning STT."""
        if not self.recording and not self.transcripts:
            return
        out = Path(__file__).with_name("logs")
        out.mkdir(exist_ok=True)
        stem = out / time.strftime("call-%Y%m%d-%H%M%S")
        if self.recording:
            with wave.open(str(stem) + ".wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(gnani.STT_RATE)
                w.writeframes(bytes(self.recording))
        stem.with_suffix(".json").write_text(json.dumps(self.transcripts, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
        log.info("saved %s.wav (%.0fs) and transcripts", stem, len(self.recording) / (gnani.STT_RATE * 2))

    async def run(self):
        await self.ui({"type": "status", "text": "Connecting…"})
        await self.ui({"type": "config", "stt_rate": gnani.STT_RATE, "tts_rate": gnani.TTS_RATE})
        await self.browser.start()
        await self.browser.init_page()
        await self.push_screen()
        await self.stt.connect()
        loop = asyncio.get_running_loop()

        def on_otp(mobile, otp, sms):
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.ui({"type": "sms", "text": sms})))
        self._otp_listener = on_otp
        portal.OTP_LISTENERS.append(on_otp)

        self._speaker_task = asyncio.create_task(self._speaker())
        unduck_task = asyncio.create_task(self._unduck_watchdog())
        agent_task = asyncio.create_task(self.agent.run())
        await self.ui({"type": "status", "text": "Connected"})
        try:
            while True:
                m = await self.ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes"):
                    if os.environ.get("SAVE_RECORDINGS") == "1" and len(self.recording) < gnani.STT_RATE * 2 * 1800:
                        self.recording.extend(m["bytes"])
                    await self.stt.send_pcm(m["bytes"])
                elif m.get("text"):
                    try:
                        ctl = json.loads(m["text"])
                    except json.JSONDecodeError:
                        continue
                    if ctl.get("type") == "ping":
                        await self.ui({"type": "pong"})
                    elif ctl.get("type") == "barge_in":
                        self.barge_in = bool(ctl.get("enabled"))
                    elif ctl.get("type") == "typed":      # debug: type instead of speaking
                        await self.on_stt({"type": "transcript", "text": ctl.get("text", "")})
        except WebSocketDisconnect:
            pass
        finally:
            self.closed = True
            agent_task.cancel()
            unduck_task.cancel()
            self._speaker_task.cancel()
            if self._otp_listener in portal.OTP_LISTENERS:
                portal.OTP_LISTENERS.remove(self._otp_listener)
            # Free the Chromium context immediately so quick reconnects never overlap on 512 MB RAM.
            try:
                await self.browser.close()
            except Exception:
                pass
            if os.environ.get("SAVE_RECORDINGS") == "1":
                try:
                    await self.stt.flush()
                except Exception:
                    pass
                try:
                    self._save_recording()
                except Exception:
                    log.exception("could not save recording")
            try:
                await self.stt.close()
            except Exception:
                pass
            log.info("call ended")


@app.websocket("/ws/call")
async def call_ws(ws: WebSocket):
    await ws.accept()
    load_dotenv(Path(__file__).with_name(".env"), override=True)   # pick up rotated keys without a restart
    missing = [k for k in ("GNANI_API_KEY",) if not os.environ.get(k)]
    if not (os.environ.get("NEBIUS_API_KEY") or os.environ.get("GROQ_API_KEY")):
        missing.append("NEBIUS_API_KEY or GROQ_API_KEY")
    if missing:
        await ws.send_text(json.dumps({"type": "log", "text": f"Missing {', '.join(missing)} in .env"}))
        await ws.close()
        return
    call = Call(ws)
    try:
        await call.run()
    except Exception as e:
        log.exception("call failed")
        try:
            await ws.send_text(json.dumps({"type": "log", "text": f"Call failed: {e}"}))
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    # Local: 127.0.0.1. In Docker/Render: HOST=0.0.0.0 and Render's $PORT.
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        ws_ping_interval=25.0,
        ws_ping_timeout=90.0,
        timeout_keep_alive=75,
    )
