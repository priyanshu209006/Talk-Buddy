"""A real Chromium the agent drives, plus a page->text compressor built for speech."""
import asyncio
import base64
import logging
import os
import re
import time
from urllib.parse import quote_plus, unquote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import adapters

log = logging.getLogger("browser")
os.environ.setdefault("NODE_OPTIONS", "--max-old-space-size=128")

# Capture playing <video>/<audio> streams inside the page without OS loopback or double audio,
# streaming 24 kHz mono Int16 PCM back to Python via Playwright's exposed __dabSendPcm binding.
AUDIO_HOOK_JS = r"""
(() => {
  if (window.__dabAudioHooked) return;
  window.__dabAudioHooked = true;

  const TARGET_RATE = 24000;
  let ac = null;
  let proc = null;
  let silent = null;
  const hooked = new WeakSet();

  function ensureGraph() {
    if (ac) return ac;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ac = new AC({ sampleRate: TARGET_RATE });
    // 2048 samples at 24000 Hz = ~85.3 ms per frame for low-latency, jitter-free streaming
    proc = ac.createScriptProcessor(2048, 1, 1);
    silent = ac.createGain();
    silent.gain.value = 0;
    proc.onaudioprocess = (ev) => {
      if (typeof window.__dabSendPcm !== 'function') return;
      const inp = ev.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(inp.length);
      for (let i = 0; i < inp.length; i++) {
        let s = inp[i];
        if (s > 1) s = 1;
        else if (s < -1) s = -1;
        i16[i] = s < 0 ? s * 32768 : s * 32767;
      }
      // Stream continuously so client jitter buffer maintains a steady clock
      const bytes = new Uint8Array(i16.buffer);
      let bin = '';
      const chunkSize = 2048;
      for (let i = 0; i < bytes.length; i += chunkSize) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
      }
      try {
        window.__dabSendPcm(btoa(bin), ac.sampleRate);
      } catch (e) {}
    };
    proc.connect(silent);
    silent.connect(ac.destination);
    return ac;
  }

  function hookEl(el) {
    if (!el || hooked.has(el)) return;
    hooked.add(el);
    const attach = () => {
      try {
        if (el.tagName && el.tagName.toLowerCase() === 'video') {
          el.muted = false;
        }
        const ctx = ensureGraph();
        if (!ctx) return;
        if (ctx.state === 'suspended') ctx.resume().catch(() => {});
        const src = ctx.createMediaElementSource(el);
        const gain = ctx.createGain();
        const syncVol = () => { gain.gain.value = el.muted ? 0 : (el.volume ?? 1); };
        syncVol();
        el.addEventListener('volumechange', syncVol);
        src.connect(gain);
        gain.connect(proc);
      } catch (e) {}
    };
    if (!el.paused) attach();
    el.addEventListener('play', attach, { once: true });
    el.addEventListener('playing', attach, { once: true });
  }

  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function(...args) {
    hookEl(this);
    return origPlay.apply(this, args);
  };

  function scan() {
    document.querySelectorAll('video, audio').forEach(hookEl);
  }
  document.addEventListener('playing', e => hookEl(e.target), true);
  if (document.documentElement) {
    scan();
    new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  } else {
    window.addEventListener('DOMContentLoaded', scan, { once: true });
  }

  // Autonomous ad-skip and cookie-consent auto-dismissal
  function autoSkipAds() {
    const sels = [
      '.ytp-skip-ad-button', '.ytp-ad-skip-button', '.ytp-ad-skip-button-modern',
      '.videoAdUiSkipButton', 'button.ytp-ad-skip-button-slot',
      'button[aria-label="Skip ad"]', 'button[aria-label="Skip ads"]'
    ];
    for (const sel of sels) {
      const btn = document.querySelector(sel);
      if (btn && btn.offsetParent !== null) {
        try { btn.click(); } catch (e) {}
        break;
      }
    }
  }
  setInterval(autoSkipAds, 1200);
})();
"""

