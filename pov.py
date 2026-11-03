"""Whole-PoV analysis shared by report.py (full detail) and build_dashboard.py (snapshot widget).

Everything is anchored to one as-of time and the PoV start (POV_START, default = prospect account creation),
so the numbers in the report and the snapshot are reproducible and say which window they cover.
"""
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import nr
import queries as Q

DAY_MS = 86_400_000
TZ = ZoneInfo(Q.TZ)
POV_START = os.environ.get("POV_START", "2026-08-17")
POV_END = os.environ.get("POV_END")  # optional; default = as-of
HALF_LIFE_DAYS = 7          # activity loses half its weight every 7 days
LAPSED_DAYS = 14            # a user with no UI activity for this long is "lapsed"
# Capabilities that are navigation or account admin, not product evaluation.
TRIVIAL_CAPS = {None, "HOMEPAGE", "NR_UI", "MANAGE_YOUR_PLAN", "ADMINISTRATION"}
# Audit actions that build something (dashboards, alerts, rules, pipelines, synthetics, workflows).
# User/group/key management and trial events are admin and don't count.
BUILD_ACTION = re.compile(
    r"^(dashboard\.(create|update|add_widgets|update_widgets_in_page|update_page|add_page)"
    r"|alerts\.(condition|policy|muting_rule|nrql_condition)\.(create|update)"
    r"|e2m_rule\.create|nrql_drop_rule\.create|account\.(create|update)_log_parsing_rule"
    r"|account\.update_log_pipeline_configuration_options|entity_created"
    r"|synthetics?\..*(create|update).*|workflow.*(create|update).*|notifications\.(create|update)_.*)$")
CRITERIA_FILE = Path(__file__).parent / "criteria.json"


def pov_start_ms():
    return int(datetime.fromisoformat(POV_START).replace(tzinfo=TZ).timestamp() * 1000)


def channel(e):
    """Same rule as queries.CHANNEL."""
    if e.get("source.userApiKeyId"):
        return "API key"
    return "AI / MCP" if e.get("source.name") == "nerdgraph" else "UI"


def fetch_window(where, since_ms, until_ms):
    """Raw NrdbQuery events in [since, until); halves the window whenever it hits the 5000-row NRQL limit."""
    batch = nr.nrql(f"FROM NrdbQuery SELECT timestamp, user, productCapabilityId, productFeature, source.name, "
                    f"source.userApiKeyId, source.dashboardId, query WHERE {where} SINCE {since_ms} UNTIL {until_ms} LIMIT MAX")
    if len(batch) < 5000 or until_ms - since_ms < 60_000:
        return batch
    mid = (since_ms + until_ms) // 2
    return fetch_window(where, since_ms, mid) + fetch_window(where, mid, until_ms)


def fetch_events(where, since_ms, until_ms):
    rows = fetch_window(where, since_ms, until_ms)
    for r in rows:
        r["channel"] = channel(r)
        r["cohort"] = "Prospect" if r["user"].endswith("@" + Q.DOMAIN) else "New Relic"
    return sorted(rows, key=lambda r: r["timestamp"])


def fetch_audit(since_ms, until_ms):
    return nr.nrql(f"FROM NrAuditEvent SELECT timestamp, actionIdentifier, actorEmail, actorId, actorType, targetId, targetType, description "
                   f"WHERE {Q.A_USER} OR {Q.A_NR} SINCE {since_ms} UNTIL {until_ms} LIMIT MAX")


def fetch_ingest_weekly(since_ms, until_ms):
    rows = nr.nrql(f"FROM NrConsumption SELECT sum(GigabytesIngested) AS gb WHERE productLine = 'DataPlatform' "
                   f"FACET weekOf(timestamp) SINCE {since_ms} UNTIL {until_ms} LIMIT MAX WITH TIMEZONE '{Q.TZ}'")
    return {datetime.strptime(r["facet"].replace("Week of ", ""), "%B %d, %Y").date(): r["gb"] for r in rows}


def dashboard_names(guids):
    if not guids:
        return {}
    res = nr.graphql("query($g:[EntityGuid]!){actor{entities(guids:$g){guid name}}}", {"g": sorted(guids)[:25]})
    return {e["guid"]: e["name"] for e in (res.get("data") or {}).get("actor", {}).get("entities", []) or []}


# ---------------- scoring ----------------

def decay(age_ms):
    return 0.5 ** (max(age_ms, 0) / DAY_MS / HALF_LIFE_DAYS)


def dr(x, k):
    """Diminishing returns: 0 at 0, 63% at k, 86% at 2k, 95% at 3k. Never a hard cap."""
    return 1 - math.exp(-x / k)


def is_build(r):
    return bool(BUILD_ACTION.match(r["actionIdentifier"] or "")) and r.get("targetType") not in ("user", "group")


# Activity tiers per user per week. Calibrated on the prospect's 49 user-weeks: real (non-navigation) active minutes fall
# into 1-2 (25 user-weeks: a glance), 3-7 (8) and 12+ (16), with nothing between 7 and 12, so the cut-offs are 3 and 10.
# A hand-written query or a build lifts a user one tier but only makes them Engaged with at least 3 real minutes,
# so a 2-minute visit that types one query is Light, not Engaged.
TIER_LIGHT_MIN = 3
TIER_ENGAGED_MIN = 10
TIER_WEIGHT = {"Engaged": 1.0, "Light": 0.3, "Passive": 0.05}
TIERS = ("Engaged", "Light", "Passive")
TIER_RANK = {None: 0, "Passive": 1, "Light": 2, "Engaged": 3}


