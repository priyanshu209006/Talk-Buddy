"""Gnani Prisma (streaming STT) and Timbre (streaming TTS) clients."""
import asyncio
import base64
import json
import logging
import os
import re

import websockets

log = logging.getLogger("gnani")

STT_URL = "wss://api.vachana.ai/stt/v3/stream"
TTS_URL = "wss://api.vachana.ai/api/v1/tts"

# PHONE_MODE=1 simulates a real narrowband phone line (8 kHz both ways). Default is the
# highest-quality wideband call: 16 kHz native wideband into Prisma (optimal accuracy without
# 3x WebSocket frame overhead on 0.1 vCPU) and studio 48 kHz out of Timbre (matching native
# browser AudioContext playback). Override individually with STT_RATE / TTS_RATE if needed.
PHONE_MODE = os.environ.get("PHONE_MODE", "0") == "1"
STT_RATE = int(os.environ.get("STT_RATE", "8000" if PHONE_MODE else "16000"))
TTS_RATE = int(os.environ.get("TTS_RATE", "8000" if PHONE_MODE else "48000"))
SAMPLE_RATE = STT_RATE      # back-compat name
FRAME_BYTES = 1024          # Prisma expects 1024-byte PCM frames

# Language Prisma should listen for first. The streaming endpoint takes exactly ONE locale
# (or "indic-auto"); a comma list is rejected. It's a hint, not a lock: clearly Tamil/English
# speech is still transcribed as such. A known language helps short clips most.
STT_LANGS = os.environ.get("STT_LANGS", "indic-auto")

TTS_MODEL = "timbre-v3.0"   # every v3.0 voice speaks all 10 languages

# Timbre v3.0 voice catalogue genders (from Gnani's voice library).
VOICE_GENDER = {
    "Avinash": "m", "Devansh": "m", "Prakash": "m", "Shubham": "m", "Yashraj": "m",
    "Chetana": "f", "Kalpana": "f", "Lakshmi": "f", "Riyaa": "f", "Shivani": "f",
    "Tarika": "f", "Vasudha": "f",
}
# Warm, conversational guide voice for each gender, if the caller asks to switch.
GUIDE_BY_GENDER = {"f": "Shivani", "m": "Yashraj"}

VOICES = {
    # The agent itself: warm, conversational.
    "guide": {"voice": os.environ.get("GUIDE_VOICE", "Shivani"), "speed": 1.0},
    # The website's own words, read verbatim: steady, clear.
    "site": {"voice": os.environ.get("SITE_VOICE", "Avinash"), "speed": 0.97},
}


def _key() -> str:
    key = os.environ.get("GNANI_API_KEY")
    if not key:
        raise RuntimeError("GNANI_API_KEY is not set")
    return key


