"""Full-duplex listening: decide whether something the caller's mic picked up is meant
for the assistant, or is side talk / backchannel / noise / our own echo to be ignored."""
import asyncio
import json
import logging
import os
import re
import time

import llm

log = logging.getLogger("duplex")

RELEVANT, BACKCHANNEL, SIDE_TALK, NOISE, ECHO = "RELEVANT", "BACKCHANNEL", "SIDE_TALK", "NOISE", "ECHO"

PROMPT = """You are the listening filter of a phone voice assistant ("Dial-a-Browser") that browses \
websites for a caller. The caller's microphone also picks up other people, TV and the caller talking \
to someone else. Classify the LATEST UTTERANCE:

RELEVANT    - meant for the assistant: a request or question for it, an answer to what it just asked \
(even "हाँ", "नहीं", a number, a name), a correction, "रुको/wait/stop/एक मिनट", or a NEW request \
addressed to it (e.g. "अच्छा, मौसम भी बताओ").
BACKCHANNEL - only acknowledgement while the assistant is explaining and did NOT ask anything: \
"हम्म", "अच्छा", "ठीक है", "ok", "हाँ हाँ".
SIDE_TALK   - the caller (or someone near them) talking to another person or about something unrelated \
to the call: "बेटा खाना खा लिया?", "TV धीरे करो", "मम्मी आ रही हूँ", gossip, scolding kids.
NOISE       - fragments, TV/radio/announcement audio, gibberish, half words.

Be decisive. When an utterance could plausibly be an answer to the assistant's last question, it is \
RELEVANT. Reply with JSON only: {"v": "<LABEL>", "why": "<max 8 words>"}"""

_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]

_BACKCHANNEL = {"हम्म", "हम", "हूँ", "हुँ", "अच्छा", "अच्छाा", "ठीक", "है", "हाँ", "हां", "जी", "ओके", "ok", "okay",
                "hmm", "hm", "haan", "han", "accha", "achha", "acha", "theek", "hai", "ji", "उम्म", "अहा", "achcha"}


def _words(s: str) -> list[str]:
    # Include Indic vowel signs/virama (category Mn), which \w alone splits on: "रुको" stays one word.
    return re.findall(r"[\wऀ-෿]+", s.lower())


def echo_score(heard: str, spoken: str) -> float:
    """Share of the heard words that also appear in what the assistant just said."""
    h, s = _words(heard), set(_words(spoken))
    if len(h) < 3 or not s:
        return 0.0
    return sum(w in s for w in h) / len(h)


class Listener:
    def __init__(self):
        # Small/fast models first: this runs on every utterance heard while the assistant talks.
        self.pool = llm.Pool(groq_models=_MODELS, nebius_models=llm.NEBIUS_FAST)

    async def classify(self, text: str, *, assistant_speaking: bool, speaking_text: str,
                       recent_said: str, last_question: str, waiting: bool, task: str) -> tuple[str, str]:
        # 1) Echo: our own voice leaking from the speakers into the mic.
        if echo_score(text, speaking_text + " " + recent_said) >= 0.6:
            return ECHO, "matches the assistant's own speech"
        words = _words(text)
        # 2) Pure acknowledgement ("हम्म", "अच्छा", "ok") is a backchannel unless it answers a
        #    question the assistant has finished asking.
        if words and all(w in _BACKCHANNEL for w in words) and (assistant_speaking or not waiting):
            return BACKCHANNEL, "acknowledgement only"
        # 3) Fast path (no LLM, no delay): the assistant asked something and is quietly waiting for
        #    the answer, so what the caller says now is the answer. The LLM filter is only needed
        #    while the assistant is talking or busy, which is when side talk actually happens.
        if waiting and not assistant_speaking:
            return RELEVANT, "answer to the assistant's question"
        # 3) LLM judgement with the conversation state.
        ctx = (f"Caller's current task: {task or '(not stated yet)'}\n"
               f"Assistant's last question: {last_question or '(none)'}\n"
               f"Assistant is {'SPEAKING right now: ' + speaking_text if assistant_speaking else 'silent'}\n"
               f"Assistant is {'waiting for an answer' if waiting else 'not waiting for an answer'}\n"
               f"LATEST UTTERANCE: {text}")
        deadline = time.monotonic() + 3.0
        for slot in self.pool.ready():
            if time.monotonic() > deadline:
                break
            try:
                r = await asyncio.wait_for(self.pool.create(
                    slot, temperature=0, max_tokens=300,
                    messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": ctx}]),
                    timeout=max(0.5, deadline - time.monotonic()))
            except Exception as e:
                log.info("classifier %s failed: %s", slot[0], str(e)[:100])
                continue
            out = r.choices[0].message.content or ""
            m = re.search(r"\{.*\}", out, re.S)
            try:
                d = json.loads(m[0]) if m else {}
            except json.JSONDecodeError:
                d = {}
            v = str(d.get("v", "")).upper()
            if v in (RELEVANT, BACKCHANNEL, SIDE_TALK, NOISE):
                return v, str(d.get("why", ""))[:80]
        # Fail open: never drop the caller's words just because the filter is unavailable.
        return RELEVANT, "filter unavailable"
