"""Site-specific readers that turn dense real-world pages into facts a caller needs.

The agent still sees the raw page; these add a short, computed summary on top so the
LLM doesn't have to infer things like "where is the train right now" from 30 rows.
"""
import re

_TIME = r"(\d{2}:\d{2}) (\d{2}-[A-Za-z]{3})(\*?)"


def _parse_ntes_row(t: str):
    src = t.startswith("SRC SRC ")
    if src:
        t = t[len("SRC SRC "):]
    m = re.search(r"^(.*?)\s?([A-Z][A-Z0-9 .()'\-]*?)\s([A-Z]{2,5})\sPF\s(\d+)(\*?)", t)
    if not m:
        return None
    prefix, name, code, pf, pf_star = m.groups()
    rest = t[m.end():]
    arr = re.findall(_TIME, prefix)
    dep = re.findall(_TIME, rest)
    delay_txt = re.sub(_TIME, "", prefix).strip()
    km = re.search(r"(\d+) KMs", rest)
    row = {
        "name": name.strip(), "code": code, "pf": pf, "pf_confirmed": not pf_star,
        "km": int(km[1]) if km else (0 if src else None),
        "src": src, "dstn": "DSTN" in rest,
        "arr": arr[1] if len(arr) > 1 else (arr[0] if arr else None),       # actual/expected arrival
        "dep": dep[1] if len(dep) > 1 else (dep[0] if dep else None),
        "delay": delay_txt or None,
    }
    dep_delay = re.sub(_TIME, "", rest.split("KMs")[-1] if "KMs" in rest else rest)
    dep_delay = re.sub(r"Non-Stopping Stations|Coach Position|DSTN|SRC|⇓", "", dep_delay).strip()
    row["dep_delay"] = dep_delay or row["delay"]
    if src:
        row["delay"] = row["dep_delay"]
    return row


def ntes_status(elements: list[str]) -> dict | None:
    """Structured live status: {'last', 'next', 'dest', 'rows'} (rows as parsed dicts)."""
    rows = []
    for el in elements:
        m = re.match(r'\[e\d+\] button "(.*)"$', el)
        if m and " PF " in m[1]:
            r = _parse_ntes_row(m[1])
            if r:
                rows.append(r)
    if len(rows) < 2:
        return None
    done = lambda r: (r["dep"] or r["arr"]) is not None and (r["dep"] or r["arr"])[2] != "*"
    reached = [r for r in rows if done(r)]
    for r in rows:
        for k in ("delay", "dep_delay"):
            if r[k] and re.search(r"\d", r[k]) and "late" not in r[k]:
                r[k] += " late"
    return {"last": reached[-1] if reached else None,
            "next": next((r for r in rows if not done(r)), None),
            "dest": next((r for r in rows if r["dstn"]), rows[-1]),
            "rows": rows}


def ntes_summary(elements: list[str]) -> str | None:
    rows = []
    for el in elements:
        m = re.match(r'\[e\d+\] button "(.*)"$', el)
        if not m or " PF " not in m[1]:
            continue
        r = _parse_ntes_row(m[1])
        if r:
            rows.append(r)
    if len(rows) < 2:
        return None

    def done(r):
        t = r["dep"] or r["arr"]
        return t is not None and t[2] != "*"

    reached = [r for r in rows if done(r)]
    last = reached[-1] if reached else None
    nxt = next((r for r in rows if not done(r)), None)
    dest = next((r for r in rows if r["dstn"]), rows[-1])

    def when(t):
        return f"{t[0]} {t[1]}" if t else "?"

    for r in rows:
        for k in ("delay", "dep_delay"):
            if r[k] and re.search(r"\d", r[k]) and "late" not in r[k]:
                r[k] += " late"

    lines = ["LIVE STATUS SUMMARY (computed from NTES rows; '*' times are predictions):"]
    if last:
        verb = "departed" if last["dep"] and not last["dstn"] else "reached"
        lines.append(f"- Last reported: {verb} {last['name']} ({last['code']}) at "
                     f"{when(last['dep'] or last['arr'])}, {last['dep_delay'] or last['delay'] or 'On Time'}.")
    if nxt:
        lines.append(f"- Next stop: {nxt['name']} ({nxt['code']}), expected {when(nxt['arr'])}, "
                     f"{nxt['delay'] or 'On Time'}, platform {nxt['pf']}"
                     f"{'' if nxt['pf_confirmed'] else ' (expected)'}.")
    if dest and dest is not last:
        lines.append(f"- Destination: {dest['name']} ({dest['code']}), expected {when(dest['arr'])}, "
                     f"{dest['delay'] or 'On Time'}, platform {dest['pf']}"
                     f"{'' if dest['pf_confirmed'] else ' (expected)'}, {dest['km']} km from origin.")
    if last is None:
        lines.append("- The train has not started yet (all times are predictions).")
    lines.append("- Stops: " + "; ".join(
        f"{r['code']} {when(r['arr'] if not r['src'] else r['dep'])}{'' if done(r) else '*'}" for r in rows))
    return "\n".join(lines)


def summarize(url: str, elements: list[str]) -> str | None:
    if "enquiry.indianrail.gov.in" in url:
        return ntes_summary(elements)
    return None