class PrismaStream:
    """One streaming STT session. Push 8 kHz 16-bit PCM in, get events out."""

    def __init__(self, on_event, keyterms=None):
        self.on_event = on_event          # async callback(dict)
        self.keyterms = keyterms or []
        self.ws = None
        self._buf = bytearray()
        self._reader = None
        self._dispatcher = None
        # Events go on a queue; a separate task runs on_event. This keeps _read draining the
        # Prisma socket at all times, so a slow handler never stalls reading and makes Prisma
        # back up (which dropped trailing utterances).
        self._events: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def connect(self):
        headers = {
            "x-api-key-id": _key(),
            "model": "prisma-v3.0",
            "language_code": STT_LANGS,
            # "transcribe": numbers come back as digits ("12309", not "बारह तीन शून्य नौ"),
            # which train numbers, PNRs, OTPs and ages all need. Tested against codemix.
            "format": os.environ.get("STT_FORMAT", "transcribe"),
            "x-sample-rate": str(STT_RATE),
            "sample_rate": str(STT_RATE),
            # Gnani's guidance: clean mic 0.45-0.55, telephony 0.65-0.75; conversation
            # silence 600-900 ms so one sentence isn't split at every breath.
            "x-vad-threshold": "0.68" if PHONE_MODE else "0.5",
            "x-min-silence-ms": "650",
            "x-min-speech-ms": "150",
            # Low floor: 250ms was dropping real short answers ("हाँ खोल", "change करते हैं").
            "x-min-segment-ms": "120",
            "x-idle-timeout-sec": "600",
            "topic": ("Multilingual phone call (English, Hindi, Hinglish, regional languages) to a voice assistant "
                      "that browses websites: Wikipedia, search, live train running status, train numbers, "
                      "government schemes, weather"),
        }
        if self.keyterms:
            headers["keyterms"] = json.dumps(self.keyterms, ensure_ascii=True)
        for task in (self._reader, self._dispatcher):   # avoid duplicates on reconnect
            if task:
                task.cancel()
        self.ws = await websockets.connect(
            STT_URL,
            additional_headers=headers,
            max_size=None,
            ping_interval=25,
            ping_timeout=60,
        )
        self._reader = asyncio.create_task(self._read())
        self._dispatcher = asyncio.create_task(self._dispatch())

    async def _read(self):
        """Drain the Prisma socket as fast as possible; hand events to the dispatcher queue."""
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._events.put_nowait(msg)
        except websockets.ConnectionClosed as e:
            log.info("STT closed: %s", e)
        except Exception:
            log.exception("STT reader failed")
        finally:
            if not self._closed:
                self._events.put_nowait({"type": "stt_closed"})

    async def _dispatch(self):
        while True:
            msg = await self._events.get()
            try:
                await self.on_event(msg)
            except Exception:
                log.exception("STT event handler failed")

    async def send_pcm(self, data: bytes):
        if not self.ws or self._closed:
            return
        self._buf.extend(data)
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            try:
                await self.ws.send(frame)
            except websockets.ConnectionClosed:
                return

    async def flush(self, drain_s: float = 1.3):
        """Finalize the last utterance before closing. Prisma only emits a segment after
        trailing silence (min-silence), so on hangup we push ~1s of silence and a commit,
        then let the dispatcher deliver the final transcript. Without this the last thing
        the caller said is lost (see Gnani docs: 'end the audio with ~1s of silence')."""
        if not self.ws or self._closed:
            return
        try:
            silence = b"\x00" * FRAME_BYTES
            frames = int(drain_s * STT_RATE * 2 / FRAME_BYTES)
            for _ in range(frames):
                await self.ws.send(silence)
                await asyncio.sleep(0.01)
            try:
                await self.ws.send(json.dumps({"type": "commit"}))
            except Exception:
                pass
            await asyncio.sleep(0.4)          # give the dispatcher time to deliver it
        except Exception:
            pass

    async def close(self):
        self._closed = True
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        for task in (self._reader, self._dispatcher):
            if task:
                task.cancel()


async def synthesize(text: str, voice_kind: str = "guide", language: str = "auto",
                     voice_name: str | None = None):
    """Stream Timbre audio for `text`. Yields raw 16-bit mono PCM chunks at TTS_RATE."""
    v = VOICES.get(voice_kind, VOICES["guide"])
    req = {
        "text": text,
        "voice": voice_name or v["voice"],
        "model": TTS_MODEL,
        "language": language,
        "speed": v["speed"],
        "audio_config": {
            "sample_rate": TTS_RATE,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "raw",
        },
    }
    async with websockets.connect(
        TTS_URL,
        additional_headers={"X-API-Key-ID": _key(), "Content-Type": "application/json"},
        max_size=None,
    ) as ws:
        await ws.send(json.dumps(req, ensure_ascii=False))
        async for raw in ws:
            if isinstance(raw, bytes):     # some deployments send binary PCM directly
                yield raw
                continue
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "audio":
                audio = msg.get("data", {}).get("audio")
                if audio:
                    yield base64.b64decode(audio)
            elif t == "complete":
                return
            elif t == "error":
                raise RuntimeError(f"Timbre error: {msg.get('message')}")


# ---------------------------------------------------------------------------
# Making website text speakable the way an Indian booking clerk would say it.
# Timbre's normaliser already handles ₹ amounts (lakh/crore), dates and times.
# We only fix the identifiers it can't know about: train numbers and PNRs.
# ---------------------------------------------------------------------------

def _spaced(d: str) -> str:
    return " ".join(d)


