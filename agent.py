"""Groq-powered agent: turns a phone conversation into browser actions and speech.

Designed to stay inside Groq's free-tier limits (8k tokens/min per model):
- one `say` call carries several voice segments (guide -> site -> guide),
- one `fill_form` call fills a whole form and can submit it,
- only the newest page snapshot is sent in full,
- requests rotate across models, each of which has its own token budget.
"""
import asyncio
import json
import logging
import os
import re
import time

import gnani
import llm
import memory

log = logging.getLogger("agent")

FALLBACK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

GREETING = "Hello! I am Dial-a-Browser. I can browse any website for you on this call. What would you like to check? / नमस्ते! बताइए, क्या देखना है?"
# Played the instant a real request ends, before any thinking: the line is never silent.
ACKS = ["Sure, one second…", "Checking now…", "जी, एक सेकंड…", "अच्छा, अभी देखता हूँ…"]

PORTAL = f"http://localhost:{os.environ.get('PORT', '8000')}/portal"

SYSTEM = f"""You are "Dial-a-Browser": an intelligent voice assistant that browses any website on live calls. \
The caller can see your live browser screen on their device in real time, and you drive the browser for them while talking naturally.

LANGUAGE:
- Always reply in the EXACT SAME LANGUAGE the caller speaks.
- If the caller speaks English, respond in clear, fluent, natural English!
- If the caller speaks Hindi, respond in everyday Hindi (in Devanagari script).
- If the caller speaks Hinglish, respond in Hinglish.
- If the caller speaks Tamil, Telugu, Kannada, Bengali, Marathi, or any other language, respond in that language.
- Immediately adapt if the caller switches language at any point.

VOICE & TONE:
- Speak as a friendly, helpful assistant. Keep spoken answers concise (1-3 sentences per turn).
- Explain and summarize website facts directly and clearly.
- Never stay silent while browsing: narrate briefly what you are doing (e.g. "Opening Sydney Sweeney pictures on screen now…").

CALL CONTINUITY:
- NEVER hang up or call end_call on your own.
- Only call end_call if the caller explicitly says goodbye or asks to end the call (e.g. "bye", "goodbye", "hang up", "call cut kar do").
- When answering a question or finishing a task, ALWAYS stay on the line and ask: "Is there anything else you would like me to check?"

AUTONOMOUS TASKS & WEBSITES:
You are an autonomous web-browsing agent. You can accomplish ANY task on the web that the user asks for:
- Displaying Images / Photos / Wallpapers: When asked to show or display images, photos, or pictures of ANY person, celebrity, place, animal, or object:
  IMMEDIATELY open Bing Images:
  open_url(url="https://www.bing.com/images/search?q=<query>", narration="Opening pictures on screen now…")
  Do NOT open Wikipedia or text pages when images are requested! Open the image gallery directly so the user sees the pictures right on their live screen!
- General Search, Questions & News: When asked for general queries or facts, use Bing Search:
  open_url(url="https://www.bing.com/search?q=<query>", narration="Searching the web for that…")
- Videos & Entertainment: YouTube (`https://www.youtube.com/results?search_query=<query>`), IMDb for movies and cast.
- Sports & Live Cricket: Open `https://www.cricbuzz.com/cricket-match/live-scores` or Bing Sports.
- Weather & Forecasts: Open `https://www.bing.com/search?q=weather+<city>` or `https://mausam.imd.gov.in/`.
- Encyclopedic Articles & Biographies: Wikipedia (`https://en.wikipedia.org/wiki/<topic>`) - use this ONLY when the user asks for detailed biographical facts, history, or encyclopedia articles, NOT when they ask for images!
- Indian Railways & Trains:
  - Live train running status / "where is my train" / schedule / platform: NTES `https://enquiry.indianrail.gov.in/mntes/` . On the home page, type the train number into "Train No./Name" with type_and_submit.
  - Trains between two stations: NTES "Trains B/w Stations".
  - Train search and fares: IRCTC `https://www.irctc.co.in/nget/train-search` (search only).
- Government schemes: `https://www.myscheme.gov.in/search` .
- Ticket BOOKING walkthrough: use the RailSeva demo portal {PORTAL} and tell the caller once that it is a demo booking.
- ANY OTHER TASK: You have full access to the live internet. Autonomously navigate to whichever website best solves the user's request.

SPEED (every LLM round trip costs the caller seconds)
- Prefer ONE browse call that does the whole flow: e.g. [open url, type "From Station*" = "Bhubaneswar" \
(pick=true), type "To Station*" = "Khurda Road" (pick=true), click "Get Trains"]. Target fields/buttons by \
their visible label. Use single-element tools only to recover when a browse step fails.
- After each action you get "PAGE CHANGE: ...". If it says new fields/content appeared, the action WORKED \
(many sites change content without changing the URL). Never repeat an action that already worked.
- Don't narrate every step; one short narration per browse call is enough.

DEPTH (be genuinely useful, not just literal)
- Answer the question, then add the 1-2 facts the caller will need next, taken from the page: for a train, \
its delay, expected time and platform at their station; for a scheme, benefit amount, who is eligible, \
documents and how to apply; for a list, how many results and the best match for them.
- If the page doesn't answer it, say so plainly and offer the closest real alternative.

BROWSING
- Pages arrive as text + interactive elements with ids like [e12]; use ids from the latest snapshot only.
- Search boxes (NTES, myScheme, site search): browse type step or type_and_submit, not fill_form.
- Do not open adult/porn, gambling or piracy sites; politely decline and offer something else.
- Never enter passwords, card/bank details or Aadhaar. If a site needs login, captcha or payment, tell the caller \
they must do that step themselves.
- Remember everything the caller said (name, age, mobile, berth...). Never ask twice. Ask for missing info \
in one question.
- Forms: one fill_form call with ALL known fields (+ submit_id when complete).
- Before any irreversible step (pay/book): on the review page, read the review details in the SITE voice and \
ask for a clear yes (guide).
- OTP: ask the caller to read the OTP they got by SMS. Never invent one.
- If interrupted, stop and follow the new instruction.
- When done and the caller needs nothing else: say goodbye, then end_call.
- ALWAYS respond with tool calls, never plain text. To ask for several form fields, ask in ONE short guide \
sentence (don't read the empty form aloud).
Every browser tool has a short "narration" (guide voice) spoken while it runs, so there is never dead air.
"""