# Walk the DOM, tag every visible interactive element with a short id the LLM can
# refer to, and return a compact text rendering of what a sighted user would see.
STEALTH_JS = r"""
(() => {
  // Preserve native toString representation for wrapped functions
  const origToString = Function.prototype.toString;
  const nativeMap = new WeakMap();
  function markNative(fn, name) {
    nativeMap.set(fn, `function ${name}() { [native code] }`);
  }
  Function.prototype.toString = function() {
    if (nativeMap.has(this)) return nativeMap.get(this);
    return origToString.apply(this, arguments);
  };
  markNative(Function.prototype.toString, 'toString');

  // Plugins & MimeTypes: realistic PluginArray & MimeTypeArray with native prototype inheritance
  try {
    const pluginData = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
    ];
    const mimeData = [
      { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
      { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
    ];
    const p = Object.create(PluginArray.prototype);
    const m = Object.create(MimeTypeArray.prototype);
    const plugins = [];
    const mimes = [];

    mimeData.forEach(md => {
      const mime = Object.create(MimeType.prototype);
      for (const [k, v] of Object.entries(md)) {
        Object.defineProperty(mime, k, { value: v, enumerable: true });
      }
      mimes.push(mime);
      Object.defineProperty(m, md.type, { value: mime, enumerable: true });
    });
    Object.defineProperty(m, 'length', { value: mimes.length });
    m.item = function item(index) { return mimes[index] || null; };
    m.namedItem = function namedItem(name) { return m[name] || null; };
    markNative(m.item, 'item');
    markNative(m.namedItem, 'namedItem');

    pluginData.forEach((pd, idx) => {
      const plug = Object.create(Plugin.prototype);
      for (const [k, v] of Object.entries(pd)) {
        Object.defineProperty(plug, k, { value: v, enumerable: true });
      }
      Object.defineProperty(plug, 0, { value: mimes[0], enumerable: true });
      Object.defineProperty(plug, 1, { value: mimes[1], enumerable: true });
      Object.defineProperty(plug, 'length', { value: 2 });
      plug.item = function item(i) { return i === 0 ? mimes[0] : (i === 1 ? mimes[1] : null); };
      plug.namedItem = function namedItem(n) { return m[n] || null; };
      markNative(plug.item, 'item');
      markNative(plug.namedItem, 'namedItem');
      plugins.push(plug);
      Object.defineProperty(p, idx, { value: plug, enumerable: true });
      Object.defineProperty(p, pd.name, { value: plug, enumerable: true });
    });
    Object.defineProperty(p, 'length', { value: plugins.length });
    p.item = function item(index) { return plugins[index] || null; };
    p.namedItem = function namedItem(name) { return p[name] || null; };
    markNative(p.item, 'item');
    markNative(p.namedItem, 'namedItem');
    p[Symbol.iterator] = function*() { for (let x of plugins) yield x; };
    m[Symbol.iterator] = function*() { for (let x of mimes) yield x; };

    const navProto = Object.getPrototypeOf(navigator);
    Object.defineProperty(navProto, 'plugins', { get: () => p, enumerable: true, configurable: true });
    Object.defineProperty(navProto, 'mimeTypes', { get: () => m, enumerable: true, configurable: true });
  } catch (e) {}

  // window.chrome with runtime, app, csi, loadTimes
  try {
    const csi = function csi() {};
    const loadTimes = function loadTimes() {};
    markNative(csi, 'csi');
    markNative(loadTimes, 'loadTimes');
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
      loadTimes,
      csi,
    };
  } catch (e) {}

  // Notification.permission: match permissions.query prompt state so HEADCHR_PERMISSIONS check passes
  try {
    if (typeof Notification !== 'undefined') {
      Object.defineProperty(Notification, 'permission', {
        get: () => 'default',
        configurable: true,
      });
    }
  } catch (e) {}

  // Hardware concurrency, memory, languages, and platform on prototype
  try {
    const navProto = Object.getPrototypeOf(navigator);
    if (!navigator.deviceMemory) {
      Object.defineProperty(navProto, 'deviceMemory', { get: () => 8, enumerable: true, configurable: true });
    }
    Object.defineProperty(navProto, 'languages', { get: () => ['en-IN', 'en-US', 'en', 'hi'], enumerable: true, configurable: true });
    Object.defineProperty(navProto, 'platform', { get: () => 'Win32', enumerable: true, configurable: true });
  } catch (e) {}

  // WebGL vendor and renderer unmasking with native getParameter wrapper
  try {
    const spoofGL = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function getParameter(param) {
        if (param === 37445) return 'Google Inc. (Intel)';
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return orig.apply(this, arguments);
      };
      markNative(proto.getParameter, 'getParameter');
    };
    if (typeof WebGLRenderingContext !== 'undefined') spoofGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') spoofGL(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // Screen outer dimensions
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
        js_mb = os.environ.get("JS_HEAP_MB", "128" if os.environ.get("RENDER") else "256")
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
            f"--js-flags=--max-old-space-size={js_mb}",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
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
    VIEWPORT_W = 1280
    VIEWPORT_H = 800

    def __init__(self):
        self._pw = None
        self._browser = None
        self.page = None
        self.corpus = ""          # text of the latest page snapshot (for site-voice grounding)
        self._on_frame = None
        self._on_audio = None
        self._cdp = None
        self._sc_task = None
        self._sc_pending = None
        self._sc_last_at = 0.0
        self._sc_enabled = False

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

    async def start(self, on_frame=None, on_audio=None):
        # One browser process for the whole server (launched at startup, ~3 s saved per call);
        # each call gets its own isolated context.
        self._on_frame = on_frame
        self._on_audio = on_audio
        self._browser = await shared_browser()
        user_agent = os.environ.get(
            "BROWSER_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        proxy_url = os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        proxy = {"server": proxy_url} if proxy_url else None

        self._ctx = await self._browser.new_context(
            viewport={"width": self.VIEWPORT_W, "height": self.VIEWPORT_H},
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
        # Suppress heavy invasive telemetry without triggering net::ERR_FAILED errors or blocking fonts
        async def block_trackers(route):
            u = route.request.url.lower()
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
                await route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=b"/* tracker filtered */",
                )
            else:
                await route.continue_()

        await self._ctx.route("**/*", block_trackers)
        await self._ctx.add_init_script(STEALTH_JS)
        if self._on_audio:
            await self._ctx.add_init_script(AUDIO_HOOK_JS)
            async def _on_pcm(b64: str, rate: int = 24000):
                try:
                    await self._on_audio(int(rate or 24000), b64)
                except Exception:
                    pass
            await self._ctx.expose_function("__dabSendPcm", _on_pcm)
        self.page = await self._ctx.new_page()
        await self._attach_screencast()

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

    async def _stop_screencast(self):
        cdp = self._cdp
        self._cdp = None
        self._sc_enabled = False
        self._sc_pending = None
        if cdp:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass

    async def _attach_screencast(self):
        """Stream live viewport JPEG frames via CDP so animations, scrolling, and video play in real time."""
        await self._stop_screencast()
        if not self._on_frame or not self._ctx or not self.page or self.page.is_closed():
            return
        if os.environ.get("LIVE_SCREEN", "1") == "0":
            return
        on_render = bool(os.environ.get("RENDER"))
        quality = int(os.environ.get("SCREENCAST_QUALITY", "52" if on_render else "60"))
        max_w = int(os.environ.get("SCREENCAST_WIDTH", "840" if on_render else "1024"))
        max_h = int(os.environ.get("SCREENCAST_HEIGHT", "525" if on_render else "640"))
        every_n = int(os.environ.get("SCREENCAST_EVERY_NTH", "3" if on_render else "2"))
        min_dt = 1.0 / max(1.0, float(os.environ.get("SCREENCAST_FPS", "8" if on_render else "12")))
        try:
            cdp = await self._ctx.new_cdp_session(self.page)
            self._cdp = cdp
            self._sc_enabled = True
            self._sc_last_at = 0.0

            async def _flush_frames():
                try:
                    while self._sc_enabled and self._sc_pending is not None:
                        b64 = self._sc_pending
                        self._sc_pending = None
                        now = time.monotonic()
                        wait = min_dt - (now - self._sc_last_at)
                        if wait > 0.005:
                            await asyncio.sleep(wait)
                        self._sc_last_at = time.monotonic()
                        raw_url = self.page.url if (self.page and not self.page.is_closed()) else ""
                        url = "Ready: Dial-a-Browser" if (not raw_url or raw_url == "about:blank" or raw_url.startswith("data:text/html")) else raw_url
                        await self._on_frame(url, b64)
                except Exception:
                    pass
                finally:
                    self._sc_task = None

            async def _ack(sid):
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
                except Exception:
                    pass

            def _on_sc(ev):
                asyncio.create_task(_ack(ev["sessionId"]))
                if not self._sc_enabled:
                    return
                self._sc_pending = ev["data"]
                if self._sc_task is None or self._sc_task.done():
                    self._sc_task = asyncio.create_task(_flush_frames())

            cdp.on("Page.screencastFrame", _on_sc)
            await cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": max_w,
                "maxHeight": max_h,
                "everyNthFrame": every_n,
            })
        except Exception as e:
            log.warning("screencast start failed: %s", e)

    async def interact_click(self, nx: float, ny: float):
        if not self.alive():
            return
        x = max(0.0, min(float(self.VIEWPORT_W - 1), float(nx) * self.VIEWPORT_W))
        y = max(0.0, min(float(self.VIEWPORT_H - 1), float(ny) * self.VIEWPORT_H))
        try:
            await self.page.mouse.click(x, y)
        except Exception as e:
            log.debug("interact_click failed: %s", e)

    async def interact_wheel(self, dy: float):
        if not self.alive():
            return
        try:
            await self.page.mouse.wheel(0, max(-1200.0, min(1200.0, float(dy))))
        except Exception as e:
            log.debug("interact_wheel failed: %s", e)

    async def restart(self, url: str | None = None):
        """Relaunch after the browser died, and reopen where we were."""
        log.warning("browser died; relaunching")
        try:
            await self.close()
        except Exception:
            pass
        self._browser = self.page = None
        await self.start(on_frame=self._on_frame, on_audio=self._on_audio)
        if url and url.startswith("http"):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await self._settle()
            except Exception:
                pass

    async def close(self):
        """Close this call's context only; the shared browser stays warm for the next call."""
        await self._stop_screencast()
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
            s = await asyncio.wait_for(self.page.evaluate(SNAPSHOT_JS), timeout=6.0)
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
        await self._attach_screencast()

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
        for attempt in range(2):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                break
            except PWTimeout:
                break
            except Exception as e:
                if attempt == 0 and any(err in str(e) for err in ("ERR_NETWORK_CHANGED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED")):
                    await asyncio.sleep(0.5)
                    continue
                raise
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
            await asyncio.sleep(0.15)
            now = time.monotonic()
            fresh = sum(1 for t0 in self._inflight.values() if now - t0 < 2.0)
            try:
                rem = max(0.5, max_s - (time.monotonic() - start))
                n = await asyncio.wait_for(
                    self.page.evaluate(
                        "() => (document.body ? document.body.innerText.length : 0) + "
                        "document.querySelectorAll('a,button,input,select').length * 50"
                    ),
                    timeout=min(2.5, rem),
                )
            except asyncio.TimeoutError:
                return
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
            await self.page.wait_for_timeout(150)
            await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(250)
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

    async def forward(self) -> str:
        try:
            await self.page.go_forward(wait_until="domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        return await self.snapshot()

    async def reload(self) -> str:
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=15000)
        except PWTimeout:
            pass
        await self._settle()
        return await self.snapshot()

    async def skip_ad(self) -> bool:
        if not self.alive():
            return False
        sels = [
            ".ytp-skip-ad-button", ".ytp-ad-skip-button", ".ytp-ad-skip-button-modern",
            ".videoAdUiSkipButton", "button.ytp-ad-skip-button-slot",
            "button[aria-label='Skip ad']", "button[aria-label='Skip ads']",
            "button:has-text('Skip Ad')", "button:has-text('Skip ad')", "button:has-text('Skip')",
        ]
        for s in sels:
            try:
                loc = self.page.locator(s).first
                if await loc.is_visible(timeout=250):
                    await loc.click(timeout=800)
                    log.info("Autonomously skipped ad: %s", s)
                    return True
            except Exception:
                pass
        return False
