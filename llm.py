"""LLM pool: several (provider, model) pairs behind one call.

Groq first (fastest), then Nebius Token Factory. On a rate limit we don't wait: we move to the
next pair that isn't cooling down. Both speak the OpenAI chat-completions dialect.
"""
import logging
import os
import time

from groq import AsyncGroq
from openai import AsyncOpenAI

log = logging.getLogger("llm")

NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1/"
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
# Benchmarked on this agent's real prompt + tools (see README): DeepSeek V4 Pro planned the whole
# NTES flow in one call and wrote the best Hindi; GLM-5.3 Flash also planned right but answered slower.
# V4 Pro reasons at length (26 s on a big page) - too slow for a phone call; it's the backup.
NEBIUS_AGENT = ["deepseek-ai/DeepSeek-V4.1-Flash-1017", "deepseek-ai/DeepSeek-V4-Pro-0813"]
# Small, quick judgement (duplex side-talk filter): the cheap Flash model is plenty.
NEBIUS_FAST = ["deepseek-ai/DeepSeek-V4.1-Flash-1017"]


class RateLimited(Exception):
    pass


class Pool:
    def __init__(self, groq_models=None, prefer: str | None = None, nebius_models=None):
        """Nebius first (smarter models, higher limits), Groq as the fallback."""
        self.slots = []                       # (label, client, model, kind)
        nk = os.environ.get("NEBIUS_API_KEY")
        if nk:
            n = AsyncOpenAI(api_key=nk, base_url=NEBIUS_URL, max_retries=0, timeout=40)
            env = os.environ.get("NEBIUS_MODELS", "")
            nm = [m.strip() for m in env.split(",") if m.strip()] if env and nebius_models is None \
                else list(nebius_models or NEBIUS_AGENT)
            self.slots += [(f"nebius:{m}", n, m, "nebius") for m in nm]
        gk = os.environ.get("GROQ_API_KEY")
        if gk:
            g = AsyncGroq(api_key=gk, max_retries=0)
            models = list(groq_models or GROQ_MODELS)
            if prefer and prefer in models:
                models.remove(prefer)
                models.insert(0, prefer)
            self.slots += [(f"groq:{m}", g, m, "groq") for m in models]
        self.blocked_until = {s[0]: 0.0 for s in self.slots}

    def ready(self):
        now = time.monotonic()
        return [s for s in self.slots if self.blocked_until[s[0]] <= now]

    def next_free_in(self) -> float:
        return max(0.0, min(self.blocked_until.values(), default=0) - time.monotonic())

    async def create(self, slot, **kw):
        label, client, model, kind = slot
        if "gpt-oss" in model:
            kw.setdefault("reasoning_effort", "low")
        try:
            return await client.chat.completions.create(model=model, **kw)
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                try:
                    ra = float(e.response.headers.get("retry-after", "5"))
                except Exception:
                    ra = 5.0
                self.blocked_until[label] = time.monotonic() + ra
                log.info("rate limited on %s for %.0fs", label, ra)
                raise RateLimited(label) from e
            raise