# ---------------------------------------------------------------------------
# Gender in Hindi grammar.
# Only first-person singular takes "हूँ", so "<verb>ा हूँ" / "<verb>ी हूँ" always refers to the
# speaker. That lets us (a) make the assistant's self-reference match its voice and (b) learn the
# caller's grammatical gender from how THEY refer to themselves, never from pitch or names.
# ---------------------------------------------------------------------------
# masculine -> feminine pairs for first-person forms
_SELF_PAIRS = [
    ("रहा हूँ", "रही हूँ"), ("रहा हूं", "रही हूं"), ("सकता हूँ", "सकती हूँ"), ("सकता हूं", "सकती हूं"),
    ("चुका हूँ", "चुकी हूँ"), ("गया हूँ", "गई हूँ"), ("पाया हूँ", "पाई हूँ"),
    ("ऊँगा", "ऊँगी"), ("ूँगा", "ूँगी"), ("ऊंगा", "ऊंगी"), ("ूंगा", "ूंगी"),
]
# habitual "करता हूँ" / "देखती हूँ" (verb stem + ता/ती + हूँ)
_HABIT_M = re.compile(r"(\S+?)ता (हूँ|हूं)")
_HABIT_F = re.compile(r"(\S+?)ती (हूँ|हूं)")


def voice_gender(voice_name: str) -> str:
    return VOICE_GENDER.get(voice_name, "f")


def fix_self_gender(text: str, gender: str) -> str:
    """Make the speaker's first-person Hindi verb forms match `gender` ('f' or 'm')."""
    for m, f in _SELF_PAIRS:
        text = text.replace(m, f) if gender == "f" else text.replace(f, m)
    if gender == "f":
        text = _HABIT_M.sub(lambda x: f"{x[1]}ती {x[2]}", text)
    else:
        text = _HABIT_F.sub(lambda x: f"{x[1]}ता {x[2]}", text)
    return text


_CALLER_F = re.compile(r"(रही|सकती|चुकी|गई|ती|थी) (हूँ|हूं)|ूँगी|ूंगी|ऊँगी|ऊंगी|"
                       r"(विधवा|गर्भवती|गृहिणी) (हूँ|हूं)|साल की[^।,.?]{0,25}(हूँ|हूं)|"
                       r"मैं (विधवा|गर्भवती|माँ|मां|बहू|गृहिणी|पत्नी)|\b(rahi|sakti|gayi) (hoon|hu|hun)\b")
_CALLER_M = re.compile(r"(रहा|सकता|चुका|गया|ता|था) (हूँ|हूं)|ूँगा|ूंगा|ऊँगा|ऊंगा|"
                       r"(विधुर) (हूँ|हूं)|साल का[^।,.?]{0,25}(हूँ|हूं)|"
                       r"मैं (विधुर|पिता|पति)|\b(raha|sakta|gaya) (hoon|hu|hun)\b")


def caller_gender_from(text: str) -> str | None:
    """'f' / 'm' when the caller's own first-person grammar makes it explicit, else None."""
    f, m = bool(_CALLER_F.search(text)), bool(_CALLER_M.search(text))
    if f and not m:
        return "f"
    if m and not f:
        return "m"
    return None


AUDIO_TAGS = ("laugh", "giggle", "sigh", "exhales", "pauses", "excited", "slow", "whispers")


def strip_tags(text: str) -> str:
    """Remove Timbre audio tags like <pauses> (for display and grounding checks)."""
    return re.sub(r"\s*<[a-z ]{3,20}>\s*", " ", text).strip()


def speakable(text: str) -> str:
    # 10-digit PNR -> "4 5 2, 8 1 9, 0 3 3 7"
    text = re.sub(
        r"(?<![\d₹,.])(\d{3})(\d{3})(\d{4})(?![\d,.])",
        lambda m: f"{_spaced(m[1])}, {_spaced(m[2])}, {_spaced(m[3])}",
        text,
    )
    # 5-digit train number -> "1 2 6 2 2" (never "twelve thousand ...")
    text = re.sub(r"(?<![\d₹,.])(\d{5})(?![\d,.])", lambda m: _spaced(m[1]), text)
    # 6-digit OTP -> digit by digit
    text = re.sub(r"(?<![\d₹,.])(\d{6})(?![\d,.])", lambda m: _spaced(m[1]), text)
    return text
