"""RailSeva: a mock railway booking portal (demo stand-in for a real government site).

Deliberately built like a typical portal: server-rendered forms, tables, a review step
and an OTP sent to the user's phone. Everything is fake; no real tickets are booked.
"""
import datetime as dt
import hashlib
import html
import random
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/portal")

STATIONS = {
    "NDLS": "New Delhi", "PNBE": "Patna Jn", "MAS": "MGR Chennai Central", "HWH": "Howrah Jn",
    "CSMT": "Mumbai CSMT", "SBC": "KSR Bengaluru", "LKO": "Lucknow NR", "BSB": "Varanasi Jn",
    "JP": "Jaipur Jn", "ADI": "Ahmedabad Jn", "SC": "Secunderabad Jn", "GHY": "Guwahati",
    "MDU": "Madurai Jn", "BPL": "Bhopal Jn", "CBE": "Coimbatore Jn", "ERS": "Ernakulam Jn",
}
ALIASES = {
    "delhi": "NDLS", "dilli": "NDLS", "new delhi": "NDLS", "patna": "PNBE", "chennai": "MAS",
    "madras": "MAS", "kolkata": "HWH", "calcutta": "HWH", "howrah": "HWH", "mumbai": "CSMT",
    "bombay": "CSMT", "bengaluru": "SBC", "bangalore": "SBC", "lucknow": "LKO", "varanasi": "BSB",
    "banaras": "BSB", "kashi": "BSB", "jaipur": "JP", "ahmedabad": "ADI", "hyderabad": "SC",
    "secunderabad": "SC", "guwahati": "GHY", "madurai": "MDU", "bhopal": "BPL",
    "coimbatore": "CBE", "kochi": "ERS", "ernakulam": "ERS", "cochin": "ERS",
}
NAMES = ["Rajdhani Express", "Sampark Kranti", "Superfast Express", "Mail", "Garib Rath",
         "Humsafar Express", "Vande Bharat Express", "Duronto Express", "Jan Shatabdi", "Express"]
CLASSES = {"SL": ("Sleeper", 1.0), "3A": ("AC 3 Tier", 2.6), "2A": ("AC 2 Tier", 3.7)}

BOOKINGS: dict[str, dict] = {}
PNRS: dict[str, dict] = {}
OTP_LISTENERS = []   # callables(phone_number, otp) – the phone simulator subscribes


def resolve(q: str) -> str | None:
    q = (q or "").strip()
    if not q:
        return None
    up = q.upper()
    if up in STATIONS:
        return up
    low = q.lower()
    for k, v in ALIASES.items():
        if k in low:
            return v
    for code, name in STATIONS.items():
        if low in name.lower():
            return code
    return None


