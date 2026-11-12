---
title: Talk Buddy (Dial-a-Browser)
emoji: 📞
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
---

# Talk Buddy (Dial-a-Browser)

**Every website in India becomes a phone number.** The caller speaks Hindi, Hinglish or any of 10 Indian
languages. An agent drives a real browser on **live official websites** and talks back. The caller needs no
smartphone, no typing and no literacy.

India's public services are websites: train status, welfare schemes, weather, results. Hundreds of millions of
people can't use them. Today the answer is a separate helpline for each service. Talk Buddy works with the
websites as they already are: it isn't trained or scripted for any particular site.

Built on **Gnani AI**: Prisma v3.0 (speech-to-text) and Timbre v3.0 (text-to-speech). Reasoning runs on
**DeepSeek via Nebius Token Factory**, with Groq as a fallback.

## Real use cases (tested live)
| Caller says | Real site | What happens | Measured |
|---|---|---|---|
| "मेरी बेटी 12309 राजधानी से आ रही है, अभी कहाँ पहुँची?" | **NTES** (Indian Railways) | Types the train number, reads live position, delay, ETA and platform | ~23 s end to end |
| "भुवनेश्वर से खुर्दा रोड की ट्रेनें बताओ" | **NTES** Trains B/w Stations | Opens the tab, fills both stations via autocomplete, gets the list (155 trains) | ~33 s |
| "मैं 62 साल की विधवा हूँ बिहार से, कोई पेंशन योजना?" | **myScheme.gov.in** | Searches, filters to Bihar, opens the scheme, quotes "₹400/- per month" from the page | ~26 s |
| "कोणार्क सूर्य मंदिर किसने बनवाया?" | Hindi **Wikipedia** | Opens the article and answers | ~10 s |
| Booking walkthrough | RailSeva (local demo portal) | IRCTC needs the user's own login, captcha and payment, which the agent never handles, so the booking flow runs on a clearly labelled demo portal | |

Most of the remaining time is the government sites' own servers responding. The greeting plays in under 1 s, and an
acknowledgement ("जी, एक सेकंड…") in about 2 s, so the line is never silent.

Also tested: **PM-Kisan** blocks automated browsers; **IRCTC** blocks headless browsers.

## What makes it work

### 1. Two voices, and the website voice can't hallucinate
- **Guide voice** (*Shivani*) is the assistant: explanations and questions.
- **Site voice** (*Avinash*) speaks **only the website's own words**.
- **Grounding check:** every site-voice line is compared with the text of the page that is open right now. If
  under 80% of its words and numbers are on the page, it's **blocked and never spoken**. In testing, a line
  the model invented scored 40% and was rejected; real lines from the page scored 100%. The UI shows
  "✓ N% found on page" on each site-voice line.

### 2. Full duplex: talk over it, side talk is ignored
- When the caller starts speaking, the assistant's voice **drops to 25% volume** instead of cutting off.
- Each utterance is classified:

| Heard | Action |
|---|---|
| Request, answer, "रुको", or a new request ("मौसम भी बताओ") | Stop talking and handle it |
| "हम्म", "अच्छा", "ok" while it explains | Keep talking |
| Side talk ("बेटा, TV की आवाज़ कम करो"), TV, noise | Ignore it; volume returns |
| The assistant's own voice echoing through the speakers | Ignore it |

  7/7 on the test set. Ignored speech appears greyed out in the UI with the reason.

### 3. General browsing, fast
- **`browse` runs a multi-step plan in one LLM call:** "open → click 'Trains B/w Stations' → type 'From Station*'
  (pick suggestion) → type 'To Station*' → click 'Get Trains'". Steps target **visible labels**, not element ids,
  so the model can plan pages it hasn't seen yet.
- **Learned site flows:** every flow that ends in a verified answer is saved per website (`data/site_flows.json`)
  and reused, so repeat tasks plan correctly the first time. Nothing is hardcoded per site.
- **"PAGE CHANGE" feedback:** after each action the model is told what changed. Many sites update content without
  changing the URL, and this stops the model repeating a click that already worked.
- **Smart page settling:** tracks the page's own fresh requests and DOM stability instead of waiting for
  "network idle", which chatty sites like NTES never reach. That one change cut a five-step flow from 37 s to 16 s.
- **Site readers** (`adapters.py`) turn dense pages into facts. Example: NTES's 30 station rows become "Last reported
  / Next stop / Destination ETA + platform".

