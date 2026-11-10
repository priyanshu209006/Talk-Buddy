# Dial-a-Browser

**Every website in India becomes a phone number.** The caller speaks in any Indian language from any phone.
An agent drives a real browser on **live official websites** and talks back. The caller needs no smartphone,
no typing and no literacy.

India's public services are websites: train status, welfare schemes, weather, results. Hundreds of millions of
people can't use them. Today the answer is a separate helpline for each service. Dial-a-Browser works with the
websites as they already are.

## Real use cases (tested live)
| Caller says | Real site used | What happens |
|---|---|---|
| "मेरी बेटी 12309 राजधानी से आ रही है, ट्रेन कहाँ पहुँची?" | **NTES**, Indian Railways' official enquiry site | Types the train no., reads live position, delay, ETA and platform. *Tested: "last reported Danapur, 3 min late; New Delhi expected 07:55, platform 7", about 14 s after the question* |
| "मैं 62 साल की विधवा हूँ, बिहार से, कोई पेंशन योजना है?" | **myScheme.gov.in** (5,154 government schemes) | Searches, filters by state, opens the scheme, quotes its benefit and eligibility lines |
| "आज बारिश होगी?" | **IMD** (mausam.imd.gov.in) | Reads the official forecast |
| "ताजमहल के बारे में बताओ" | Hindi Wikipedia | Reads and summarises |
| Booking walkthrough | RailSeva demo portal (local) | IRCTC requires the user's own login, captcha and payment, which the agent never handles, so the booking flow is shown on a clearly labelled demo portal |

Also checked: **PM-Kisan** blocks automated browsers. **IRCTC** blocks headless browsers (it loads in a real window; see `HEADLESS` below).

## Why the website voice can be trusted
- **Two voices.** *Shivani* (guide) is the assistant. *Avinash* (site) speaks only the website's own words.
- **Grounding check.** Every site-voice line is checked against the text of the page that is open right now.
  If under 80% of its words and numbers are on the page, it is **blocked and never spoken**, and the agent must
  quote the page. In testing, a line the model invented ("Bihar widow pension ₹400") scored 40% and was
  rejected; real lines from the scheme page scored 100%. The phone UI shows "✓ N% found on page" on every
  site-voice line.
- **Site readers** (`adapters.py`) turn dense pages into facts. Example: NTES's 30 station rows become
  "Last reported / Next stop / Destination ETA + platform".

## Architecture
```
caller audio (8 kHz) ──▶ Prisma v3.0 streaming STT ──▶ Groq agent (open / click / fill_form / type_and_submit / say)
                                                          │
                                                          ▼
                                       real Chromium/Edge (Playwright) on live websites
                                                          │ page → compact text + site summary + grounding corpus
caller ◀── Timbre v3.0 streaming TTS (guide + site voice) ◀┘
       ▲
       └── Prisma speech_start while we talk → barge-in: audio stops, agent is told what was cut off
```

| Timbre feature | How it's used |
|---|---|
| Two voices | Assistant vs. the website's verified words |
| Narration during page loads | "NTES खोल रहा हूँ…", so there's no dead air while the browser works |
| Indian number reading | Train no. and PNR read digit by digit; ₹ in lakh/crore (Timbre normaliser + `gnani.speakable`) |
| All 10 languages per voice | English websites explained in the caller's language |
| Streaming + barge-in | Cut off mid-sentence the moment the caller speaks |

## Run it
```bash
cd C:\Users\user\dial-a-browser
.venv\Scripts\python.exe -X utf8 server.py
```
Open **http://localhost:8000/phone** in Chrome or Edge, press **📞 Call**, allow the mic and speak (use
headphones so barge-in doesn't hear the assistant). With no mic, type in the box under the phone.

`.env` needs `GNANI_API_KEY` and `GROQ_API_KEY` (it's re-read on every call, so rotated keys apply without a restart).
`HEADLESS=1` is the stable default. `HEADLESS=0` uses a real browser window parked off-screen, which IRCTC
accepts, but on this machine Edge sometimes closed that window by itself. The agent relaunches the browser
automatically if it dies.

## Files
- `server.py`: call session: audio routing, speech queue, barge-in, OTP→SMS relay
- `gnani.py`: Prisma streaming STT and Timbre streaming TTS clients, plus speakable numbers
- `agent.py`: Groq tool-calling agent: prompt, tools, site-voice grounding, model rotation
- `browser.py`: Playwright driver, page→text snapshot, grounding check, crash recovery
- `adapters.py`: site readers (NTES live-status summary)
- `portal.py`: RailSeva demo booking portal
- `static/phone.html`: feature-phone UI with a live view of the agent's browser

## Limits
- **Groq free tier: 8k tokens/min per model**, plus a daily cap. The agent batches speech, sends only the newest
  page in full, and rotates `gpt-oss-120b` → `gpt-oss-20b` → `qwen3.8-27b`. On a busy call you may hear
  "एक सेकंड…". A Dev-tier key removes this.
- Real sites change their layout. Generic browsing adapts, but `adapters.py` readers may need updates.
