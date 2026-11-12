"""A real Chromium the agent drives, plus a page->text compressor built for speech."""
import base64
import logging
import os
import re
import time
from urllib.parse import quote_plus, unquote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import adapters

log = logging.getLogger("browser")
os.environ.setdefault("NODE_OPTIONS", "--max-old-space-size=64")

# Walk the DOM, tag every visible interactive element with a short id the LLM can
# refer to, and return a compact text rendering of what a sighted user would see.
STEALTH_JS = r"""
(() => {
  try {
    delete Object.getPrototypeOf(navigator).webdriver;
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  try {
    window.chrome = {
      app: {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      },
      runtime: {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
      },
      loadTimes: function() {},
      csi: function() {},
    };
  } catch (e) {}

  try {
    const fakePlugins = [
      { name: 'PDF Viewer', filename: 'pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ];
    Object.defineProperty(navigator, 'plugins', {
      get: () => fakePlugins,
      configurable: true,
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: fakePlugins[0] }
      ],
      configurable: true,
    });
  } catch (e) {}

  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['en-IN', 'en-GB', 'en-US', 'en', 'hi'],
      configurable: true,
    });
    Object.defineProperty(navigator, 'platform', {
      get: () => 'Win32',
      configurable: true,
    });
  } catch (e) {}

  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(params)
      );
    }
  } catch (e) {}

  try {
    const spoofGL = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Intel)';
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return orig.apply(this, [param]);
      };
    };
    if (typeof WebGLRenderingContext !== 'undefined') spoofGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') spoofGL(WebGL2RenderingContext.prototype);
  } catch (e) {}

  try {
    if (window.outerWidth === 0) {
      Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
    }
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
    }
  } catch (e) {}
})();
"""