def is_handwritten_ui(e):
    """A query the person typed in the query builder (UI channel), excluding autocomplete and schema lookups."""
    if e.get("productFeature") != "Query your data":
        return False
    q = NRQL_NOISE.sub("", " ".join((e.get("query") or "").split())).strip()
    return bool(q) and not UI_GENERATED.search(q)


def tier_of(real_min, hw, builds):
    if real_min >= TIER_ENGAGED_MIN or ((hw or builds) and real_min >= TIER_LIGHT_MIN):
        return "Engaged"
    if real_min >= TIER_LIGHT_MIN or hw or builds:
        return "Light"
    return "Passive"


def tiers(ui_events, build_events, logins=(), bucket=None):
    """{(user, bucket): {"tier", "real_minutes", "capabilities", "handwritten", "builds", "last"}}; bucket = week by default."""
    bucket = bucket or (lambda ms: week_start(ms))
    uw = defaultdict(lambda: {"real": set(), "caps": set(), "hw": 0, "builds": 0, "last": 0})
    for e in ui_events:
        d = uw[(e["user"], bucket(e["timestamp"]))]
        d["last"] = max(d["last"], e["timestamp"] // 60_000 * 60_000)
        if e["productCapabilityId"] not in TRIVIAL_CAPS:
            d["real"].add(e["timestamp"] // 60_000)
            d["caps"].add(e["productCapabilityId"])
        d["hw"] += is_handwritten_ui(e)
    for r in build_events:
        d = uw[(r["actorEmail"], bucket(r["timestamp"]))]
        d["builds"] += 1
        d["last"] = max(d["last"], r["timestamp"])
    for r in logins:
        d = uw[(r["actorEmail"], bucket(r["timestamp"]))]
        d["last"] = max(d["last"], r["timestamp"])
    return {k: {"tier": tier_of(len(d["real"]), d["hw"], d["builds"]), "real_minutes": len(d["real"]), "capabilities": len(d["caps"]),
                "handwritten": d["hw"], "builds": d["builds"], "last": d["last"]} for k, d in uw.items()}


# ---------------- onboarding milestones ----------------

MILESTONE_HALF_LIFE_DAYS = 14   # onboarding credit fades twice as slowly as usage, but it does fade
STALL_AFTER_DAYS = 42           # after ~6 weeks the PoV is expected to keep adding sources...
STALL_WINDOW_DAYS = 21          # ...so no new milestone for 3 weeks costs a multiplier
STALL_FACTOR = 0.85
SOURCE_DOMAINS_EXCLUDED = ("VIZ", "AIOPS", "NGEP", "NR1")  # dashboards/alerts/pipeline rules are building, not data sources


def fetch_milestones(audit, ingest_by_type, start_ms, until_ms):
    """Time-stamped onboarding milestones: ingest keys, first data per type, first entity per type, ingest growth weeks."""
    import base64
    ms = []
    for r in audit:
        if r["actionIdentifier"] == "api_key_service.ingest_key.create" and (r["actorEmail"] or "").endswith("@" + Q.DOMAIN):
            ms.append({"t": r["timestamp"], "kind": "Ingest key created", "what": r["actorEmail"], "weight": 1.0})
    for typ, weeks in ingest_by_type.items():
        first = min((w for w, gb in weeks.items() if gb > 0.1), default=None)
        if first:
            ms.append({"t": int(datetime.combine(first, datetime.min.time(), TZ).timestamp() * 1000), "kind": "First data of a type",
                       "what": typ.replace("Bytes", ""), "weight": 1.0})
    rows = nr.nrql(f"FROM EntityAudits SELECT min(timestamp) AS first, uniqueCount(guid) AS n WHERE event = 'CREATION' "
                   f"FACET substring(guid, 0, 28) SINCE {start_ms} UNTIL {until_ms} LIMIT MAX")
    first_type = {}
    for x in rows:
        g = x["facet"]
        parts = base64.b64decode(g[: len(g) // 4 * 4]).decode("utf-8", "replace").split("|")
        if len(parts) >= 3 and parts[1] not in SOURCE_DOMAINS_EXCLUDED:
            k = f"{parts[1]} {parts[2][:12]}".rstrip()
            first_type[k] = min(first_type.get(k, x["first"]), x["first"])
    for k, t in first_type.items():
        ms.append({"t": int(t), "kind": "First entity of a type", "what": k, "weight": 1.0})
    totals = defaultdict(float)
    for weeks in ingest_by_type.values():
        for w, gb in weeks.items():
            totals[w] += gb
    ws = sorted(totals)
    for a, b in zip(ws, ws[1:]):
        if totals[b] > 1.25 * totals[a] and totals[b] - totals[a] > 1000:
            ms.append({"t": int(datetime.combine(b, datetime.min.time(), TZ).timestamp() * 1000), "kind": "Ingest growth",
                       "what": f"{totals[a] / 1000:.1f} -> {totals[b] / 1000:.1f} TB/week", "weight": 0.5})
    return sorted(ms, key=lambda m: m["t"])


def fetch_ingest_by_type(since_ms, until_ms):
    rows = nr.nrql(f"FROM NrConsumption SELECT sum(GigabytesIngested) AS gb WHERE productLine = 'DataPlatform' "
                   f"FACET usageMetric, weekOf(timestamp) SINCE {since_ms} UNTIL {until_ms} LIMIT MAX WITH TIMEZONE '{Q.TZ}'")
    out = defaultdict(dict)
    for r in rows:
        out[r["facet"][0]][datetime.strptime(r["facet"][1].replace("Week of ", ""), "%B %d, %Y").date()] = r["gb"]
    return out


def fetch_nr_minutes(since_ms, until_ms):
    """New Relic staff UI activity for the whole PoV, as (user, hour start ms, active minutes): cheap enough to cover
    every week, and per person so an alias can be hidden in the report."""
    rows = nr.nrql(f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/60000)) AS m WHERE {Q.Q_NR} AND {Q.UI_ONLY} "
                   f"FACET user, floor(timestamp/3600000) SINCE {since_ms} UNTIL {until_ms} LIMIT MAX")
    return [(r["facet"][0], int(r["facet"][1]) * 3_600_000, r["m"]) for r in rows]


# ---------------- score ----------------

WEIGHTS = {"People": 25, "Depth": 25, "Breadth": 15, "Building": 15, "Momentum": 20}
K = {"People": 5, "Depth": 300, "Breadth": 5, "Building": 4, "Momentum": 3}  # value at which a part reaches 63% of its max
CAP_FULL_MINUTES = 30      # a capability counts fully after 30 recent minutes (brief touches count a little)
SUSTAIN_MIN_ENGAGED = 2    # a "sustained" week has at least this many engaged users


def engagement_score(ui_events, build_events, nr_minutes, as_of, logins=(), milestones=(), start_ms=None):
    """Engagement score 0-100 (v3). See README 'Engagement score'. Everything is up to as_of."""
    minutes = {(e["user"], e["timestamp"] // 60_000) for e in ui_events}
    real_minutes = {(e["user"], e["timestamp"] // 60_000) for e in ui_events if e["productCapabilityId"] not in TRIVIAL_CAPS}
    tier_map = tiers(ui_events, build_events, logins)
    best = {}
    for (u, w), t in tier_map.items():
        best[u] = max(best.get(u, 0), TIER_WEIGHT[t["tier"]] * decay(as_of - t["last"]))
    people_raw = sum(best.values())
    by_user, by_cap, seen_cap = Counter(), Counter(), set()
    depth_raw = 0.0
    for (u, m) in real_minutes:
        w = decay(as_of - m * 60_000)
        depth_raw += w
        by_user[u] += w
    for e in ui_events:
        k = (e["user"], e["timestamp"] // 60_000, e["productCapabilityId"])
        if k not in seen_cap and e["productCapabilityId"] not in TRIVIAL_CAPS:
            seen_cap.add(k)
            by_cap[e["productCapabilityId"]] += decay(as_of - k[1] * 60_000)
    breadth_raw = sum(min(1.0, v / CAP_FULL_MINUTES) for v in by_cap.values())
    # Dashboards count half: creating one is cheap; alerts and rules count fully.
    build_raw = sum((0.5 if (r["actionIdentifier"] or "").startswith("dashboard.") else 1.0) * decay(as_of - r["timestamp"]) for r in build_events)
    mdecay = lambda age: 0.5 ** (max(age, 0) / DAY_MS / MILESTONE_HALF_LIFE_DAYS)
    momentum_raw = sum(m["weight"] * mdecay(as_of - m["t"]) for m in milestones if m["t"] < as_of)

    raws = {"People": people_raw, "Depth": depth_raw, "Breadth": breadth_raw, "Building": build_raw, "Momentum": momentum_raw}
    parts = {k: WEIGHTS[k] * dr(v, K[k]) for k, v in raws.items()}
    base = sum(parts.values())

    top_user, top_w = (by_user.most_common(1) or [(None, 0)])[0]
    top_share = top_w / depth_raw if depth_raw else 0
    concentration = 1 - max(0.0, top_share - 0.5)
    since30 = as_of - 30 * DAY_MS
    p_min = sum(decay(as_of - m * 60_000) for (u, m) in minutes if since30 <= m * 60_000 < as_of)
    n_min = sum(v * decay(as_of - h) for _, h, v in nr_minutes if since30 <= h < as_of)
    share = p_min / (p_min + n_min) if p_min + n_min else 0
    share_factor = 0.5 + min(share, 0.5)
    # Sustained: how many of the last 3 weeks (this one included) had 2+ engaged users.
    wk = week_start(as_of - 1)
    recent_weeks = [wk - timedelta(days=7 * i) for i in range(3)]
    sustained_weeks = sum(1 for w in recent_weeks if sum(1 for (u, ww), t in tier_map.items() if ww == w and t["tier"] == "Engaged") >= SUSTAIN_MIN_ENGAGED)
    sustain_factor = 0.7 + 0.1 * sustained_weeks
    last_ms = max((m["t"] for m in milestones if m["t"] < as_of), default=None)
    stalled = bool(start_ms and as_of - start_ms >= STALL_AFTER_DAYS * DAY_MS and (last_ms is None or as_of - last_ms > STALL_WINDOW_DAYS * DAY_MS))
    stall_factor = STALL_FACTOR if stalled else 1.0
    score = base * concentration * share_factor * sustain_factor * stall_factor
    return {
        "score": round(score), "base": round(base, 1),
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "raw": {"people": round(people_raw, 2), "user-minutes": round(depth_raw, 1), "capabilities": round(breadth_raw, 2),
                "builds": round(build_raw, 2), "milestones": round(momentum_raw, 2)},
        "concentration": round(concentration, 2), "top_user": top_user, "top_share": round(top_share, 2),
        "share_factor": round(share_factor, 2), "share": round(share, 3),
        "sustain_factor": round(sustain_factor, 2), "sustained_weeks": sustained_weeks,
        "stall_factor": stall_factor, "last_milestone": last_ms,
    }


# Bands, calibrated on archetypes run through engagement_score (see README "How the bands were chosen").
BAND_STRONG = 45
BAND_PARTIAL = 15
BANDS_HOW = ("Sample PoVs run through the same formula: 8 engaged users x 1 h/week with builds and new sources = 65; 5 engaged x 40 min = 48; "
             "that strong team after a silent week = 45, after two = 24; 3 users or a New Relic-led PoV = 17; a 2-person team with an 80% champion = 8; "
             "15 new log-ins with 1 engaged user = 10 (and yellow); one champion alone = 2.")


def verdict(s):
    return (("good", "Strong: actively testing") if s >= BAND_STRONG else
            ("warning", "Partial: some real testing, gaps") if s >= BAND_PARTIAL else ("critical", "Weak: not really testing"))


READING_GUIDE = (f"0 means nobody from the prospect has done anything recently; 100 means many people using New Relic in depth, every week, "
                 f"across many capabilities, building their own dashboards and alerts and still adding data sources, with the prospect (not New Relic) doing the clicking. "
                 f"Recent activity counts most: a week-old minute is worth half, a two-week-old one a quarter. "
                 f"The colour next to the number is about quality, not headcount: green means people are really engaging, yellow means enrolled or thin, red means nobody engaged.")
WEEKLY_GUIDE = "Each week's score is computed as if it were the end of that week, using only activity up to then. Recent weeks count more, so the score falls when people stop."


# ---------------- quality colour and progression ----------------

def quality(engaged, active, new, converted, dropped_engaged=0):
    """(colour, reason). Driven by depth of interaction, not headcount; conversions to engaged count as progress."""
    if converted >= 3 and engaged >= 3:
        return "green", f"Green: {converted} users moved up to engaged ({engaged} engaged of {active} active)"
    if engaged >= 3 and engaged >= 0.4 * active:
        return "green", f"Green: {engaged} of {active} active users engaged"
    if engaged == 0:
        return "red", ("Red: nobody active" if active == 0 else f"Red: {active} active, none engaged")
    if new >= 5:
        return "yellow", f"Yellow: {new} new users enrolled, only {engaged} engaged"
    return "yellow", f"Yellow: {engaged} engaged of {active} active" + (f", {dropped_engaged} engaged user{'s' if dropped_engaged > 1 else ''} dropped back" if dropped_engaged else "")


def transitions(tier_map, weeks):
    """Per week: how users moved between tiers vs the previous week, and per-cohort conversion."""
    by_week = defaultdict(dict)
    for (u, w), d in tier_map.items():
        by_week[w][u] = d["tier"]
    first_seen = {}
    for w in sorted(by_week):
        for u in by_week[w]:
            first_seen.setdefault(u, w)
    out, prev = [], {}
    for w in weeks:
        cur = by_week.get(w, {})
        row = {"week": w, "new": 0, "new_engaged": 0, "up": 0, "to_engaged": 0, "same": 0, "down": 0, "dropped": 0, "dropped_engaged": 0,
               "engaged": sum(1 for t in cur.values() if t == "Engaged"), "active": len(cur)}
        for u, t in cur.items():
            if first_seen[u] == w:
                row["new"] += 1
                row["new_engaged"] += t == "Engaged"
                continue
            r0, r1 = TIER_RANK[prev.get(u)], TIER_RANK[t]
            if r1 > r0:
                row["up"] += 1
                row["to_engaged"] += t == "Engaged"
            elif r1 < r0:
                row["down"] += 1
            else:
                row["same"] += 1
        for u, t in prev.items():
            if u not in cur:
                row["dropped"] += 1
                row["dropped_engaged"] += t == "Engaged"
        row["colour"], row["reason"] = quality(row["engaged"], row["active"], row["new"], row["to_engaged"], row["dropped_engaged"])
        out.append(row)
        prev = cur
    cohorts = []
    for w in weeks:
        members = [u for u, f in first_seen.items() if f == w]
        if not members:
            continue
        ranks = {u: max(TIER_RANK[by_week[x].get(u)] for x in by_week if x >= w) for u in members}
        cohorts.append({"week": w, "size": len(members), "ever_light": sum(r >= 2 for r in ranks.values()),
                        "ever_engaged": sum(r >= 3 for r in ranks.values()),
                        "engaged_latest": sum(1 for u in members if by_week.get(weeks[-1], {}).get(u) == "Engaged")})
    return out, cohorts


def period_summary(weekly, start, end):
    """PoV-period score: average of weekly scores in [start, end], with min, max, trend and colour counts."""
    ws = [w for w in weekly if start <= w["week"] + timedelta(days=6) and w["week"] <= end]
    if not ws:
        return None
    sc = [w["score"] for w in ws]
    last = sc[-4:]
    n = len(last)
    slope = (sum((i - (n - 1) / 2) * (v - sum(last) / n) for i, v in enumerate(last)) / sum((i - (n - 1) / 2) ** 2 for i in range(n))) if n > 1 else 0
    colours = Counter(w["colour"] for w in ws)
    colour = "green" if colours["green"] * 2 >= len(ws) else "red" if colours["red"] * 2 >= len(ws) else "yellow"
    return {"weeks": len(ws), "mean": round(sum(sc) / len(sc)), "min": min(sc), "max": max(sc),
            "trend": "rising" if slope > 3 else "falling" if slope < -3 else "flat", "slope": round(slope, 1),
            "colour": colour, "colours": dict(colours),
            "reason": f"{colour.capitalize()}: {colours['green']} green, {colours['yellow']} yellow, {colours['red']} red weeks"}


def explain(sc):
    """Plain-language breakdown lines: [(label, points, max, why)], then multipliers [(factor, why)]."""
    raw = sc["raw"]
    parts = [
        ("People", sc["parts"]["People"], WEIGHTS["People"], f"about {raw['people']:.1f} recently active people: engaged count fully, light 30%, passive 5%"),
        ("Depth", sc["parts"]["Depth"], WEIGHTS["Depth"], f"{raw['user-minutes']:.0f} recent minutes of real use (full marks need hours per week)"),
        ("Breadth", sc["parts"]["Breadth"], WEIGHTS["Breadth"], f"{raw['capabilities']:.1f} capabilities used in depth recently (each needs ~30 minutes)"),
        ("Building", sc["parts"]["Building"], WEIGHTS["Building"], f"{raw['builds']:.1f} recent alerts, rules or dashboards built (a dashboard counts half)"),
        ("Momentum", sc["parts"]["Momentum"], WEIGHTS["Momentum"], f"{raw['milestones']:.1f} recent onboarding milestones: new data types, sources, ingest keys"),
    ]
    mult = [
        (sc["concentration"], f"{sc['top_user']} does {sc['top_share']:.0%} of recent activity" if sc["concentration"] < 1 else "activity is spread across people"),
        (sc["share_factor"], f"the prospect is only {sc['share']:.0%} of recent UI time; New Relic staff do the rest" if sc["share_factor"] < 1 else "the prospect does most of the clicking"),
        (sc["sustain_factor"], f"{sc['sustained_weeks']} of the last 3 weeks had {SUSTAIN_MIN_ENGAGED}+ engaged users"),
    ]
    if sc["stall_factor"] < 1:
        mult.append((sc["stall_factor"], f"no new data source or key for {STALL_WINDOW_DAYS}+ days, {STALL_AFTER_DAYS // 7}+ weeks into the PoV"))
    return parts, mult


# ---------------- whole-PoV views ----------------

def week_start(ms):
    d = datetime.fromtimestamp(ms / 1000, TZ).date()
    return d - timedelta(days=d.weekday())


def weekly_cohorts(ui_events, start_ms, as_of):
    weeks = []
    w = week_start(start_ms)
    while w <= week_start(as_of):
        weeks.append(w)
        w += timedelta(days=7)
    active = defaultdict(set)
    minutes = defaultdict(set)
    for e in ui_events:
        wk = week_start(e["timestamp"])
        active[wk].add(e["user"])
        minutes[wk].add((e["user"], e["timestamp"] // 60_000))
    seen, prev, rows = set(), set(), []
    for wk in weeks:
        a = active[wk]
        rows.append({"week": wk, "active": len(a), "new": len(a - seen), "returning": len(a & seen),
                     "lapsed": len(prev - a), "minutes": len(minutes[wk]), "who": sorted(a)})
        seen |= a
        prev = a
    return rows


def user_table(ui_events, all_events):
    first, last, mins, days = {}, {}, Counter(), defaultdict(set)
    for e in ui_events:
        u, t = e["user"], e["timestamp"]
        first[u] = min(first.get(u, t), t)
        last[u] = max(last.get(u, 0), t)
        days[u].add(datetime.fromtimestamp(t / 1000, TZ).date())
    for k in {(e["user"], e["timestamp"] // 60_000) for e in ui_events}:
        mins[k[0]] += 1
    any_seen = {e["user"] for e in all_events}
    return {u: {"first": first[u], "last": last[u], "minutes": mins[u], "days": len(days[u])} for u in first}, any_seen


def invite_funnel(audit, users, any_seen, as_of):
    created, deleted, logins = {}, set(), defaultdict(int)
    for r in audit:
        d = r.get("description") or ""
        if r["actionIdentifier"] == "user.create":
            m = re.search(r"User (\S+@\S+) was created", d)
            if m and m.group(1).endswith("@" + Q.DOMAIN) and r["timestamp"] > created.get(m.group(1), {}).get("at", 0):
                created[m.group(1)] = {"by": r["actorEmail"] if r["actorEmail"] != "New Relic Employee" else f"NR user {r['actorId']}", "at": r["timestamp"]}
        elif r["actionIdentifier"] == "user.delete":
            m = re.search(r"User (\S+@\S+) was deleted", d)
            if m:
                deleted.add((m.group(1), r["timestamp"]))
        elif r["actionIdentifier"] == "user.login" and (r["actorEmail"] or "").endswith("@" + Q.DOMAIN):
            logins[r["actorEmail"]] += 1
    # Deleted and never re-created users aren't invitees any more (e.g. a typo'd address replaced by the right one).
    for u, t in deleted:
        if u in created and created[u]["at"] < t and u not in users:
            del created[u]
    rows = []
    for u in sorted(set(created) | set(users)):
        info = users.get(u)
        status = ("active" if info and as_of - info["last"] < LAPSED_DAYS * DAY_MS else
                  "lapsed" if info else "logged in, never used the UI" if logins.get(u) or u in any_seen else "never activated")
        rows.append({"user": u, "invited_by": created.get(u, {}).get("by", "(not in audit: account owner or before the PoV)"),
                     "invited_at": created.get(u, {}).get("at"), "logins": logins.get(u, 0),
                     "last": info["last"] if info else None, "minutes": info["minutes"] if info else 0, "status": status})
    funnel = {"invited": len(created), "ever_logged_in": sum(1 for u in created if logins.get(u) or u in users),
              "ever_used_ui": sum(1 for u in created if u in users),
              "active_now": sum(1 for r in rows if r["status"] == "active"), "seen_total": len(users)}
    return rows, funnel


def assets(audit, all_events, as_of):
    built = [r for r in audit if (r["actorEmail"] or "").endswith("@" + Q.DOMAIN) and is_build(r)]
    dash_creators = {r["targetId"]: r["actorEmail"] for r in built if r["actionIdentifier"] == "dashboard.create"}
    views = defaultdict(lambda: {"creator": 0, "others": set(), "other_views": 0, "last": None, "weeks": Counter()})
    for e in all_events:
        g = e.get("source.dashboardId")
        if g in dash_creators:
            v = views[g]
            if e["user"] == dash_creators[g]:
                v["creator"] += 1
            else:
                v["other_views"] += 1
                v["others"].add(e["user"])
            v["last"] = max(v["last"] or 0, e["timestamp"])
            v["weeks"][week_start(e["timestamp"])] += 1
    names = dashboard_names(set(dash_creators))
    dash_rows = []
    for g, who in dash_creators.items():
        v = views[g]
        created_at = min(r["timestamp"] for r in built if r["targetId"] == g)
        dash_rows.append({"guid": g, "name": names.get(g, "(deleted or not visible)"), "creator": who, "created": created_at,
                          "creator_views": v["creator"], "other_views": v["other_views"], "other_viewers": len(v["others"]),
                          "last_view": v["last"], "weeks_viewed": len(v["weeks"]),
                          "revisited": bool(v["last"] and v["last"] - created_at > DAY_MS)})
    return built, sorted(dash_rows, key=lambda x: -(x["creator_views"] + x["other_views"]))


NRQL_NOISE = re.compile(r"(?i)\s+(SINCE|UNTIL)\s+(\d+\s+\w+\s+ago|'[^']*'|\d+|today|yesterday|now)|\s+LIMIT\s+\w+|\s+TIMESERIES(\s+(\d+\s+\w+|AUTO|MAX))?|\s+WITH TIMEZONE\s+'[^']*'|\s+COMPARE WITH\s+\d+\s+\w+\s+ago")
# Query-builder autocomplete and schema lookups that the UI runs on the user's behalf.
UI_GENERATED = re.compile(r"(?i)^SHOW\b|\bkeyset\(\)|latest\(description\).*FACET metricName|as 'item'|^SELECT uniques\(")
KEYWORDS = {"and", "or", "not", "is", "null", "in", "like", "where", "facet", "select", "from", "as", "true", "false", "since", "limit",
            "timeseries", "order", "by", "desc", "asc", "with", "timezone", "rlike", "cases", "extrapolate", "max"}
STANDARD_ATTRS = {"appname", "host", "hostname", "name", "duration", "error", "timestamp", "entity.guid", "entity.name", "message", "level",
                  "service.name", "http.statuscode", "request.uri", "transactionname", "traceid", "span.kind", "entityguid", "metricname",
                  "cluster_name", "clustername", "namespacename", "podname", "containername", "eventtype", "awsregion", "region", "status"}


def handwritten(events):
    """Queries a person typed or scripted: query builder, NerdGraph API with a key; Ask AI kept but labelled."""
    out = []
    for e in events:
        f = e.get("productFeature") or ""
        if f == "Query your data" or e["channel"] == "API key" or "Ask AI" in f:
            out.append(dict(e, origin="Ask AI" if "Ask AI" in f else "API key" if e["channel"] == "API key" else "Query builder"))
    return out


def digest(hw):
    per = defaultdict(lambda: {"queries": Counter(), "origins": Counter(), "event_types": Counter(), "attrs": Counter()})
    for e in hw:
        q = NRQL_NOISE.sub("", " ".join((e.get("query") or "").split())).strip()
        if not q or UI_GENERATED.search(q):
            continue
        p = per[e["user"]]
        p["queries"][q] += 1
        p["origins"][e["origin"]] += 1
    for u, p in per.items():
        for q in p["queries"]:
            for m in re.findall(r"(?i)\bFROM\s+([A-Za-z_][\w:]*(?:\s*,\s*[A-Za-z_][\w:]*)*)", q):
                for t in m.split(","):
                    p["event_types"][t.strip()] += 1
            bare = re.sub(r"'(?:[^'\\]|\\.)*'", "''", q)  # drop string literals: they're values, not attributes
            for clause in re.findall(r"(?i)\b(?:WHERE|FACET)\b(.*?)(?=\bFACET\b|\bWHERE\b|\bORDER BY\b|$)", bare):
                for a in re.findall(r"`([^`]+)`|\b([A-Za-z_][\w.]*)\b(?!\s*\()", clause):
                    a = a[0] or a[1]
                    if a.lower() not in KEYWORDS and not a.isdigit() and not re.fullmatch(r"\d+(\.\d+)?", a):
                        p["attrs"][a] += 1
    return per


def criteria():
    """Hook for item 8: PoV success criteria. criteria.json = [{"name", "capabilities": [...], "event_types": [...]}]."""
    if CRITERIA_FILE.exists():
        return json.loads(CRITERIA_FILE.read_text())
    return None


def actions(as_of, users, funnel_rows, weekly, ingest, sc, dash_rows, share_30, milestones=()):
    """Rule-based next steps for the SE. Each rule is one line in the README."""
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d")
    out = []
    last_any = max((u["last"] for u in users.values()), default=None)
    idle_days = (as_of - last_any) / DAY_MS if last_any else None
    if idle_days is None or idle_days >= 7:
        who = max(users, key=lambda u: users[u]["last"]) if users else None
        n_lapsed = sum(1 for r in funnel_rows if r["status"] == "lapsed")
        out.append(("critical", (f"No prospect UI activity for {idle_days:.0f} days (last: {who}, {fmt(last_any)}); all {n_lapsed} past users have lapsed."
                                 if last_any else "No prospect UI activity at all."),
                    "Book a check-in this week: is the PoV paused, blocked, or lost? Then call the top past users (see People)."))
    if sc["top_share"] > 0.5 and sc["top_user"]:
        tu = users.get(sc["top_user"], {})
        status = f"inactive since {fmt(tu['last'])}" if tu and as_of - tu["last"] >= LAPSED_DAYS * DAY_MS else "still active"
        out.append(("critical" if "inactive" in status else "warning",
                    f"Champion at risk: {sc['top_user']} is {sc['top_share']:.0%} of recent activity, {status}.",
                    "Find a second hands-on owner; ask the champion who else should be testing."))
    never = [r["user"] for r in funnel_rows if r["status"] == "never activated"]
    if never:
        out.append(("warning", f"{len(never)} invitees never activated: {', '.join(never[:8])}{'...' if len(never) > 8 else ''}.",
                    "Send them a short getting-started note with one task each, or confirm they aren't evaluators."))
    lapsed = sorted((r for r in funnel_rows if r["status"] == "lapsed"), key=lambda r: -r["minutes"])
    if lapsed and not (idle_days is None or idle_days >= 7):  # otherwise already part of the inactivity line
        out.append(("warning", f"{len(lapsed)} users lapsed (no UI use in {LAPSED_DAYS}+ days): "
                    + ", ".join(f"{r['user']} ({fmt(r['last'])})" for r in lapsed[:6]) + ("..." if len(lapsed) > 6 else "") + ".",
                    "Call the top ones: what did they test, what stopped them?"))
    full_weeks = [w for w in sorted(ingest) if w + timedelta(days=7) <= datetime.fromtimestamp(as_of / 1000, TZ).date()]
    if full_weeks:
        lw = full_weeks[-1]
        active_lw = next((w["active"] for w in weekly if w["week"] == lw), 0)
        if ingest[lw] > 100 and active_lw <= 1:
            out.append(("critical", f"Data flowing, nobody looking: {ingest[lw] / 1000:.1f} TB ingested in the week of {lw:%m-%d}, {active_lw} prospect users active.",
                        "Show them what this data answers (one dashboard or alert per use case), or reduce ingest."))
    if share_30 is not None and share_30 < 0.25:
        out.append(("warning", f"New Relic does most of the clicking: prospect share of UI time is {share_30:.0%} (30d).",
                    "Hand over: next sessions should be the prospect driving, the SE watching."))
    stale = [d for d in dash_rows if not d["last_view"] or as_of - d["last_view"] > LAPSED_DAYS * DAY_MS]
    if stale:
        out.append(("warning", f"{len(stale)} of {len(dash_rows)} prospect-built dashboards not viewed in {LAPSED_DAYS}+ days.",
                    "Ask whether they answer the right questions; offer to rework one together."))
    if sc.get("stall_factor", 1) < 1:
        last = sc.get("last_milestone")
        out.append(("warning", f"Onboarding stalled: no new data type, source or ingest key since {fmt(last) if last else 'the start'}.",
                    "Agree the next data source to add (another service, browser, synthetics), or confirm scope is complete."))
    if criteria() is None:
        out.append(("info", "PoV success criteria aren't mapped yet (criteria.json missing).", "Add the criteria to score progress per criterion."))
    return out


def analyze(as_of, window_days=30, human=("UI",)):
    """Fetch and compute everything once, anchored to as_of. Used by report.py and build_dashboard.py."""
    start = pov_start_ms()
    since = as_of - window_days * DAY_MS
    pov_events = fetch_events(Q.Q_USER, start, as_of)
    nr_window = fetch_events(Q.Q_NR, since, as_of)
    audit = fetch_audit(start, as_of)
    ingest = fetch_ingest_weekly(start, as_of)
    pov_ui = [e for e in pov_events if e["channel"] in human]
    nr_ui = [e for e in nr_window if e["channel"] in human]
    builds = [r for r in audit if (r["actorEmail"] or "").endswith("@" + Q.DOMAIN) and is_build(r)]
    logins = [r for r in audit if r["actionIdentifier"] == "user.login" and (r["actorEmail"] or "").endswith("@" + Q.DOMAIN)]
    nr_minutes = fetch_nr_minutes(start, as_of)
    ingest_by_type = fetch_ingest_by_type(start, as_of)
    milestones = fetch_milestones(audit, ingest_by_type, start, as_of)
    score_at = lambda t: engagement_score([e for e in pov_ui if e["timestamp"] < t], [b for b in builds if b["timestamp"] < t], nr_minutes, t,
                                          [r for r in logins if r["timestamp"] < t], [m for m in milestones if m["t"] < t], start)
    sc = score_at(as_of)
    tier_map = tiers(pov_ui, builds, logins)
    weekly = weekly_cohorts(pov_ui, start, as_of)
    trans, cohorts = transitions(tier_map, [w["week"] for w in weekly])
    for w, tr in zip(weekly, trans):  # score as of the end of each week, to show the trend
        t = min(int(datetime.combine(w["week"] + timedelta(days=7), datetime.min.time(), TZ).timestamp() * 1000), as_of)
        w["detail"] = score_at(t)
        w["score"] = w["detail"]["score"]
        w["gb"] = ingest.get(w["week"], 0)
        w["tiers"] = {k: sorted(u for (u, wk), d in tier_map.items() if wk == w["week"] and d["tier"] == k) for k in TIERS}
        w["transition"] = tr
        w["colour"], w["reason"] = tr["colour"], tr["reason"]
    # Current quality: the last 7 days as one bucket, compared with the 7 days before.
    last7 = tiers([e for e in pov_ui if e["timestamp"] >= as_of - 7 * DAY_MS], [b for b in builds if b["timestamp"] >= as_of - 7 * DAY_MS],
                  [r for r in logins if r["timestamp"] >= as_of - 7 * DAY_MS], bucket=lambda ms: "now")
    eng7 = sum(1 for d in last7.values() if d["tier"] == "Engaged")
    seen_before = {u for (u, w) in tier_map if datetime.combine(w, datetime.min.time(), TZ).timestamp() * 1000 < as_of - 7 * DAY_MS}
    new7 = sum(1 for (u, _) in last7 if u not in seen_before)
    sc["colour"], sc["reason"] = quality(eng7, len(last7), new7, 0)
    sc["reason"] = sc["reason"].replace("Red: nobody active", "Red: nobody from the prospect active in the last 7 days")
    end = datetime.fromisoformat(POV_END).date() if POV_END else datetime.fromtimestamp(as_of / 1000, TZ).date()
    period = period_summary(weekly, datetime.fromisoformat(POV_START).date(), end)
    users, any_seen = user_table(pov_ui, pov_events)
    funnel_rows, funnel = invite_funnel(audit, users, any_seen, as_of)
    built, dash_rows = assets(audit, pov_events, as_of)
    um = lambda evs: len({(e["user"], e["timestamp"] // 60_000) for e in evs if e["timestamp"] >= since})
    p, n = um(pov_ui), um(nr_ui)
    share_window = p / (p + n) if p + n else None
    acts = actions(as_of, users, funnel_rows, weekly, ingest, sc, dash_rows, share_window, milestones)
    return {"as_of": as_of, "since": since, "start": start, "pov_events": pov_events, "nr_window": nr_window, "audit": audit,
            "ingest": ingest, "pov_ui": pov_ui, "builds": builds, "score": sc, "weekly": weekly, "users": users, "tiers": tier_map,
            "funnel_rows": funnel_rows, "funnel": funnel, "built": built, "dash_rows": dash_rows,
            "share_window": share_window, "actions": acts, "milestones": milestones, "cohorts": cohorts, "period": period,
            "pov_end": end, "ingest_by_type": ingest_by_type}