_SEGMENTS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "voice": {"type": "string", "enum": ["guide", "site"]},
            "text": {"type": "string"},
        },
        "required": ["voice", "text"],
    },
}
_NARR = {"type": "string", "description": "Short guide-voice line spoken while this runs, or empty."}


def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _tool("say", "Speak to the caller: one or more segments, each in the guide or site voice.",
          {"segments": _SEGMENTS,
           "wait_for_reply": {"type": "boolean",
                              "description": "true when you asked something or finished, and should now listen."}},
          ["segments", "wait_for_reply"]),
    _tool("browse", "FASTEST way to act: run several browser steps in ONE call, no round trip between "
                    "them. Steps target fields/buttons by their visible label or text, so you can plan a "
                    "page you haven't seen yet. Steps: {do:'open',url} | {do:'type',target,text,enter?,pick?} "
                    "(enter defaults true; pick=true picks the first autocomplete suggestion) | "
                    "{do:'click',target} | {do:'select',target,option} | {do:'wait_for',text}. "
                    "Stops at the first failing step. Returns the final page + what each step did.",
          {"steps": {"type": "array", "items": {"type": "object", "properties": {
              "do": {"type": "string", "enum": ["open", "type", "click", "select", "wait_for"]},
              "url": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"},
              "option": {"type": "string"}, "enter": {"type": "boolean"}, "pick": {"type": "boolean"}},
              "required": ["do"]}},
           "narration": _NARR},
          ["steps", "narration"]),
    _tool("open_url", "Open a URL. Returns the page snapshot.",
          {"url": {"type": "string"}, "narration": _NARR}, ["url", "narration"]),
    _tool("click", "Click an element by id. Returns the new page snapshot.",
          {"element_id": {"type": "string"}, "narration": _NARR}, ["element_id", "narration"]),
    _tool("fill_form", "Fill several inputs/selects at once (selects take the visible option label), "
                       "optionally clicking a submit element afterwards. Returns the page snapshot.",
          {"fields": {"type": "array", "items": {"type": "object", "properties": {
              "element_id": {"type": "string"}, "value": {"type": "string"}},
              "required": ["element_id", "value"]}},
           "submit_id": {"type": "string", "description": "Element to click after filling, or empty."},
           "narration": _NARR},
          ["fields", "narration"]),
    _tool("type_and_submit", "Click a search box, type text like a human (triggers autocomplete) and press "
                             "Enter. Use for search boxes. Returns the page snapshot.",
          {"element_id": {"type": "string"}, "text": {"type": "string"},
           "pick_suggestion": {"type": "boolean",
                               "description": "Press ArrowDown first to pick the first autocomplete suggestion."},
           "narration": _NARR},
          ["element_id", "text", "narration"]),
    _tool("go_back", "Browser back button.", {"narration": _NARR}, []),
    _tool("set_voice", "Switch the assistant (guide) voice when the caller asks for a male or female voice.",
          {"gender": {"type": "string", "enum": ["female", "male"]}}, ["gender"]),
    _tool("end_call", "Hang up (after saying goodbye with say).", {}, []),
]