SNAPSHOT_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-dab]').forEach(e => e.removeAttribute('data-dab'));
  let n = 0;
  const elements = [];
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    const direct = clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.title || '');
    if (direct) return direct;
    // e.g. id="facet-beneficiaryState-input" -> "beneficiary state filter"
    if (el.id) return el.id.replace(/^facet-/, '').replace(/-input$/, ' filter')
                     .replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[-_]/g, ' ').toLowerCase();
    return '';
  };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [onclick]';
  document.querySelectorAll(sel).forEach(el => {
    if (!vis(el)) return;
    const id = 'e' + (++n);
    el.setAttribute('data-dab', id);
    const tag = el.tagName.toLowerCase();
    let desc;
    if (tag === 'select') {
      const opts = [...el.options].slice(0, 25).map(o => clean(o.text)).join(' | ');
      desc = `select "${labelFor(el)}" selected="${clean(el.options[el.selectedIndex]?.text)}" options: ${opts}`;
    } else if (tag === 'input' || tag === 'textarea') {
      const type = el.type || 'text';
      if (type === 'submit' || type === 'button') desc = `button "${clean(el.value)}"`;
      else if (type === 'checkbox' || type === 'radio') {
        const lab = labelFor(el);
        if (!lab) { el.removeAttribute('data-dab'); n--; return; }   // unlabeled filter noise
        desc = `${type} "${lab}" checked=${el.checked}`;
      }
      else desc = `input[${type}] "${labelFor(el) || (el.closest('form,[role=search],header') ? 'search box' : '')}" value="${clean(el.value)}"`;
    } else if (tag === 'a') {
      desc = `link "${clean(el.innerText) || el.getAttribute('aria-label') || el.href}"`;
    } else {
      const t = clean(el.innerText) || el.getAttribute('aria-label') || el.title || '';
      if (!t) { el.removeAttribute('data-dab'); n--; return; }        // icon-only, unlabeled
      desc = `button "${t}"`;
    }
    elements.push(`[${id}] ${desc}`);
  });

  // Visible reading-order text: headings, paragraphs, table rows, list items, alerts.
  const lines = [];
  const seen = new Set();
  document.querySelectorAll('h1,h2,h3,h4,p,li,tr,[role=alert],.alert,.notice,label,dt,dd,caption').forEach(el => {
    if (!vis(el)) return;
    if (el.closest('tr') && el.tagName !== 'TR') return;
    let t;
    if (el.tagName === 'TR') {
      t = [...el.children].map(c => clean(c.innerText)).filter(Boolean).join(' | ');
    } else {
      t = clean(el.innerText);
    }
    if (!t || t.length < 2 || seen.has(t)) return;
    seen.add(t);
    const tag = el.tagName.toLowerCase();
    lines.push((/^h\d$/.test(tag) ? '# ' : '') + t.slice(0, 300));
  });
  return { url: location.href, title: document.title, lines, elements,
           fulltext: clean(document.body ? document.body.innerText : '') };
}
"""

MAX_CHARS = 5500

_SHARED: dict = {"pw": None, "browser": None}
_SHARED_LOCK = None


async def shared_browser():
    """Launch (once) or return the warm browser shared by all calls."""
    global _SHARED_LOCK
    import asyncio
    if _SHARED_LOCK is None:
        _SHARED_LOCK = asyncio.Lock()
    async with _SHARED_LOCK:
        b = _SHARED["browser"]
        if b and b.is_connected():
            return b
        if _SHARED["pw"] is None:
            _SHARED["pw"] = await async_playwright().start()
        pw = _SHARED["pw"]
        # Headless is the stable default. HEADLESS=0 runs a real window parked off-screen,
        # which IRCTC accepts, but on this machine Edge sometimes closed that window by itself.
        headless = os.environ.get("HEADLESS", "1") != "0"
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,Translate,BackForwardCache",
            "--disable-infobars",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-accelerated-2d-canvas",
            "--renderer-process-limit=1",
            "--no-zygote",
            "--js-flags=--max-old-space-size=64",
            "--disk-cache-size=1",
            "--media-cache-size=1",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--mute-audio",
        ]
        if not headless:
            args.extend(["--window-position=-32000,-32000", "--window-size=1280,800"])
        channel = os.environ.get("BROWSER_CHANNEL") or None
        ignore_default_args = ["--enable-automation"]
        try:
            b = await pw.chromium.launch(
                headless=headless,
                channel=channel,
                args=args,
                ignore_default_args=ignore_default_args,
            )
        except Exception as e:
            log.warning("Chromium launch failed (%s); trying fallback args", str(e).splitlines()[0])
            b = await pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        _SHARED["browser"] = b
        return b


class Browser:
    def __init__(self):
        self._pw = None
        self._browser = None
        self.page = None
        self.corpus = ""          # text of the latest page snapshot (for site-voice grounding)

    def grounded(self, text: str) -> tuple[bool, float]:
        """Is `text` actually on the current page? Share of its words/numbers found there."""
        norm = lambda x: re.findall(r"[a-z0-9₹]+(?:[.:,/-][a-z0-9]+)*", x.lower().replace(",", ""))
        words = [w for w in norm(text) if len(w) > 1 or w.isdigit()]
        if not words:
            return True, 1.0
        page = set(norm(self.corpus))
        page_joined = self.corpus.lower().replace(",", "")
        hits = sum(1 for w in words if w in page or w in page_joined)
        share = hits / len(words)
        return share >= 0.8, share

    async def start(self):
        # One browser process for the whole server (launched at startup, ~3 s saved per call);
        # each call gets its own isolated context.
        self._browser = await shared_browser()
        user_agent = os.environ.get(
            "BROWSER_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        proxy_url = os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        proxy = {"server": proxy_url} if proxy_url else None

        self._ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=user_agent,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            color_scheme="light",
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            java_script_enabled=True,
            proxy=proxy,
            extra_http_headers={
                "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8,hi;q=0.7",
            },
        )
        # Block telemetry, ads, web fonts, and media streams to stay under Render Free's 512 MB cap
        async def block_trackers(route):
            req = route.request
            if req.resource_type in ("font", "media"):
                await route.abort()
                return
            u = req.url.lower()
            if any(t in u for t in (
                "google-analytics.com",
                "googlesyndication.com",
                "doubleclick.net",
                "px.ads.linkedin.com",
                "connect.facebook.net",
                "adservice.google",
                "hotjar.com",
                "clarity.ms",
            )):
                await route.abort()
            else:
                await route.continue_()

        await self._ctx.route("**/*", block_trackers)
        await self._ctx.add_init_script(STEALTH_JS)
        self.page = await self._ctx.new_page()

    async def init_page(self):
        try:
            await self.page.set_content("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {
    margin: 0;
    padding: 30px;
    background: #0f1115;
    color: #e8eaef;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 85vh;
    text-align: center;
    box-sizing: border-box;
  }
  .card {
    background: #171a21;
    border: 1px solid #262b36;
    border-radius: 16px;
    padding: 36px 44px;
    max-width: 600px;
    box-shadow: 0 12px 36px rgba(0,0,0,0.5);
  }
  .badge {
    display: inline-block;
    background: rgba(79, 209, 165, 0.15);
    color: #4fd1a5;
    border: 1px solid rgba(79, 209, 165, 0.3);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 16px;
  }
  h1 {
    color: #ffffff;
    font-size: 26px;
    margin: 0 0 10px;
  }
  p {
    color: #8a93a6;
    font-size: 15px;
    line-height: 1.6;
    margin: 0;
  }
</style>
</head>
<body>
  <div class="card">
    <div class="badge">● Live Browser Connected</div>
    <h1>Dial-a-Browser</h1>
    <p>Ready to browse the web for you on this call. Ask to search for images of any person, look up live information, check trains, or visit any website!</p>
  </div>
</body>
</html>""")
        except Exception as e:
            log.warning("init_page failed: %s", e)

    def alive(self) -> bool:
        return bool(self._browser and self._browser.is_connected() and self.page and not self.page.is_closed())

    async def restart(self, url: str | None = None):
        """Relaunch after the browser died, and reopen where we were."""
        log.warning("browser died; relaunching")
        try:
            await self.close()
        except Exception:
            pass
        self._browser = self.page = None
        await self.start()
        if url and url.startswith("http"):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await self._settle()
            except Exception:
                pass

    async def close(self):
        """Close this call's context only; the shared browser stays warm for the next call."""
        ctx = getattr(self, "_ctx", None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    async def snapshot(self) -> str:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except PWTimeout:
            pass
        try:
            s = await self.page.evaluate(SNAPSHOT_JS)
        except Exception as e:
            return f"(could not read page: {e})"
        # Separate budgets: on many real sites (e.g. NTES) the data lives inside buttons,
        # so the element list must not be starved by long page text.
        def clip(lines, budget, per_line):
            out, used = [], 0
            for ln in lines:
                ln = ln if len(ln) <= per_line else ln[:per_line] + "…"
                if used + len(ln) > budget:
                    out.append("...(more omitted)")
                    break
                out.append(ln)
                used += len(ln) + 1
            return "\n".join(out)
        self.last_elements = s["elements"]        # untrimmed, for site readers / fast lane
        try:
            summary = adapters.summarize(s["url"], s["elements"])
        except Exception as e:
            log.warning("adapter failed: %s", e)
            summary = None
        # Everything the site voice is allowed to quote: the page's own text, its
        # controls, and any summary computed from them.
        self.corpus = " ".join([s.get("fulltext", ""), " ".join(s["elements"]), summary or ""])
        return (
            f"URL: {s['url']}\nTITLE: {s['title']}\n\n"
            + (summary + "\n\n" if summary else "")
            + "PAGE TEXT:\n"
            + clip(s["lines"], int(MAX_CHARS * 0.4), 240)
            + "\n\nINTERACTIVE ELEMENTS:\n"
            # Form controls first: search boxes often sit last in the DOM and would be cut.
            + clip(sorted(s["elements"], key=lambda e: 0 if (" input[" in e or " select " in e) else 1),
                   int(MAX_CHARS * 0.6), 200)
        )

    async def screenshot_b64(self) -> str | None:
        try:
            q = int(os.environ.get("SCREEN_QUALITY", "85"))
            png = await self.page.screenshot(type="jpeg", quality=q, timeout=8000)
            return base64.b64encode(png).decode()
        except Exception:
            return None

    def _el(self, element_id: str):
        return self.page.locator(f'[data-dab="{element_id}"]').first

    @staticmethod
    def _gallery_url(query: str) -> str:
        port = os.environ.get("PORT", "8000")
        return f"http://127.0.0.1:{port}/gallery?q={quote_plus(query)}"

    @classmethod
    def _rewrite_image_url(cls, url: str) -> str:
        low = url.lower()
        port = os.environ.get("PORT", "8000")
        if re.match(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/gallery\b", low):
            return re.sub(
                r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?",
                f"http://127.0.0.1:{port}",
                url,
                count=1,
                flags=re.IGNORECASE,
            )
        is_img_search = (
            "bing.com/images" in low
            or "images.search.yahoo.com" in low
            or "search.brave.com/images" in low
            or ("google.com/search" in low and ("tbm=isch" in low or "udm=2" in low))
            or ("duckduckgo.com" in low and ("iax=images" in low or "ia=images" in low))
        )
        if is_img_search:
            q_match = re.search(r"[?&](?:q|p)=([^&#]+)", url)
            if q_match:
                q = unquote_plus(q_match.group(1)).strip()
                if q:
                    rewritten = cls._gallery_url(q)
                    log.info("Rerouted image search to local gallery: %s", rewritten)
                    return rewritten
        return url

    async def _recycle_page(self):
        """Open a fresh tab and close the old one so renderer heap doesn't climb across navigations."""
        if not self._ctx or not self.page:
            return
        if not (self.page.url or "").startswith(("http://", "https://")):
            return
        old = self.page
        try:
            self.page = await self._ctx.new_page()
            await old.close()
        except Exception:
            if self.page is None or self.page.is_closed():
                self.page = await self._ctx.new_page()

    async def _wait_images(self, timeout_ms: int = 2000):
        """Wait briefly for visible <img> tags on gallery pages to finish decoding."""
        if not self.page:
            return
        try:
            await self.page.evaluate(
                """(ms) => new Promise((resolve) => {
                    const deadline = Date.now() + ms;
                    const check = () => {
                        const imgs = [...document.querySelectorAll('img')];
                        if (imgs.every(i => i.complete) || Date.now() >= deadline) return resolve();
                        setTimeout(check, 80);
                    };
                    check();
                })""",
                timeout_ms,
            )
        except Exception:
            pass

    async def _goto(self, url: str, timeout: int = 25000):
        if not url.startswith(("http://", "https://")):
            scheme = "http://" if url.startswith(("localhost", "127.0.0.1")) else "https://"
            url = scheme + url
        url = self._rewrite_image_url(url)
        await self._recycle_page()
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except PWTimeout:
            pass
        if "google.com/sorry" in self.page.url or "sorry/index" in self.page.url:
            q_match = re.search(r"[?&]q=([^&#]+)", url)
            if q_match:
                q = unquote_plus(q_match.group(1)).strip()
                is_img = "isch" in url or "tbm=isch" in url or "udm=2" in url
                fallback = self._gallery_url(q) if is_img else f"https://www.bing.com/search?q={quote_plus(q)}"
                log.info("Google CAPTCHA encountered; falling back: %s", fallback)
                try:
                    await self.page.goto(fallback, wait_until="domcontentloaded", timeout=timeout)
                except Exception:
                    pass
        if "/gallery" in (self.page.url or ""):
            await self._wait_images(2000)

    async def open(self, url: str) -> str:
        await self._goto(url, timeout=25000)
        try:
            title = (await self.page.title()).lower()
            if any(w in title for w in ("just a moment", "security check", "checking your browser", "attention required")):
                await self.page.wait_for_timeout(2500)
            # Dismiss search engine consent banners (Google, Bing, Yahoo) if present
            for sel in ("#L2AGLb", "button:has-text('Accept all')", "button:has-text('I agree')", "#bnp_btn_accept", "button[name='agree']", "button:has-text('Agree')"):
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=250):
                    await loc.click(timeout=800)
                    await self.page.wait_for_timeout(300)
                    break
        except Exception:
            pass
        await self._settle()
        return await self.snapshot()

    def _track_network(self):
        """Count the page's own fresh requests. 'networkidle' never fires on sites that poll in the
        background (NTES), so we ignore requests older than ~2 s instead of waiting them out."""
        if getattr(self, "_tracked", None) is self.page:
            return
        self._tracked, self._inflight = self.page, {}
        kinds = ("document", "xhr", "fetch", "script")
        self.page.on("request", lambda r: self._inflight.__setitem__(r, time.monotonic())
                     if r.resource_type in kinds else None)
        done = lambda r: self._inflight.pop(r, None)
        self.page.on("requestfinished", done)
        self.page.on("requestfailed", done)

    async def _settle(self, max_s: float = 4.0):
        """Wait until the page has finished reacting: no fresh requests in flight and the rendered
        content unchanged for ~300 ms. Typically 0.3-1 s instead of fixed sleeps."""
        self._track_network()
        start = time.monotonic()
        last, stable = -1, 0
        while time.monotonic() - start < max_s:
            await self.page.wait_for_timeout(150)
            now = time.monotonic()
            fresh = sum(1 for t0 in self._inflight.values() if now - t0 < 2.0)
            try:
                n = await self.page.evaluate(
                    "() => (document.body ? document.body.innerText.length : 0) + "
                    "document.querySelectorAll('a,button,input,select').length * 50")
            except Exception:                    # mid-navigation: wait for the new document
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                last, stable = -1, 0
                continue
            stable = stable + 1 if (n == last and n > 200 and fresh == 0) else 0
            if stable >= 2:
                return
            last = n

    async def click(self, element_id: str) -> str:
        el = self._el(element_id)
        self._track_network()
        await el.click(timeout=6000)
        await self._settle()          # handles both navigations and in-page (JS) updates
        return await self.snapshot()

    async def fill(self, element_id: str, text: str) -> str:
        el = self._el(element_id)
        await el.fill(text, timeout=6000)
        return await self.snapshot()

    async def select(self, element_id: str, option: str) -> str:
        el = self._el(element_id)
        try:
            await el.select_option(label=option, timeout=4000)
        except Exception:
            await el.select_option(value=option, timeout=4000)
        return await self.snapshot()

    async def fill_form(self, fields: list[dict], submit_id: str | None) -> str:
        problems = []
        for f in fields:
            el = self._el(f["element_id"])
            value = str(f.get("value", ""))
            try:
                tag = await el.evaluate("e => e.tagName.toLowerCase()", timeout=4000)
                if tag == "select":
                    try:
                        await el.select_option(label=value, timeout=3000)
                    except Exception:
                        # tolerate "lower" vs "Lower", partial labels, or option values
                        opts = await el.evaluate("e => [...e.options].map(o => o.text)")
                        match = next((o for o in opts if value.lower() in o.lower()), None)
                        if not match:
                            raise ValueError(f"no option like '{value}' (options: {opts})")
                        await el.select_option(label=match, timeout=3000)
                else:
                    await el.fill(value, timeout=4000)
            except Exception as e:
                problems.append(f"{f['element_id']}: {str(e).splitlines()[0]}")
        if problems:
            return "ERROR: some fields failed, nothing submitted:\n" + "\n".join(problems) + "\n\n" + await self.snapshot()
        if submit_id:
            return await self.click(submit_id)
        return await self.snapshot()

    async def type_and_submit(self, element_id: str, text: str, pick_suggestion: bool = False) -> str:
        """Type like a human (fires autocomplete), then press Enter. Needed by search boxes
        such as NTES 'Train No./Name' that ignore programmatic fills."""
        el = self._el(element_id)
        try:
            await el.click(timeout=3000)
        except PWTimeout:
            # Custom dropdowns (react-select on myScheme) put a placeholder div over the
            # input that swallows clicks; focusing the input directly works.
            await el.focus(timeout=3000)
        try:
            await el.fill("", timeout=3000)
        except Exception:
            pass
        await self.page.keyboard.type(text, delay=35)
        await self.page.wait_for_timeout(600 if pick_suggestion else 350)   # let autocomplete appear
        # Press on the page, not the locator: some sites (NTES) swap the input out
        # as soon as results render, and a locator press would wait 30 s for it.
        if pick_suggestion:
            await self.page.keyboard.press("ArrowDown")
        await self.page.keyboard.press("Enter")
        await self._settle()
        return await self.snapshot()

    # ------------------------------------------------------------------ multi-step
    _KINDS = {
        "type": ("input[", "textarea"),
        "select": ("select ", "input["),            # custom dropdowns are often inputs
        "click": ("button ", "link ", "checkbox ", "radio "),
    }

    def _find(self, target: str, do: str) -> str | None:
        """Element id whose label/text best matches `target` (e.g. "Search", "Train No./Name")."""
        import difflib
        t = re.sub(r"\s+", " ", (target or "").lower()).strip()
        best, best_score = None, 0.0
        for ln in getattr(self, "last_elements", []) or []:
            m = re.match(r'\[(e\d+)\] (\S+) "(.*?)"', ln)
            if not m:
                continue
            eid, kind, label = m[1], m[2] + " ", m[3].lower()
            if not any(kind.startswith(k) or ln.split("] ", 1)[1].startswith(k) for k in self._KINDS[do]):
                continue
            if not t:                                # "type" with no target: first text box
                return eid if do == "type" else None
            if label == t:
                score = 1.0
            elif t in label or (label and label in t):
                score = 0.85 - 0.002 * abs(len(label) - len(t))
            else:
                score = difflib.SequenceMatcher(None, t, label).ratio() * 0.8
            if score > best_score:
                best, best_score = eid, score
        return best if best_score >= 0.5 else None

    async def run_steps(self, steps: list[dict]) -> str:
        """Run a whole flow without an LLM round trip per step. Targets are visible labels/text.
        Stops at the first failure and reports it with the page as it is."""
        done = []
        for i, st in enumerate(steps, 1):
            do = (st.get("do") or "").lower()
            try:
                if do == "open":
                    url = st["url"]
                    await self._goto(url, timeout=20000)
                    try:
                        title = (await self.page.title()).lower()
                        if any(w in title for w in ("just a moment", "security check", "checking your browser", "attention required")):
                            await self.page.wait_for_timeout(2500)
                        # Dismiss search engine consent banners (Google, Bing, Yahoo) if present
                        for sel in ("#L2AGLb", "button:has-text('Accept all')", "button:has-text('I agree')", "#bnp_btn_accept", "button[name='agree']", "button:has-text('Agree')"):
                            loc = self.page.locator(sel).first
                            if await loc.is_visible(timeout=250):
                                await loc.click(timeout=800)
                                await self.page.wait_for_timeout(300)
                                break
                    except Exception:
                        pass
                    await self._settle()
                    done.append(f"{i}. open {url} ✓")
                elif do in ("type", "click", "select"):
                    await self.snapshot()                  # tag elements on the current page
                    eid = self._find(st.get("target", ""), do)
                    if not eid:
                        await self._settle()               # maybe still rendering; one retry
                        await self.snapshot()
                        eid = self._find(st.get("target", ""), do)
                    if not eid:
                        here = [m[1] for m in (re.match(r'\[e\d+\] (?:link|button|input\[\w+\]|select) "(.+?)"', ln)
                                               for ln in (self.last_elements or [])) if m][:20]
                        raise ValueError(
                            f"nothing on the page matches '{st.get('target', '')}'. The page has: "
                            + "; ".join(here)
                            + ". If the field you need lives behind a tab/link, add a click step on it first.")
                    if do == "type":
                        if st.get("enter", True):
                            await self.type_and_submit(eid, str(st.get("text", "")), bool(st.get("pick")))
                        else:
                            await self._el(eid).fill(str(st.get("text", "")), timeout=4000)
                    elif do == "click":
                        await self.click(eid)
                    else:
                        r = await self.fill_form([{"element_id": eid, "value": str(st.get("option", ""))}], None)
                        if r.startswith("ERROR"):
                            raise ValueError(r.splitlines()[1] if len(r.splitlines()) > 1 else r)
                    done.append(f"{i}. {do} '{st.get('target', '')}' ✓")
                elif do == "wait_for":
                    await self.page.wait_for_function(
                        "t => document.body.innerText.toLowerCase().includes(t)",
                        arg=str(st.get("text", "")).lower(), timeout=8000)
                    done.append(f"{i}. wait_for '{st.get('text')}' ✓")
                else:
                    raise ValueError(f"unknown step '{do}'")
            except Exception as e:
                done.append(f"{i}. {do} '{st.get('target', st.get('url', ''))}' ✗ {str(e).splitlines()[0]}")
                break
        snap = await self.snapshot()
        return snap + "\n\nSTEPS:\n" + "\n".join(done)

    async def back(self) -> str:
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        return await self.snapshot()
