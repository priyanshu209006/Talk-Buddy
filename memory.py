"""Learned site flows: every multi-step browse that fully succeeds is remembered per website.

Nothing is hardcoded per site. The agent learns, e.g. that on enquiry.indianrail.gov.in the
"Trains B/w Stations" tab must be clicked before "From Station*" exists, and next time it plans
the whole flow in one LLM call instead of discovering it through failed steps."""
import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("memory")
PATH = Path(__file__).with_name("data") / "site_flows.json"
_lock = threading.Lock()
MAX_PER_SITE = 6


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _shape(steps: list[dict]) -> str:
    return " -> ".join(f"{s.get('do')}:{s.get('target') or s.get('url', '')}" for s in steps)


def record(steps: list[dict], final_url: str):
    """Remember a fully successful flow (labels + example values) for its website."""
    first_open = next((s.get("url") for s in steps if s.get("do") == "open"), None)
    host = urlparse(first_open or final_url).netloc
    if not host or len(steps) < 2:
        return
    clean = [{k: v for k, v in s.items() if k in ("do", "url", "target", "text", "option", "pick", "enter")}
             for s in steps]
    with _lock:
        db = _load()
        flows = [f for f in db.get(host, []) if _shape(f["steps"]) != _shape(clean)]
        flows.insert(0, {"steps": clean})
        db[host] = flows[:MAX_PER_SITE]
        PATH.parent.mkdir(exist_ok=True)
        PATH.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("learned flow on %s: %s", host, _shape(clean))


def prompt_block(max_sites: int = 8) -> str:
    db = _load()
    if not db:
        return ""
    lines = ["\n\nLEARNED FLOWS (these browse step sequences worked before on these sites; reuse the "
             "same labels and order, swapping in the caller's values):"]
    for host, flows in list(db.items())[:max_sites]:
        for f in flows[:3]:
            parts = []
            for s in f["steps"]:
                if s["do"] == "open":
                    parts.append(f"open {s.get('url')}")
                elif s["do"] == "type":
                    parts.append(f"type '{s.get('target')}'='{s.get('text')}'" + (" pick" if s.get("pick") else ""))
                elif s["do"] == "select":
                    parts.append(f"select '{s.get('target')}'='{s.get('option')}'")
                else:
                    parts.append(f"{s['do']} '{s.get('target') or s.get('text')}'")
            lines.append(f"- {host}: " + " -> ".join(parts))
    return "\n".join(lines)
