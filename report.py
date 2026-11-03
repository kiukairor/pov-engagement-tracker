"""Local engagement report. Python fetches and pre-computes; the browser renders and recomputes.

  python3 report.py [--days 30] [--gap 30] [--channels UI] [--pov-start YYYY-MM-DD] [--pov-end YYYY-MM-DD]

Writes three files into out/ (gitignored):
  report.html     the static app (a copy of report_template.html, no data and no PII in it)
  report.json     all the data (prospect emails, query text): the single source the app reads
  report.data.js  the same JSON wrapped as `window.REPORT = ...`, because browsers block fetch() on file:// pages

The browser recomputes tiers, the engagement score (a port of pov.engagement_score, checked for parity on load),
weekly series, KPIs and charts, so users can be hidden (e.g. a test user or an NR alias) and everything follows.
Sessions are ESTIMATED: a user's queries are split wherever there's a gap longer than --gap minutes.
"""
import argparse
import json
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import nr
import pov
import queries as Q
from pov import DAY_MS

HERE = Path(__file__).parent


def estimate_sessions(events, gap_min):
    by_user = defaultdict(list)
    for e in events:
        by_user[e["user"]].append(e)
    sessions = []
    for user, evs in by_user.items():
        cur = [evs[0]]
        for e in evs[1:] + [None]:
            if e is None or e["timestamp"] - cur[-1]["timestamp"] > gap_min * 60_000:
                start, end = cur[0]["timestamp"], cur[-1]["timestamp"]
                minutes = {c["timestamp"] // 60_000 for c in cur}
                distinct = len({c.get("query") for c in cur})
                span = max((end - start) / 60_000, 1)  # a one-query session still took at least a minute
                sessions.append({
                    "user": user, "start": start, "end": end, "minutes": round(span, 1), "queries": len(cur),
                    "active_minutes": len(minutes), "distinct": distinct,
                    "capabilities": ", ".join(sorted({c["productCapabilityId"] or "?" for c in cur}))[:80],
                    # Auto-refresh looks like a long session that keeps re-running a few distinct queries.
                    "flag": "repetitive: possible auto-refresh" if span >= 30 and distinct / len(cur) < 0.2
                            else "long, mostly idle" if span >= 60 and len(minutes) / span < 0.25 else "",
                })
                cur = [e] if e is not None else []
            else:
                cur.append(e)
    return sorted(sessions, key=lambda x: x["start"])


def native_sessions(since_ms, until_ms):
    rows = nr.nrql(f"FROM NrAuditEvent SELECT timestamp, actorEmail, actionIdentifier, aparse(description, '%Session Id: *') AS sid, description "
                   f"WHERE {Q.A_USER} AND actionIdentifier IN ('user.login','user.logout') SINCE {since_ms} UNTIL {until_ms} LIMIT MAX")
    logins = {r["sid"]: r for r in rows if r["actionIdentifier"] == "user.login" and r["sid"]}
    out = []
    for r in rows:
        if r["actionIdentifier"] == "user.logout" and r["sid"] in logins:
            out.append({"user": r["actorEmail"], "start": logins[r["sid"]]["timestamp"],
                        "hours": round((r["timestamp"] - logins[r["sid"]]["timestamp"]) / 3_600_000, 1),
                        "end": "expired" if "expired" in (r["description"] or "") else "logged out"})
    return rows, out


def local_ms(d):
    return int(datetime.combine(d, datetime.min.time(), pov.TZ).timestamp() * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--gap", type=int, default=30, help="inactivity gap in minutes that ends a session")
    ap.add_argument("--channels", default="UI", help="comma list of channels that count as human: UI, 'API key', 'AI / MCP'")
    ap.add_argument("--pov-start", help="PoV start date YYYY-MM-DD (default POV_START)")
    ap.add_argument("--pov-end", help="PoV end date YYYY-MM-DD (default POV_END or as-of)")
    ap.add_argument("--out", default=str(HERE / "out" / "report.html"))
    a = ap.parse_args()
    if a.pov_start:
        pov.POV_START = a.pov_start
    if a.pov_end:
        pov.POV_END = a.pov_end
    human = tuple(c.strip() for c in a.channels.split(","))
    now_ms = int(time.time() * 1000)
    since = now_ms - a.days * DAY_MS
    A = pov.analyze(now_ms, a.days, human)
    tz = pov.TZ
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M") if ms else ""

    # ---- indices ----
    users, caps, srcs = [], [], []
    uix = lambda u: users.index(u) if u in users else users.append(u) or len(users) - 1
    cix = lambda c: caps.index(c) if c in caps else caps.append(c) or len(caps) - 1
    six = lambda s: srcs.index(s) if s in srcs else srcs.append(s) or len(srcs) - 1
    start_day = datetime.fromtimestamp(A["start"] / 1000, tz).date()
    end_day = datetime.fromtimestamp(now_ms / 1000, tz).date()
    days = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    weeks = [w["week"] for w in A["weekly"]]

    # ---- prospect UI activity at minute level: [user, minute, cap, handwritten, source, day, hour, week] ----
    ev = {}
    for e in A["pov_ui"]:
        m = e["timestamp"] // 60_000
        dt = datetime.fromtimestamp(e["timestamp"] / 1000, tz)
        k = (e["user"], m, e["productCapabilityId"])
        hw = 1 if pov.is_handwritten_ui(e) else 0
        if k in ev:
            ev[k][3] |= hw
            continue
        ev[k] = [uix(e["user"]), m, cix(e["productCapabilityId"]), hw, six(e.get("source.name") or "?"),
                 (dt.date() - start_day).days, dt.hour, weeks.index(pov.week_start(e["timestamp"]))]
    logins = [[uix(r["actorEmail"]), r["timestamp"]] for r in A["audit"]
              if r["actionIdentifier"] == "user.login" and (r["actorEmail"] or "").endswith("@" + Q.DOMAIN)]
    builds = [[uix(r["actorEmail"]), r["timestamp"], 1 if (r["actionIdentifier"] or "").startswith("dashboard.") else 0,
               r["actionIdentifier"], r["targetType"], (r["description"] or "")[:140]] for r in A["builds"]]
    milestones = [[m["t"], m["weight"], m["kind"], m["what"], uix(m["what"]) if "@" in str(m["what"]) else -1] for m in A["milestones"]]
    nr_rows = [[uix(u), h, v] for u, h, v in pov.fetch_nr_minutes(A["start"], now_ms)]

    # ---- last-N-days detail that only Python has ----
    win = [e for e in A["pov_events"] if e["timestamp"] >= since]
    sessions = estimate_sessions([e for e in win if e["channel"] in human], a.gap)
    audit_rows, native = native_sessions(since, now_ms)
    non_ui = defaultdict(lambda: defaultdict(set))
    for e in win:
        if e["channel"] not in human:
            non_ui[e["user"]][e["channel"]].add(e["timestamp"] // 60_000)
    nr_win = A["nr_window"]
    nr_daily = defaultdict(set)
    nr_caps = defaultdict(set)
    for e in nr_win:
        if e["channel"] in human:
            d = (datetime.fromtimestamp(e["timestamp"] / 1000, tz).date() - start_day).days
            nr_daily[(e["user"], d)].add(e["timestamp"] // 60_000)
            nr_caps[(e["user"], e["productCapabilityId"] or "(unattributed)")].add(e["timestamp"] // 60_000)
    mix_min = defaultdict(set)
    for e in win + nr_win:
        mix_min[(e["user"], e["channel"], e["cohort"])].add(e["timestamp"] // 60_000)
    nr_ids = {}
    for r in nr.nrql(f"FROM NrAuditEvent SELECT latest(description) AS d WHERE {Q.A_NR} AND description LIKE '%@{Q.NR_DOMAIN}%' "
                     f"FACET actorId SINCE {A['start']} UNTIL {now_ms} LIMIT MAX"):
        m = re.search(r"[\w.+-]+@" + re.escape(Q.NR_DOMAIN), r["d"] or "")
        if m:
            nr_ids[f"NR user {r['facet']}"] = m.group(0)
    ch_rows = nr.nrql(f"FROM NrAuditEvent SELECT count(*) AS n WHERE {Q.A_CHANGE_BOTH} FACET {Q.A_COHORT}, {Q.A_ORIGIN}, "
                      f"if({Q.A_USER}, actorEmail, concat('NR user ', actorId)), actionIdentifier SINCE {since} UNTIL {now_ms} LIMIT MAX")
    changes = [{"cohort": r["facet"][0], "origin": r["facet"][1], "user": nr_ids.get(r["facet"][2], r["facet"][2]), "action": r["facet"][3], "n": r["n"]}
               for r in ch_rows]
    dg = pov.digest(pov.handwritten(A["pov_events"]))
    digest = [{"user": u, "distinct": len(d["queries"]), "origins": dict(d["origins"]), "event_types": d["event_types"].most_common(6),
               "attrs": d["attrs"].most_common(10), "custom": [x for x, _ in d["attrs"].most_common(20) if x.lower() not in pov.STANDARD_ATTRS][:8],
               "queries": d["queries"].most_common(15)} for u, d in dg.items()]

    sc = A["score"]
    data = {
        "meta": {"account": nr.ACCOUNT_ID, "domain": Q.DOMAIN, "nr_domain": Q.NR_DOMAIN, "tz": Q.TZ, "as_of": now_ms, "as_of_text": fmt(now_ms),
                 "since": since, "window_days": a.days, "pov_start": pov.POV_START, "pov_end": f"{A['pov_end']:%Y-%m-%d}", "start_ms": A["start"],
                 "human": list(human), "gap": a.gap},
        "constants": {"half_life_days": pov.HALF_LIFE_DAYS, "milestone_half_life_days": pov.MILESTONE_HALF_LIFE_DAYS, "weights": pov.WEIGHTS, "k": pov.K,
                      "tier_weight": pov.TIER_WEIGHT, "tier_light_min": pov.TIER_LIGHT_MIN, "tier_engaged_min": pov.TIER_ENGAGED_MIN,
                      "cap_full_minutes": pov.CAP_FULL_MINUTES, "sustain_min_engaged": pov.SUSTAIN_MIN_ENGAGED, "stall_after_days": pov.STALL_AFTER_DAYS,
                      "stall_window_days": pov.STALL_WINDOW_DAYS, "stall_factor": pov.STALL_FACTOR, "band_strong": pov.BAND_STRONG,
                      "band_partial": pov.BAND_PARTIAL, "lapsed_days": pov.LAPSED_DAYS},
        "text": {"reading_guide": pov.READING_GUIDE, "weekly_guide": pov.WEEKLY_GUIDE, "bands_how": pov.BANDS_HOW},
        "users": users, "cohort": ["Prospect" if u.endswith("@" + Q.DOMAIN) else "New Relic" for u in users],
        "caps": [{"name": c or "(unattributed)", "trivial": c in pov.TRIVIAL_CAPS} for c in caps], "sources": srcs,
        "days": [{"date": f"{d:%Y-%m-%d}", "start": local_ms(d), "weekday": d.weekday()} for d in days],
        "weeks": [{"date": f"{w:%Y-%m-%d}", "start": local_ms(w), "end": local_ms(w + timedelta(days=7))} for w in weeks],
        "ev": list(ev.values()), "logins": logins, "builds": builds, "milestones": milestones, "nr": nr_rows,
        "ingest": [{"date": f"{w:%Y-%m-%d}", "gb": A["ingest"].get(w, 0)} for w in weeks],
        "nr_daily": [[uix(u), d, len(m)] for (u, d), m in nr_daily.items()],
        "nr_caps": [[uix(u), c, len(m)] for (u, c), m in nr_caps.items()],
        "non_ui": [[uix(u), ch, len(m)] for u, chs in non_ui.items() for ch, m in chs.items()],
        "mix": [[uix(u), ch, coh, len(m)] for (u, ch, coh), m in mix_min.items()],
        "sessions": [dict(s, user=uix(s["user"])) for s in sessions],
        "native": [dict(s, user=uix(s["user"])) for s in native],
        "logins_window": sum(r["actionIdentifier"] == "user.login" for r in audit_rows),
        "expiries_window": sum("expired" in (r.get("description") or "") for r in audit_rows),
        "changes": [dict(c, user=uix(c["user"]) if "@" in c["user"] else c["user"]) for c in changes],
        "funnel": A["funnel"], "funnel_rows": [dict(r, user=uix(r["user"])) for r in A["funnel_rows"]],
        "dashboards": [dict(d, creator=uix(d["creator"])) for d in A["dash_rows"]],
        "digest": [dict(d, user=uix(d["user"])) for d in digest],
        "actions": A["actions"],
        "server": {"score": sc["score"], "weekly": [w["score"] for w in A["weekly"]], "period": A["period"]},
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, default=str, separators=(",", ":"))
    out.with_suffix(".json").write_text(blob)
    out.with_name(out.stem + ".data.js").write_text("window.REPORT = " + blob + ";\n")
    shutil.copyfile(HERE / "report_template.html", out)
    print(f"Wrote {out} (+ .json, .data.js: {len(blob) / 1024:.0f} KB) | as of {fmt(now_ms)}, score {sc['score']} {sc['colour']}, "
          f"weekly {[w['score'] for w in A['weekly']]}, period {A['period']['mean'] if A['period'] else '-'}")


if __name__ == "__main__":
    main()