def _prose_segments(text: str) -> list[dict]:
    """Split 'guide\\n...\\nsite\\n...' or 'guide: ...' style prose into voice segments."""
    segs, voice, buf = [], "guide", []
    for line in text.splitlines():
        m = re.match(r"^\s*(guide|site)\s*(?:voice)?\s*[:\-]?\s*(.*)$", line, re.I)
        if m:
            if buf:
                segs.append({"voice": voice, "text": " ".join(buf)})
            voice, buf = m[1].lower(), ([m[2].strip()] if m[2].strip() else [])
        elif line.strip():
            buf.append(line.strip())
    if buf:
        segs.append({"voice": voice, "text": " ".join(buf)})
    return segs or [{"voice": "guide", "text": text}]


class Agent:
    def __init__(self, call):
        self.call = call                      # server.Call
        # No SDK auto-retry: on a rate limit we move to another model instead of sleeping mid-call.
        self.pool = llm.Pool(prefer=os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b")
        self.messages = [{"role": "system", "content": SYSTEM}]
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.ended = False
        # State the duplex listener uses to judge what the caller says.
        self.waiting = False          # asked something and is listening for the answer
        self.last_question = ""
        self.recent_user: list[str] = []
        # Caller's grammatical gender, learned only from how THEY refer to themselves
        # ("मैं जा रही हूँ" -> f). None = unknown -> neutral address. Never guessed from pitch/name.
        self.caller_gender: str | None = None

    def hear(self, text: str):
        said = text.split("  [detected")[0]
        self.recent_user = (self.recent_user + [said])[-4:]
        self.waiting = False
        m_lang = re.search(r"\[detected language:\s*([a-zA-Z\-_]+)\]", text)
        if m_lang:
            self.user_lang = m_lang.group(1).lower()
        elif any("\u0900" <= c <= "\u097F" for c in said):
            self.user_lang = "hi"
        elif not getattr(self, "user_lang", None):
            self.user_lang = "en"
        g = gnani.caller_gender_from(said)
        if g and g != self.caller_gender:
            self.caller_gender = g
            log.info("caller refers to themself with %s grammar", "feminine" if g == "f" else "masculine")
        self.inbox.put_nowait(text)

    def _gender_rules(self) -> str:
        me = gnani.voice_gender(self.call.guide_voice)
        mine = ("You speak with a WOMAN's voice: always feminine first person (मैं देख रही हूँ, बता सकती हूँ, "
                "बताऊँगी)." if me == "f" else
                "You speak with a MAN's voice: always masculine first person (मैं देख रहा हूँ, बता सकता हूँ, "
                "बताऊँगा).")
        if self.caller_gender == "f":
            them = ("The caller refers to herself with feminine grammar: address her with feminine forms "
                    "(आप पहुँच जाएँगी, आप योग्य हैं, आप 62 साल की हैं).")
        elif self.caller_gender == "m":
            them = ("The caller refers to himself with masculine grammar: address him with masculine forms "
                    "(आप पहुँच जाएँगे, आप 62 साल के हैं).")
        else:
            them = ("The caller's gender is NOT known. Do not guess it from their name, voice or topic. Use "
                    "gender-neutral phrasing: imperatives and आपको/आपका constructions (\"बताइए\", \"आपको "
                    "मिलेगा\", \"आपकी ट्रेन\"), not gendered verb endings for the caller.")
        return (f"\n\nGENDER (Hindi grammar)\n- {mine}\n- {them}\n- Third persons take their own gender from "
                "what the caller said (\"मेरी बेटी\" -> वो पहुँच जाएगी). Trains are feminine (ट्रेन आ रही है).\n"
                "- If the caller asks for a male/female voice (\"आदमी की आवाज़ में बोलो\"), call set_voice.")

    def _drain(self) -> bool:
        got = False
        while not self.inbox.empty():
            self.messages.append({"role": "user", "content": self.inbox.get_nowait()})
            got = True
        return got

    async def run(self):
        # Fixed greeting: no LLM round trip, and its audio is pre-cached, so it starts instantly.
        self.messages.append({"role": "user", "content": "(Call connected.)"})
        self.messages.append({"role": "assistant", "content": GREETING})
        await self._exec("say", {"segments": [{"voice": "guide", "text": GREETING}], "wait_for_reply": True})
        while not self.ended:
            text = await self.inbox.get()
            self.messages.append({"role": "user", "content": text})
            # Brief debounce (up to ~0.6s) to coalesce closely-spaced transcript segments
            for _ in range(3):
                if not self.call.user_talking and self.inbox.empty():
                    break
                await asyncio.sleep(0.2)
                self._drain()
            self._drain()
            await self._turn()

    # ------------------------------------------------------------------ turn loop
    async def _turn(self, max_steps: int = 12):
        for _ in range(max_steps):
            self._drain()
            notes = self.call.pop_notes()
            if notes:
                self.messages.append({"role": "user", "content": "(system note) " + " ".join(notes)})
            resp = await self._llm()
            if resp is None:
                is_en = getattr(self, "user_lang", "en").startswith("en")
                if getattr(self, "quota_out_until", 0) > time.monotonic():
                    msg = ("Sorry, my system is very busy right now. Please call again in a few minutes."
                           if is_en else
                           "माफ़ कीजिए, अभी मेरा सिस्टम बहुत busy है। कुछ मिनट बाद फिर से call कीजिए।")
                    await self.call.speak(msg, "guide")
                else:
                    await self.call.ui({"type": "log", "text": "Groq: no model available for this step"})
                    msg = ("Sorry, I had a brief hiccup. Could you say that again?"
                           if is_en else
                           "माफ़ कीजिए, एक सेकंड दिक्कत आ गई। फिर से बोलिए?")
                    await self.call.speak(msg, "guide")
                self.waiting = True
                return
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                # Prose instead of a tool call (seen on fallback models). Recover the
                # guide/site structure if it imitated it, and treat it as a question.
                text = re.sub(r"[*_`#]+", "", (msg.content or "")).strip()
                self.messages.append({"role": "assistant", "content": text})
                await self._exec("say", {"segments": _prose_segments(text), "wait_for_reply": True})
                return
            self.messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in calls],
            })
            stop = False
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result, done_turn = await self._exec(c.function.name, args)
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                stop = stop or done_turn
            if stop:
                return
        is_en = getattr(self, "user_lang", "en").startswith("en")
        prompt_next = "What would you like me to check next?" if is_en else "आप बताइए, आगे क्या करना है?"
        await self.call.speak(prompt_next, "guide")
        self.waiting = True

    def _compact(self) -> list:
        """Only the newest page snapshot is sent in full; older ones shrink to their URL line."""
        last_page = max((i for i, m in enumerate(self.messages)
                         if m["role"] == "tool" and m["content"].startswith(("URL:", "ERROR:"))), default=-1)
        out = []
        for i, m in enumerate(self.messages):
            if m["role"] == "tool" and i != last_page and len(m["content"]) > 200:
                m = {**m, "content": m["content"].splitlines()[0] + " (older page snapshot omitted)"}
            out.append(m)
        # System prompt + live gender rules (they change with the voice and what the caller says).
        out[0] = {"role": "system", "content": self.messages[0]["content"] + self._gender_rules()
                  + memory.prompt_block()}
        return out

    async def _llm(self):
        msgs = self._compact()
        said_wait = False
        give_up_at = time.monotonic() + 35        # per-minute limits free up within ~30 s
        while time.monotonic() < give_up_at:
            # Preferred order (Groq models, then Nebius), skipping any that are cooling down.
            for slot in self.pool.ready():
                for choice in ("required", "auto"):
                    try:
                        t = time.monotonic()
                        resp = await self.pool.create(slot, messages=msgs, tools=TOOLS, tool_choice=choice,
                                                      temperature=0.3, max_tokens=1500)
                    except llm.RateLimited:
                        break                                  # next slot, no waiting
                    except Exception as e:
                        status = getattr(e, "status_code", None)
                        if status == 400:
                            log.info("bad generation on %s (tool_choice=%s): %s", slot[0], choice, str(e)[:120])
                            continue
                        log.warning("LLM call failed on %s: %s", slot[0], str(e)[:160])
                        break
                    m = resp.choices[0].message
                    u = resp.usage
                    log.info("LLM %s %.2fs %s tok -> %s", slot[0], time.monotonic() - t,
                             u.prompt_tokens if u else "?", [c.function.name for c in (m.tool_calls or [])])
                    if m.tool_calls or (m.content or "").strip():
                        return resp
                    log.info("empty generation on %s", slot[0])
                    break
            # Everyone is cooling down (or failed): keep the line alive and wait for the first to free up.
            wait = self.pool.next_free_in()
            if wait > 20:
                # Not a per-minute blip: the key's hourly/daily quota is used up. Waiting minutes in
                # silence is the worst experience, so say so now.
                log.warning("LLM quota exhausted on all models (next free in %.0fs)", wait)
                self.quota_out_until = time.monotonic() + wait
                await self.call.ui({"type": "quota", "seconds": int(wait),
                                    "text": f"LLM quota exhausted on all providers; next free in ~{int(wait // 60) + 1} min"})
                return None
            if not said_wait and wait > 1.5:
                said_wait = True
                is_en = getattr(self, "user_lang", "en").startswith("en")
                wait_msg = "Just a moment, checking that for you…" if is_en else "बस एक सेकंड, देख रही हूँ…"
                asyncio.create_task(self.call.speak(wait_msg, "guide"))
            log.info("all models busy; waiting %.1fs", max(wait, 0.5))
            await asyncio.sleep(min(max(wait, 0.5), 10.0))
        return None

    # ------------------------------------------------------------------ tools
    async def _exec(self, name: str, a: dict) -> tuple[str, bool]:
        b = self.call.browser
        await self.call.ui({"type": "action", "name": name, "args": a})
        try:
            if name == "say":
                segs = a.get("segments") or ([{"voice": a.get("voice", "guide"), "text": a["text"]}]
                                             if a.get("text") else [])
                # Grounding restriction disabled: speak all segments naturally without blocking
                wait = bool(a.get("wait_for_reply", True))
                trail = self.__dict__.get("trail") or []
                if len(trail) >= 2:
                    memory.record(list(trail), b.page.url)
                    self.trail = []
                guide = [s.get("text", "") for s in segs]
                if guide:
                    self.last_question = gnani.strip_tags(guide[-1])
                for s in segs:
                    plain_text = gnani.strip_tags(s.get("text", ""))
                    await self.call.speak(plain_text, "guide")
                # If the caller already said something while we were talking, we're not
                # waiting any more (avoids a race that mislabels their next words).
                self.waiting = wait and self.inbox.empty()
                return "spoken", wait
            if name == "set_voice":
                g = "m" if a.get("gender") == "male" else "f"
                self.call.guide_voice = gnani.GUIDE_BY_GENDER[g]
                log.info("guide voice -> %s", self.call.guide_voice)
                await self.call.ui({"type": "log", "text": f"voice switched to {self.call.guide_voice}"})
                return (f"Voice is now {self.call.guide_voice} ({'male' if g == 'm' else 'female'}). Use "
                        f"{'masculine' if g == 'm' else 'feminine'} first-person grammar from now on and "
                        "confirm the switch to the caller in one short line."), False
            if name == "end_call":
                recent_user = " ".join(m.get("content", "") for m in self.messages[-4:] if m.get("role") == "user").lower()
                hangup_pattern = r"\b(bye|goodbye|hang up|call cut|disconnect|call kat do|call band karo|alvida|phone rakh do|end call)\b"
                if re.search(hangup_pattern, recent_user):
                    self.ended = True
                    await self.call.hangup(after_speech=True)
                    return "call ended", True
                return "Do not hang up. Stay on the line and ask the caller if they need anything else.", False

            narration = (a.get("narration") or "").strip()
            if narration:
                # Talk while the browser works: don't wait for the line to finish.
                asyncio.create_task(self.call.speak(narration, "guide"))
            try:
                return await self._browse(name, a)
            except Exception as e:
                if "has been closed" not in str(e) and b.alive():
                    raise
                # The browser process died: relaunch, restore the page, retry once.
                last_url = getattr(b, "last_url", None)
                await b.restart(last_url)
                if name in ("click", "fill_form", "type_and_submit", "browse"):
                    # element ids belong to the dead page; hand back a fresh snapshot instead
                    return ("The browser restarted; element ids changed. Fresh page:\n"
                            + await b.snapshot()), False
                return await self._browse(name, a)
        except Exception as e:
            log.warning("tool %s failed: %s", name, e)
            try:
                snap = await b.snapshot()
            except Exception:
                snap = ""
            return f"ERROR: {e}. Current page:\n{snap}", False

    async def _browse(self, name: str, a: dict) -> tuple[str, bool]:
        b = self.call.browser
        before_url = b.page.url if b.page else ""
        before = {self._label(e) for e in (getattr(b, "last_elements", None) or [])}
        self._trail_add(name, a, b)
        if name == "browse":
            steps = a.get("steps") or []
            snap = await b.run_steps(steps)
            tail = snap.rsplit("STEPS:", 1)[-1]
            ok = steps[:tail.count("✓")]
            trail = self.__dict__.setdefault("trail", [])
            if any(s.get("do") == "open" for s in ok):
                trail.clear()
            trail.extend(ok)
            if "✗" not in tail and steps:
                memory.record(steps, b.page.url)          # learn this site's working flow
        elif name == "open_url":
            snap = await b.open(a["url"])
        elif name == "click":
            snap = await b.click(a["element_id"])
        elif name == "fill_form":
            snap = await b.fill_form(a.get("fields") or [], a.get("submit_id") or None)
        elif name == "type_and_submit":
            snap = await b.type_and_submit(a["element_id"], a.get("text", ""),
                                           bool(a.get("pick_suggestion", False)))
        elif name == "go_back":
            snap = await b.back()
        else:
            return f"unknown tool {name}", False
        b.last_url = b.page.url
        asyncio.create_task(self.call.push_screen())      # don't make the caller wait for a screenshot
        # Tell the model what this action changed. Many sites (NTES, myScheme) swap content
        # without changing the URL, and without this the model repeats a click that worked.
        after = {self._label(e) for e in (getattr(b, "last_elements", None) or [])}
        added = [x for x in after - before if x][:12]
        if b.page.url != before_url:
            change = f"PAGE CHANGE: navigated to {b.page.url}"
        elif added:
            change = "PAGE CHANGE: same URL, new on the page: " + "; ".join(sorted(added))
        else:
            change = ("PAGE CHANGE: none visible. Do NOT repeat the same action; read the page below or "
                      "try a different element.")
        lines = snap.split("\n", 2)
        return "\n".join(lines[:2] + [change] + lines[2:]), False

    def _trail_add(self, name: str, a: dict, b):
        """Record what was done in label terms (element ids die with the page), so a sequence of
        single actions that ends in a verified answer can be learned as a flow too."""
        trail = self.__dict__.setdefault("trail", [])
        label = lambda eid: next((m[1] for m in (re.match(rf'\[{re.escape(eid)}\] \S+ "(.*?)"', ln)
                                                for ln in (getattr(b, "last_elements", None) or [])) if m), None)
        if name == "open_url":
            trail.clear()
            trail.append({"do": "open", "url": a.get("url")})
        elif name == "browse":
            pass      # recorded after it runs, successful steps only (see _browse)
        elif name == "click" and label(a.get("element_id", "")):
            trail.append({"do": "click", "target": label(a["element_id"])})
        elif name == "type_and_submit" and label(a.get("element_id", "")):
            trail.append({"do": "type", "target": label(a["element_id"]), "text": a.get("text", ""),
                          "pick": bool(a.get("pick_suggestion"))})
        elif name == "fill_form":
            for f in a.get("fields") or []:
                if label(f.get("element_id", "")):
                    trail.append({"do": "type", "target": label(f["element_id"]), "text": f.get("value", ""),
                                  "enter": False})
            if a.get("submit_id") and label(a["submit_id"]):
                trail.append({"do": "click", "target": label(a["submit_id"])})

    @staticmethod
    def _label(el_line: str) -> str:
        m = re.match(r'\[e\d+\] (\S+) "(.*?)"', el_line)
        return f'{m[1]} "{m[2]}"' if m and m[2] else ""