### 4. Natural Indian speech
| | |
|---|---|
| **Prisma STT** | 16 kHz wideband, `hi-IN` hint, `transcribe` format so "बारह तीन शून्य नौ" arrives as **12309**; VAD tuned for a laptop mic; key terms (stations, trains, scheme words) boosted |
| **Timbre TTS** | 24 kHz playback; everyday spoken Hindi ("अच्छा, देखिए…") instead of formal written Hindi; sparing `<pauses>` / `<excited>` audio tags; train numbers and PNRs read digit by digit; ₹ amounts in lakh/crore |
| **Gender-aware Hindi** | The assistant's grammar follows its voice (female voice: "देख रही हूँ"). The caller is addressed by the gender their **own words** show ("मैं विधवा हूँ" → feminine), never guessed from pitch or name; until then, neutral phrasing. Callers can ask for a male or female voice. |
| **Language** | Replies in the caller's language and script; English websites are explained in Hindi, Tamil and more |

## Architecture
```
caller mic (16 kHz) ─▶ Prisma v3.0 streaming STT ─▶ duplex filter (relevant / backchannel / side talk / echo)
                                                         │
                                                         ▼
                              LLM agent: DeepSeek V4.1 Flash (Nebius) → V4 Pro → Groq fallbacks
                              tools: browse (multi-step) · open · click · type · fill_form · say · set_voice
                                                         │
                                                         ▼
                              shared headless Chromium (Playwright) on live websites
                              page → compact text + site summary + grounding corpus + PAGE CHANGE
                                                         │
caller ◀─ Timbre v3.0 streaming TTS (24 kHz, guide + site voice, cached common phrases) ◀─┘
```

### Why these models
Benchmarked on this agent's real prompt and tools (plan the NTES flow + answer from a live-status page):

| Model | Correct plan in one call | Answer | Time |
|---|---|---|---|
| **DeepSeek V4.1 Flash** (primary) | with learned flows ✅ | good Hindi | ~2–3 s |
| DeepSeek V4 Pro (fallback) | ✅ even cold | best Hindi, but reasons at length | up to 26 s on big pages |
| GLM-5.3 Flash | ✅ | good | 7.8 s |
| Qwen3-235B | ❌ missed the tab | OK | 2.5 s |
| gpt-oss-120b (Groq) | ❌ wrong labels | called DDU "Darjeeling" | 1 s |

## Run locally
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -X utf8 server.py
```
Open **http://localhost:8000/phone** in Chrome or Edge, press **📞 Call**, allow the mic and speak. Use
headphones. With no mic, type in the box under the phone.

### `.env`
```
GNANI_API_KEY=...        # required (Prisma + Timbre)
NEBIUS_API_KEY=...       # primary LLM (DeepSeek)
GROQ_API_KEY=...         # fallback LLM
# optional
NEBIUS_MODELS=           # override, comma-separated, in order
GUIDE_VOICE=Shivani      # SITE_VOICE=Avinash
STT_LANGS=hi-IN          # one locale or indic-auto
PHONE_MODE=0             # 1 = simulate an 8 kHz phone line
HEADLESS=1               # 0 = real window off-screen (IRCTC accepts it)
PROXY_URL=               # optional: http://user:pass@host:port (e.g. Indian proxy for geo-blocked sites)
```
`.env` is re-read on every call, so rotated keys apply without a restart.

## Deploy on Render
The repo includes a `Dockerfile` (Microsoft's Playwright image with Chromium) and `render.yaml`.
1. On Render, choose **New → Blueprint** and pick this repo.
2. Enter `GNANI_API_KEY`, `NEBIUS_API_KEY` and `GROQ_API_KEY` when prompted.
3. Open `https://<app>.onrender.com/phone`. Render's HTTPS lets the browser use the mic.

Use the **Standard** plan (2 GB): Chromium doesn't fit in 512 MB. The region is Singapore, the closest to India.
Some Indian government sites may block non-Indian IPs, so test them after deploying.

## Files
| File | Role |
|---|---|
| `server.py` | Call session: audio routing, speech queue, duplex ducking/barge-in, TTS cache, call recording to `logs/` |
| `gnani.py` | Prisma streaming STT + Timbre streaming TTS; speakable numbers; Hindi gender grammar |
| `agent.py` | Tool-calling agent: prompt, tools, grounding, learned flows, gender rules |
| `llm.py` | Provider pool: Nebius → Groq, instant switch on rate limits |
| `duplex.py` | Side-talk / backchannel / echo filter |
| `browser.py` | Shared Playwright browser, page snapshots, multi-step `browse`, smart settling, crash recovery |
| `memory.py` | Learned per-site flows |
| `adapters.py` | Site readers (NTES live status) |
| `portal.py` | RailSeva demo booking portal |
| `static/phone.html` | Feature-phone UI with a live view of the agent's browser |

## Limits
- Real sites change; generic browsing adapts, but `adapters.py` readers may need updates.
- Free LLM tiers rate-limit heavy use. The agent switches providers instantly, and if every provider is out of
  quota it tells the caller immediately instead of going silent.
- No logins, passwords, Aadhaar, card details or payments are ever entered by the agent; adult, gambling and
  piracy sites are refused.
