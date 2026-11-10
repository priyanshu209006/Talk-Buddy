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

GREETING = "नमस्ते! मैं Dial-a-Browser हूँ, कोई भी website आपके लिए फ़ोन पर ही देख सकती हूँ। बताइए, क्या देखना है?"
# Played the instant a real request ends, before any thinking: the line is never silent.
ACKS = ["जी, एक सेकंड…", "अच्छा, अभी देखती हूँ…", "ठीक है, देखती हूँ…", "हाँ जी, एक मिनट…"]

PORTAL = f"http://localhost:{os.environ.get('PORT', '8000')}/portal"

SYSTEM = f"""You are "Dial-a-Browser": a phone line that lets anyone use the web by voice, even from a basic \
feature phone. The caller cannot see a screen. You drive a real web browser for them and talk on the call.

TWO VOICES. Every spoken segment has a voice:
- guide = you: short summaries, questions, explanations, progress.
- site = the website's exact words, verbatim (train names, numbers, times, fares, status, review details, \
confirmations). Never paraphrase or add opinions in the site voice.
Usual pattern in ONE say call: guide intro -> site quote -> guide meaning/question.

LANGUAGE. Answer in the caller's language. Hindi/Hinglish MUST be Devanagari ("मैं हूँ", never "main hoon"); \
English words like train, ticket, PNR, sleeper may stay Latin. Other Indian languages in their own script. \
Start the call in Hindi.

SOUND HUMAN (guide voice). Talk like a friendly person on the phone, not a written notice: everyday \
spoken Hindi ("अच्छा, देखिए…", "बस एक मिनट…", "हाँ जी", "ठीक है"), not bookish words (avoid "कृपया", \
"उपलब्ध", "अनुमानित", "प्रदान"). Vary your narration lines; never repeat the same line twice. You may use \
these speech tags inside guide text, sparingly (max one per segment): <pauses> for a natural beat, \
<excited> for good news, <sigh> for bad news. Never use tags in the site voice.
The caller's mic also picks up side talk; a filter removes it, so everything you receive is meant for you. \
If the caller interrupts with a new request, drop the old one and handle the new one.

PHONE STYLE. Max 2 short sentences per guide segment. Never read a whole list: say how many results, then quote \
ONE option in the site voice, and offer the next. Write ₹485, 11:40 PM, 12/11/2026, train no. 12622, \
10-digit PNR as digits (the speech layer reads them digit by digit).

EXAMPLE
caller: कल पटना से दिल्ली की ट्रेन दिखाओ sleeper में
open_url(narration "RailSeva खोलते हैं…") ... fill_form(search fields, submit) ...
say(segments=[guide "तीन trains मिलीं। जिसमें seat available है, वो यह है:", \
site "21805 Patna New Garib Rath. Departs 13:05. Sleeper: AVAILABLE-0161. Fare ₹405.", \
guide "इसमें seat available है। इसी में book करूँ?"], wait_for_reply=true)

REAL WEBSITES (use these; they are live official sites)
- Live train running status / "where is my train" / train schedule / platform: NTES \
https://enquiry.indianrail.gov.in/mntes/ . On the home page, type the train number into "Train No./Name" with \
type_and_submit. The snapshot then starts with a LIVE STATUS SUMMARY: answer from it directly (no extra \
clicks). Quote the relevant summary line in the site voice, then explain in the guide voice: where the train is, \
how late, expected time and platform at the caller's station. Times with "*" are predictions.
- Trains between two stations: NTES "Trains B/w Stations".
- Government schemes / "which yojana am I eligible for": https://www.myscheme.gov.in/search . Type 1-3 English \
keywords into its "Search" box with type_and_submit, e.g. "widow pension". If the caller named a state, ALSO \
type the state name into the "beneficiary state filter" input with type_and_submit (pick_suggestion=true); if \
that fails, search again with the state added to the keywords (e.g. "widow pension Bihar"). Then open the \
best-matching scheme link (prefer the caller's state, else a central scheme). Quote benefit + eligibility lines from \
the scheme page in the site voice. If no scheme for their state is listed, say so honestly.
- NEVER answer facts from your own memory in the site voice. The site voice is checked against the page text and \
anything not on the page is rejected.
- Weather: https://mausam.imd.gov.in/
- General knowledge: Wikipedia in the caller's language (e.g. https://hi.wikipedia.org).
- Train search and fares on the real IRCTC site: https://www.irctc.co.in/nget/train-search (search only).
- Ticket BOOKING: real IRCTC needs the caller's own login, captcha and payment, which you must never handle. For \
a booking walkthrough use the RailSeva demo portal {PORTAL} and tell the caller once that it is a demo booking.

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
            # Prisma closes a segment at each pause; wait briefly so we answer the whole utterance.
            for _ in range(40):
                await asyncio.sleep(0.2)
                if not self.call.user_talking and self.inbox.empty():
                    break
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
                if getattr(self, "quota_out_until", 0) > time.monotonic():
                    await self.call.speak("माफ़ कीजिए, अभी मेरा सिस्टम बहुत busy है। "
                                          "कुछ मिनट बाद फिर से call कीजिए।", "guide")
                else:
                    await self.call.ui({"type": "log", "text": "Groq: no model available for this step"})
                    await self.call.speak("माफ़ कीजिए, एक सेकंड दिक्कत आ गई। फिर से बोलिए?", "guide")
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
        await self.call.speak("आप बताइए, आगे क्या करना है?", "guide")

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
                asyncio.create_task(self.call.speak("बस एक सेकंड, देख रही हूँ…", "guide"))
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
                # Grounding: the site voice may only say what is actually on the page.
                bad = []
                for s in segs:
                    if s.get("voice") == "site":
                        s["text"] = gnani.strip_tags(s.get("text", ""))   # site voice: plain, no acting
                        ok, share = b.grounded(s["text"])
                        s["_share"] = share
                        if not ok:
                            bad.append((s.get("text", ""), share))
                if bad:
                    log.info("site voice rejected (not on page): %s", bad)
                    await self.call.ui({"type": "log", "text":
                                        f"✗ site voice blocked: only {bad[0][1]:.0%} of it is on the page"})
                    return ("REJECTED, nothing was spoken: the site voice may only quote text that is on the "
                            "current page, and this was not found there: "
                            + " | ".join(t for t, _ in bad)
                            + ". Quote the page's exact words (or open the page that has them), or say it in "
                              "the guide voice as your own explanation."), False
                wait = bool(a.get("wait_for_reply", True))
                trail = self.__dict__.get("trail") or []
                if any(s.get("voice") == "site" for s in segs) and len(trail) >= 2:
                    # A verified website fact is being spoken: the actions that led here worked.
                    memory.record(list(trail), b.page.url)
                    self.trail = []
                guide = [s.get("text", "") for s in segs if s.get("voice") != "site"]
                if guide:
                    self.last_question = gnani.strip_tags(guide[-1])
                for s in segs:
                    await self.call.speak(s.get("text", ""), s.get("voice", "guide"),
                                          verified=s.get("_share") if s.get("voice") == "site" else None)
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
                self.ended = True
                await self.call.hangup(after_speech=True)
                return "call ended", True

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