def trains_between(a: str, b: str, date: str):
    seed = int(hashlib.md5(f"{a}{b}".encode()).hexdigest(), 16)
    r = random.Random(seed)
    day = random.Random(seed + int(date.replace("-", "")))
    out = []
    dist = 400 + seed % 1500
    for i in range(r.randint(3, 5)):
        num = f"{r.randint(12, 22)}{r.randint(100, 999)}"
        dep_h, dep_m = r.randint(0, 23), r.choice([0, 5, 15, 20, 30, 40, 45, 55])
        dur = dist / r.choice([48, 55, 62, 70]) + r.random()
        arr = (dt.datetime(2026, 1, 1, dep_h, dep_m) + dt.timedelta(hours=dur))
        name = f"{STATIONS[a].split()[0]} {STATIONS[b].split()[0]} {r.choice(NAMES)}"
        avail = {}
        for c, (_, mult) in CLASSES.items():
            roll = day.random()
            if roll < 0.35:
                status = f"AVAILABLE-{day.randint(1, 180):04d}"
            elif roll < 0.6:
                status = f"RAC {day.randint(1, 40)}"
            elif roll < 0.85:
                status = f"GNWL {day.randint(1, 120)}"
            else:
                status = f"RLWL {day.randint(1, 60)}"
            fare = int(round((dist * 0.32 * mult + 40) / 5) * 5)
            avail[c] = {"status": status, "fare": fare}
        out.append({
            "number": num, "name": name,
            "dep": f"{dep_h:02d}:{dep_m:02d}", "arr": arr.strftime("%H:%M"),
            "dur": f"{int(dur)}h {int((dur % 1) * 60)}m", "avail": avail,
        })
    out.sort(key=lambda t: t["dep"])
    return out


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)} | RailSeva</title>
<style>
 body{{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f2f4f7;color:#1d2433}}
 header{{background:#1f3a68;color:#fff;padding:10px 24px;display:flex;align-items:center;gap:14px}}
 header b{{font-size:22px;letter-spacing:.5px}} header span{{opacity:.8;font-size:13px}}
 .strip{{background:#f7a21b;height:4px}}
 main{{max-width:900px;margin:22px auto;background:#fff;padding:22px 28px;border:1px solid #d5dbe5}}
 h1{{font-size:20px;color:#1f3a68;margin-top:0}}
 label{{display:block;font-size:13px;margin:10px 0 4px;font-weight:bold}}
 input,select{{padding:7px;border:1px solid #9aa6b8;width:280px;font-size:14px}}
 button,.btn{{background:#1f3a68;color:#fff;border:0;padding:8px 16px;margin-top:14px;cursor:pointer;
   font-size:14px;text-decoration:none;display:inline-block}}
 .btn.small{{padding:4px 9px;margin:4px 0 0;font-size:12px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border:1px solid #d5dbe5;padding:6px;text-align:left}}
 th{{background:#e8edf5}} .ok{{color:#0a7a2f;font-weight:bold}} .wl{{color:#b3261e;font-weight:bold}}
 .notice{{background:#fff7e0;border-left:4px solid #f7a21b;padding:8px 12px;font-size:13px}}
 footer{{text-align:center;font-size:11px;color:#667;margin:18px}}
</style></head><body>
<header><b>RailSeva</b><span>Passenger Reservation &amp; Enquiry (demo portal)</span></header><div class="strip"></div>
<main>{body}</main>
<footer>RailSeva is a demonstration portal for Dial-a-Browser. No real tickets are issued.</footer>
</body></html>""")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def home():
    today = dt.date.today()
    opts = "".join(
        f'<option value="{(today + dt.timedelta(d)).isoformat()}">'
        f'{(today + dt.timedelta(d)).strftime("%d/%m/%Y (%A)")}</option>'
        for d in range(0, 10)
    )
    return page("Book Ticket", f"""
<h1>Book Ticket</h1>
<p class="notice">Enter station name or code, e.g. Patna or PNBE.</p>
<form action="/portal/search" method="get">
 <label for="from">From station</label><input id="from" name="from" placeholder="From station">
 <label for="to">To station</label><input id="to" name="to" placeholder="To station">
 <label for="date">Journey date</label><select id="date" name="date">{opts}</select>
 <br><button type="submit">Search Trains</button>
</form>
<p><a href="/portal/pnr">Check PNR Status</a></p>""")


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request):
    q = request.query_params
    a, b, date = resolve(q.get("from")), resolve(q.get("to")), q.get("date") or dt.date.today().isoformat()
    if not a or not b or a == b:
        return page("Search", f"""<h1>Search Trains</h1><p class="notice" role="alert">
Station not found: "{html.escape(q.get('from') or '')}" / "{html.escape(q.get('to') or '')}". Please check the station names.</p>
<a class="btn" href="/portal">Modify Search</a>""")
    d = dt.date.fromisoformat(date)
    rows = ""
    for t in trains_between(a, b, date):
        cells = ""
        for c, info in t["avail"].items():
            cls = "ok" if info["status"].startswith("AVAILABLE") else "wl"
            cells += (f'<td><span class="{cls}">{info["status"]}</span><br>Fare: ₹{info["fare"]}<br>'
                      f'<a class="btn small" href="/portal/book?train={t["number"]}&cls={c}&from={a}&to={b}&date={date}"'
                      f' aria-label="Book {t["number"]} {c}">Book {c}</a></td>')
        rows += (f'<tr><td>{t["number"]}</td><td>{t["name"]}</td><td>Departs {t["dep"]}</td>'
                 f'<td>Arrives {t["arr"]}</td><td>{t["dur"]}</td>{cells}</tr>')
    return page("Trains", f"""
<h1>{STATIONS[a]} ({a}) to {STATIONS[b]} ({b}) on {d.strftime('%d/%m/%Y')}</h1>
<table><tr><th>Train No.</th><th>Train Name</th><th>Departure</th><th>Arrival</th><th>Duration</th>
<th>Sleeper (SL)</th><th>AC 3 Tier (3A)</th><th>AC 2 Tier (2A)</th></tr>{rows}</table>
<a class="btn" href="/portal">Modify Search</a>""")


@router.get("/book", response_class=HTMLResponse)
async def book(request: Request):
    q = dict(request.query_params)
    t = next((x for x in trains_between(q["from"], q["to"], q["date"]) if x["number"] == q["train"]), None)
    if not t:
        return page("Error", "<p role=alert>Train not found.</p>")
    info = t["avail"][q["cls"]]
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in q.items())
    return page("Passenger Details", f"""
<h1>Passenger Details</h1>
<p>{t['number']} {t['name']} · {CLASSES[q['cls']][0]} · Status: {info['status']} · Fare: ₹{info['fare']} per passenger</p>
<form action="/portal/review" method="post">{hidden}
 <label for="pname">Passenger name</label><input id="pname" name="pname" placeholder="Full name">
 <label for="age">Age</label><input id="age" name="age" placeholder="Age">
 <label for="gender">Gender</label><select id="gender" name="gender"><option>Female</option><option>Male</option><option>Transgender</option></select>
 <label for="berth">Berth preference</label><select id="berth" name="berth"><option>No Preference</option><option>Lower</option><option>Middle</option><option>Upper</option><option>Side Lower</option><option>Side Upper</option></select>
 <label for="mobile">Mobile number (for OTP and ticket SMS)</label><input id="mobile" name="mobile" placeholder="10 digit mobile">
 <br><button type="submit">Continue to Review</button>
</form>""")


@router.post("/review", response_class=HTMLResponse)
async def review(request: Request):
    f = dict(await request.form())
    missing = [k for k in ("pname", "age", "mobile") if not f.get(k, "").strip()]
    if missing:
        return page("Passenger Details", f'<p class="notice" role="alert">Please fill: {", ".join(missing)}</p>'
                    '<button onclick="history.back()">Go Back</button>')
    t = next(x for x in trains_between(f["from"], f["to"], f["date"]) if x["number"] == f["train"])
    info = t["avail"][f["cls"]]
    bid = uuid.uuid4().hex[:10]
    BOOKINGS[bid] = {**f, "train_name": t["name"], "dep": t["dep"], "arr": t["arr"],
                     "status": info["status"], "fare": info["fare"] + 25}
    b = BOOKINGS[bid]
    d = dt.date.fromisoformat(b["date"]).strftime("%d/%m/%Y")
    return page("Review Journey", f"""
<h1>Review Journey Details</h1>
<table>
<tr><th>Train</th><td>{b['train']} {b['train_name']}</td></tr>
<tr><th>Journey</th><td>{STATIONS[b['from']]} to {STATIONS[b['to']]}, {d}, departs {b['dep']}</td></tr>
<tr><th>Class</th><td>{CLASSES[b['cls']][0]}</td></tr>
<tr><th>Passenger</th><td>{html.escape(b['pname'])}, {html.escape(b['age'])}, {b['gender']}, {b['berth']}</td></tr>
<tr><th>Current status</th><td>{b['status']}</td></tr>
<tr><th>Total fare (incl. ₹25 convenience fee)</th><td>₹{b['fare']}</td></tr>
<tr><th>Mobile</th><td>{html.escape(b['mobile'])}</td></tr>
</table>
<form action="/portal/pay" method="post"><input type="hidden" name="bid" value="{bid}">
<button type="submit">Pay ₹{b['fare']} and Send OTP</button></form>""")


@router.post("/pay")
async def pay(bid: str = Form(...)):
    b = BOOKINGS.get(bid)
    if not b:
        return page("Error", "<p role=alert>Session expired.</p>")
    b["otp"] = f"{random.randint(0, 999999):06d}"
    for fn in list(OTP_LISTENERS):
        try:
            fn(b["mobile"], b["otp"], f"RailSeva: OTP {b['otp']} to pay Rs {b['fare']} for train {b['train']}. Do not share.")
        except Exception:
            pass
    return RedirectResponse(f"/portal/otp?bid={bid}", status_code=303)


@router.get("/otp", response_class=HTMLResponse)
async def otp_page(bid: str, err: str = ""):
    b = BOOKINGS.get(bid)
    if not b:
        return page("Error", "<p role=alert>Session expired.</p>")
    e = '<p class="notice" role="alert">Incorrect OTP. Please try again.</p>' if err else ""
    return page("Verify OTP", f"""
<h1>Payment Verification</h1>{e}
<p>An OTP has been sent to mobile number ending {html.escape(b['mobile'][-4:])}.</p>
<form action="/portal/confirm" method="post"><input type="hidden" name="bid" value="{bid}">
<label for="otp">Enter OTP</label><input id="otp" name="otp" placeholder="6 digit OTP">
<br><button type="submit">Verify and Book</button></form>""")


@router.post("/confirm", response_class=HTMLResponse)
async def confirm(bid: str = Form(...), otp: str = Form(...)):
    b = BOOKINGS.get(bid)
    if not b:
        return page("Error", "<p role=alert>Session expired.</p>")
    if "".join(ch for ch in otp if ch.isdigit()) != b.get("otp"):
        return RedirectResponse(f"/portal/otp?bid={bid}&err=1", status_code=303)
    pnr = str(random.randint(2_000_000_000, 8_999_999_999))
    st = b["status"]
    if st.startswith("AVAILABLE"):
        coach = {"SL": "S", "3A": "B", "2A": "A"}[b["cls"]] + str(random.randint(1, 9))
        st = f"CNF / {coach} / {random.randint(1, 72)} / {b['berth'].upper() if b['berth'] != 'No Preference' else 'LOWER'}"
    PNRS[pnr] = {**b, "final_status": st}
    d = dt.date.fromisoformat(b["date"]).strftime("%d/%m/%Y")
    return page("Booked", f"""
<h1>Booking Confirmed</h1>
<table>
<tr><th>PNR</th><td>{pnr}</td></tr>
<tr><th>Train</th><td>{b['train']} {b['train_name']}</td></tr>
<tr><th>Journey</th><td>{STATIONS[b['from']]} to {STATIONS[b['to']]}, {d}, departs {b['dep']}</td></tr>
<tr><th>Passenger</th><td>{html.escape(b['pname'])}, {html.escape(b['age'])}</td></tr>
<tr><th>Booking status</th><td>{st}</td></tr>
<tr><th>Amount paid</th><td>₹{b['fare']}</td></tr>
</table>""")


@router.get("/pnr", response_class=HTMLResponse)
async def pnr_page(pnr: str = ""):
    res = ""
    if pnr:
        p = PNRS.get("".join(c for c in pnr if c.isdigit()))
        if p:
            res = (f"<table><tr><th>PNR</th><td>{pnr}</td></tr><tr><th>Train</th><td>{p['train']} {p['train_name']}</td></tr>"
                   f"<tr><th>Passenger</th><td>{html.escape(p['pname'])}</td></tr><tr><th>Status</th><td>{p['final_status']}</td></tr></table>")
        else:
            res = '<p class="notice" role="alert">PNR not found.</p>'
    return page("PNR Status", f"""<h1>PNR Status</h1>
<form action="/portal/pnr" method="get"><label for="pnr">PNR number</label><input id="pnr" name="pnr" placeholder="10 digit PNR">
<br><button type="submit">Get Status</button></form>{res}""")
