"""Lightweight, verified image gallery endpoint for Dial-a-Browser.

Replaces heavy external JS image-search SPAs (which leak 250-350 MB RAM in Chromium
on 512 MB containers and frequently serve regional SafeSearch/bot-decoy images)
with a fast, relevance-verified, 9-thumbnail static grid served locally.
"""
import asyncio
import html
import json
import logging
import re
import unicodedata
import urllib.parse

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

log = logging.getLogger("gallery")
router = APIRouter(prefix="/gallery")

WIKI_UA = "DialABrowser/1.0 (https://github.com/priyanshu209006/Talk-Buddy)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_NOISE_WORDS = {
    "image", "images", "photo", "photos", "pic", "pics", "picture", "pictures",
    "wallpaper", "wallpapers", "hd", "4k", "show", "me", "find", "display",
    "open", "search", "for", "of", "the", "a", "an", "in", "on", "with", "and", "or",
}
_BAD_FILE_WORDS = ("signature", "logo", "flag", "map", "icon", "coat of arms", "ambox", "emblem", "locator")
_SECONDARY_WIKI = ("filmography", "discography", "bibliography", "list of ", "(musician)", "stadium", "(disambiguation)")


def _fold(s: str) -> str:
    """Fold Unicode diacritics to ASCII lowercase so 'Gojō' matches 'gojo'."""
    norm = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def clean_query(q: str) -> tuple[str, list[str]]:
    tokens = re.findall(r"[a-zA-Z0-9\u0900-\u097F]+", _fold(q))
    kept = [t for t in tokens if t not in _NOISE_WORDS]
    if not kept:
        kept = [t for t in tokens if len(t) >= 2]
    cleaned = " ".join(kept) if kept else (q or "").strip()
    q_words = [w for w in kept if len(w) >= 2]
    return cleaned, q_words


def _relevance_score(text: str, q_words: list[str]) -> float:
    if not q_words:
        return 1.0
    low = _fold(text)
    hits = sum(1 for w in q_words if w in low)
    return hits / len(q_words)


async def _fetch_wikipedia_and_commons(client: httpx.AsyncClient, q: str, q_words: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    q_folded = _fold(q)

    # 1. Exact/close Wikipedia article lead thumbnails
    try:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": q,
                "gsrlimit": 5,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 360,
            },
            headers={"User-Agent": WIKI_UA},
        )
        if r.status_code == 200:
            pages = (r.json().get("query") or {}).get("pages") or {}
            for _, p in sorted(pages.items(), key=lambda kv: kv[1].get("index", 999)):
                title = p.get("title", "")
                src = (p.get("thumbnail") or {}).get("source")
                if not src or src in seen:
                    continue
                t_low = _fold(title)
                rel = _relevance_score(title, q_words)
                if rel < 0.85:
                    continue
                if any(sec in t_low and sec not in q_folded for sec in _SECONDARY_WIKI):
                    continue
                if t_low == q_folded:
                    score = 2.0
                elif t_low.startswith(q_folded):
                    score = 1.6
                else:
                    score = 1.2 + rel * 0.2
                seen.add(src)
                out.append({"src": src, "title": f"{title} (Wikipedia)", "score": score})
    except Exception as e:
        log.warning("wikipedia image lookup failed: %s", e)

    # 2. Wikimedia Commons file search (File: namespace = 6)
    try:
        r2 = await client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrnamespace": 6,
                "gsrsearch": f"{q} filetype:bitmap",
                "gsrlimit": 18,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 360,
            },
            headers={"User-Agent": WIKI_UA},
        )
        if r2.status_code == 200:
            pages = (r2.json().get("query") or {}).get("pages") or {}
            for _, p in sorted(pages.items(), key=lambda kv: kv[1].get("index", 999)):
                title = p.get("title", "").replace("File:", "")
                t_low = _fold(title)
                infos = p.get("imageinfo") or []
                if not infos:
                    continue
                mime = infos[0].get("mime", "")
                src = infos[0].get("thumburl") or infos[0].get("url")
                if not src or src in seen or not mime.startswith("image/") or "svg" in mime:
                    continue
                if any(bad in t_low for bad in _BAD_FILE_WORDS):
                    continue
                rel = _relevance_score(title, q_words)
                min_rel = 1.0 if len(q_words) == 1 else 0.5
                if rel < min_rel:
                    continue
                seen.add(src)
                out.append({"src": src, "title": title.rsplit(".", 1)[0], "score": rel})
    except Exception as e:
        log.warning("wikimedia commons image lookup failed: %s", e)

    return out


