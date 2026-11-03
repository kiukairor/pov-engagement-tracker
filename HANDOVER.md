> READ-ONLY COPY: dashboard create/update code was removed and `nr.py` refuses all mutations. Mentions of deploy below describe the original project.

# HANDOVER — PoV engagement dashboard (Agent 1)

Last updated: 2026-10-28. Read this first, then `README.md` (run instructions, NRQL, score v3
formula, report architecture). Don't re-crawl the code unless a task needs it.

## Mission
Show how engaged a prospect is with New Relic during a PoV, so the SE knows how confident to be
that the prospect is testing properly. Test account: **<account-id>, US region**.

## Rules
- **"Run a PoV engagement analysis" = `report.py` only.** Show the local report (same layout as the
  last version). **Nothing is ever created or updated in the account under study.** This is
  enforced in code: mutations are blocked in `nr.py`, and there is no default account.
- New account: set `NR_ACCOUNT_ID`, `NR_REGION` and `PROSPECT_DOMAIN`.
- The key lives only in `.env` here (gitignored): `NR_API_KEY`, etc. Never print it or copy
  it into files or reports. Never open `credentials-DO-NOT-MIGRATE/`.
- PII: `out/` (report.json, report.data.js) and `dashboard.json` hold prospect emails and query
  text, and are gitignored. `report.html` itself is PII-free, but it is useless without
  `report.data.js`. Don't forward those outside NR.
- Don't create events-to-metrics rules or other account objects without user approval.
- Charts follow the `dataviz` skill. Render-check after every deploy (NerdGraph), and use headless
  Chrome screenshots at 1440 and 760 for the report.

## What exists (all deployed / working as of 2026-10-28)
- **Dashboard JSON (local only):** `build_dashboard.py` writes `dashboard.json`; nothing is deployed.
- **Report:** `report.py` writes `out/report.json` and `report.data.js`; `report_template.html` is
  the app. The browser recomputes everything and checks itself against Python (the "browser =
  Python" badge). It has:
  - a one-screen summary
  - a sticky nav
  - a hide-users filter (localStorage; recomputes the score)
  - collapsed-by-default tables and appendices, with lazy rendering
  - PoV period date inputs
  - a merged weekly chart (score panel above the people-by-tier panel)
  - a compact progression chart
- **Data sources:**
  - NrdbQuery (activity; channel split UI / API key / AI-MCP; human = UI)
  - NrAuditEvent (logins, config changes; NR staff masked, mapped via actorId)
  - NrConsumption (ingest)
  - EntityAudits (first entities)
  - No page-view or UI session data exists in NRDB.
- **Score v3 (0–100):**
  - Parts: People (tier-weighted 1 / 0.3 / 0.05), Depth, Breadth, Building, Momentum (milestones
    with a 14-day half-life).
  - Multipliers: concentration, prospect share, sustained, onboarding stall.
  - Bands: Strong 45+, Partial 15–44, Weak <15.
  - The quality colour (green/yellow/red, with a reason) is separate from the number.
  - Full formula is in the README.
- **Tiers per user-week:** Engaged = 10+ real minutes, a hand-written query or a build;
  Light = 3–9 minutes; Passive = less.

## prospect results (as of 2026-10-27/28)
- **Scores:** now 3/100, Weak, red. PoV period 08-17 → 10-27: average 16/100, falling, yellow.
- **Weekly scores v3:** 11, 30, 34, 20, 22 (the only green week), 15, 21, 13, 8, 4, 3.
- **Activity:**
  - No prospect activity since 2026-10-13.
  - the champion = 67% of activity (champion at risk).
  - 31 invited, 20 ever active, 0 active now, 11 never activated.
  - The prospect is only ~3–8% of UI time; NR staff (the user) do the rest.
- **Ingest vs use:** ~12 TB/week still ingested with nobody looking. Onboarding has stalled since
  2026-09-29.
- A bug fixed in v3: NR staff minutes were previously fetched for 30 days only.

## OPEN — waiting on the user
1. Merge the NR alias `<se>+prospect+noalert@newrelic.com` with the main identity? (Lead
   suggestion: yes.)
2. Share thresholds (50% / 25%): OK?
3. Exclude dashboard views from the prospect share by default? (Lead suggestion: no, show both.)
4. Count Ask AI/MCP as human activity? (Currently excluded; affects another user.)
5. PoV success criteria: create `criteria.json` (`[{"name", "capabilities"}]`) to enable item 8,
   mapping activity to success criteria.
6. Later: events-to-metrics rules for retention over 30 days (candidates are in the README;
   sessions can't be rebuilt from metrics).
7. Later: a Nerdpack (custom NR app) version if this becomes a team tool. The reviewer recommends
   it over a hosted web app.

## Write safety
- `nr.py` refuses every NerdGraph mutation; there is no code path that writes to the account.
- There is no default account.

Still optional:
- a separate read-only key for report.py and `--check`

## Resume prompt (paste into a fresh session)
> Read ./HANDOVER.md and act
> as Agent 1. Answers to the open items: <paste, or "ask me">. Then: <next request>.

## /pengage skill (2026-10-28)
`~/.claude/skills/pengage/SKILL.md` is the user's entry point. It asks for the account, then
keeps settings per account in `accounts/<id>.env` (gitignored, 600; loaded via `PENGAGE_ENV`),
and runs only the read-only `report.py --out out/<id>/report.html`. prospect is
`accounts/<account-id>.env`. The old top-level `.env` still works for backward compatibility.