async def _fetch_bing_strict(client: httpx.AsyncClient, q: str, q_words: list[str]) -> list[dict]:
    """Optional fallback ONLY keeping results whose caption strongly matches the query."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        r = await client.get(
            "https://www.bing.com/images/async",
            params={"q": q, "first": 1, "count": 24, "adlt": "off", "mkt": "en-US", "setlang": "en-US"},
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        if r.status_code == 200:
            raw_ms = re.findall(r'm="(\{[^"]+\})"', r.text) or re.findall(r'm=&quot;(\{.+?\})&quot;', r.text)
            for rm in raw_ms:
                try:
                    d = json.loads(html.unescape(rm))
                except Exception:
                    continue
                turl = d.get("turl") or d.get("murl")
                title = d.get("t") or d.get("desc") or ""
                if not turl or turl in seen or "explicit.bing" in turl:
                    continue
                rel = _relevance_score(title, q_words)
                # Strict threshold: reject unrelated Bing anti-bot decoys (e.g. Coffee, MT15, Satyanarayan)
                if rel < (1.0 if len(q_words) <= 2 else 0.7):
                    continue
                seen.add(turl)
                out.append({"src": turl, "title": title, "score": 0.9 * rel})
    except Exception as e:
        log.warning("bing fallback lookup failed: %s", e)
    return out


async def fetch_images(query: str, limit: int = 9) -> tuple[str, list[dict]]:
    cleaned, q_words = clean_query(query)
    if not cleaned:
        return "", []
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        wiki_items = await _fetch_wikipedia_and_commons(client, cleaned, q_words)
        if len(wiki_items) < 4:
            bing_items = await _fetch_bing_strict(client, cleaned, q_words)
            seen = {x["src"] for x in wiki_items}
            for item in bing_items:
                if item["src"] not in seen:
                    seen.add(item["src"])
                    wiki_items.append(item)
    wiki_items.sort(key=lambda x: -x["score"])
    return cleaned, wiki_items[:limit]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def gallery_page(q: str = Query("", description="Search query for images")):
    raw_q = (q or "").strip()
    if not raw_q:
        return HTMLResponse(
            "<!doctype html><html><body style='background:#0f1115;color:#e8eaef;font-family:system-ui;padding:32px'>"
            "<h2>Image Gallery</h2><p>Pass ?q=subject to display verified images.</p></body></html>"
        )
    cleaned, items = await fetch_images(raw_q, limit=9)
    esc_q = html.escape(cleaned or raw_q)
    if items:
        cards = "\n".join(
            f'<div class="card">'
            f'<img src="{html.escape(it["src"])}" alt="{html.escape(it["title"])}" '
            f'referrerpolicy="no-referrer" loading="eager" onerror="this.closest(\'.card\').style.display=\'none\'">'
            f'<div class="cap">{html.escape(it["title"][:70])}</div>'
            f"</div>"
            for it in items
        )
        status = f"Showing {len(items)} verified photos for \"{esc_q}\""
    else:
        cards = (
            f'<div class="empty">No verified images matched "{esc_q}". '
            f"Try phrasing the name or subject slightly differently.</div>"
        )
        status = f"No verified photos found for \"{esc_q}\""

    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc_q} - Images</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 16px 20px;
    background: #0f1115;
    color: #e8eaef;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
    padding-bottom: 10px;
    border-bottom: 1px solid #262b36;
  }}
  h1 {{
    margin: 0;
    font-size: 18px;
    color: #4fd1a5;
  }}
  .meta {{
    font-size: 12.5px;
    color: #8a93a6;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
  }}
  .card {{
    background: #171a21;
    border: 1px solid #262b36;
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }}
  .card img {{
    width: 100%;
    height: 190px;
    object-fit: cover;
    object-position: center 20%;
    background: #0d0f13;
    display: block;
  }}
  .cap {{
    padding: 7px 9px;
    font-size: 11.5px;
    color: #c5cbd6;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .empty {{
    grid-column: 1 / -1;
    background: #171a21;
    border: 1px solid #262b36;
    border-radius: 10px;
    padding: 36px;
    text-align: center;
    color: #8a93a6;
  }}
</style>
</head>
<body>
  <header>
    <h1>{esc_q}</h1>
    <div class="meta">{status}</div>
  </header>
  <div class="grid">
    {cards}
  </div>
</body>
</html>""")

