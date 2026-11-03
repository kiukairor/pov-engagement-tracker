# PoV engagement report

Shows how engaged a prospect is with New Relic during a PoV: who is using it, how often, on what,
an explainable 0-100 **engagement score**, the whole-PoV trend, the invite funnel, what they built and queried, and
an SE action panel. The question it answers: *can we be confident they're testing properly?*

It is **read-only**: it only queries the prospect's New Relic account (NerdGraph/NRQL) and writes local files.
It never creates or changes anything in the account (`nr.py` refuses every mutation).

## Getting started

Requirements: Python 3.9+ (standard library only, nothing to install) and a New Relic **user API key**
(`NRAK-...`) with access to the prospect's account.

**1. Create a `.env` file** next to the scripts. It is gitignored, so it is never committed.

```sh
NR_API_KEY=NRAK-...                      # user key with access to the account
NR_ACCOUNT_ID=1234567                    # the prospect's account ID (required, there is no default)
PROSPECT_DOMAIN=prospect.example.com     # email domain that identifies the prospect's users
NR_REGION=US                             # optional, EU for EU accounts (default US)
PROSPECT_TZ=UTC                          # optional, timezone for day/hour charts and Monday-based weeks
POV_START=2026-08-17                     # optional, PoV start date (default 2026-08-17)
POV_END=2026-11-15                       # optional, PoV end date (default: now)
```

**2. Run the report**

```sh
python3 report.py
```

It takes a few minutes on large accounts. It writes `out/report.html`, `out/report.json` and `out/report.data.js`.

**3. Open it**

```sh
open out/report.html     # macOS; keep report.data.js in the same folder, the page needs it
```

### Options and other accounts

```sh
python3 report.py --days 30 --gap 30                     # detail window and session gap (defaults)
python3 report.py --pov-start 2026-09-01 --pov-end 2026-10-15
python3 report.py --channels "UI,API key,AI / MCP" --out out/report_all_channels.html   # count every channel, for comparison
python3 build_dashboard.py --check                       # optional: run the widget queries and report DATA / ZEROS / EMPTY / ERROR
```

To use it for another PoV, either edit `.env`, or keep one settings file per account and point to it:
`PENGAGE_ENV=accounts/<id>.env python3 report.py --out out/<id>/report.html`.

`out/` holds prospect emails and query text and is gitignored. Don't share it outside New Relic.
`pov.py` holds the whole-PoV logic (`pov.analyze()`), `queries.py` the NRQL, `report.py` + `report_template.html` the report.
`build_dashboard.py` only writes a local `dashboard.json` (a NerdGraph `DashboardInput` you could import by hand); it is not needed for the report.

## Data sources (verified in account <account-id>, 2026-10-27)

| Event type | Useful for | Key attributes |
|---|---|---|
| `NrdbQuery` | **Main activity signal.** Every query run by a UI page, dashboard, or API call, with the user | `user` (email), `productCapabilityId` (APM, LOGS, TRACES...), `productFeature`, `source.name` (New Relic User Interface / nerdgraph / Distributed Tracing / dashboard...), `source.dashboardId`, `query.eventType`, `timestamp` |
| `NrAuditEvent` | **Logins, logouts, session expiry, config changes** | `actionIdentifier` (`user.login`, `user.logout`, `alerts.condition.*`, `dashboard.*`, `e2m_rule.*`, `api_key_service.*`...), `actorEmail`, `actorType`, `description` (contains `Session Id: <uuid>`), `isReadAudit` |
| `NrComputeUsage` | CCU attributed per user (`dimension_email`), dashboard names | For prospect it shows only E2M rule compute attributed to the rule creator. That's not human activity, so it isn't used |
| `NrConsumption`, `NrMTDConsumption`, `NrUsage`, `NrDailyUsage` | Ingest/billing | No per-user activity, so not used |
| `NrIntegrationError` | Ingest/integration errors | Not engagement data |

Login activity events **do** exist: `NrAuditEvent` `user.login` / `user.logout`. Limits:
- NR employees appear as `actorEmail = 'New Relic Employee'`, and as `@newrelic.com` in `NrdbQuery`.
  The positive domain filter excludes both.
- Logins are rare: sessions last days, and many logouts are "session has expired". So login count understates usage. `NrdbQuery` is the better daily-activity signal.
- Login events often have no Session Id, so login-to-logout pairing (native session lifetime) rarely matches. In the last 30 days, 0 prospect sessions paired.
- There is **no page-view / UI session event** in NRDB. Sessions are estimated from `NrdbQuery` gaps.

## Human vs API activity (attribution)

Does an agent or script using someone's key count as that person's activity? It depends on the key:
- **APM/infra agents** send data with a license/ingest key. That's ingest, which never appears in `NrdbQuery`, so it's never counted.
- **A script, Terraform or tool using a person's user API key** *is* attributed to that person: `user = <owner>`,
  `source.name = 'nerdgraph'`, and `source.userApiKeyId` set. UI queries never carry `source.userApiKeyId`.
- **Ask AI and the New Relic MCP server** also arrive as `source.name = 'nerdgraph'` with the user set, but no key.

So every query gets a channel (`queries.CHANNEL`):

| Channel | Rule | Counted as human? |
|---|---|---|
| UI | no key, `source.name` is not `nerdgraph` (New Relic User Interface, Distributed Tracing, errors-inbox, dashboard, other) | **yes** (default) |
| AI / MCP | `source.name = 'nerdgraph'`, no key | no, shown separately |
| API key | `source.userApiKeyId IS NOT NULL` | no, shown separately |

The score, sessions, daily users, capabilities and heatmap all use **UI only**. The dashboard has a per-day channel line,
a channel-mix table and an "API-key and AI/MCP queries" table. The report has `--channels` to change what counts.

prospect, last 30 days:
- the champion: UI 85 active minutes / 1,295 queries on 11 days; AI/MCP 7 min / 18 queries (Ask AI and MCP); API key 4 min / 20 queries,
  all in about 5 minutes on 2026-10-01.
- another user: AI/MCP 20 queries (MCP server), UI 1 query.
- Everyone else is UI only. No prospect user has `source.name = 'dashboard'` queries, so there's no dashboard auto-refresh.
- Effect of UI-only on sessions: the champion goes from 18 to **19** sessions, because one 102-minute mixed session on 2026-10-01 splits into
  UI sessions of 8, 26 and 37 min once the API/MCP queries are removed. His estimated time goes from 255 to **176 min**, and active days stay at 11.
  another user goes from 2 to 1 sessions. The scores are unchanged (7d 0, 30d 88), because the champion's days are all UI-active.
- Per-session list: no session is flagged as repetitive (auto-refresh). The longest UI session is 46 min, with 23 active minutes and 178 distinct of 314 queries.

## Prospect vs New Relic staff (comparison, not in the score)

How NR staff appear:
- **NrdbQuery**: real emails (`@newrelic.com`), so individuals can be told apart. In prospect: `<se>@newrelic.com` and the alias
  `<se>+prospect+noalert@newrelic.com`. These are the same human, but they count as 2 "people".
- **NrAuditEvent**: `actorEmail` is masked as `'New Relic Employee'`, but `actorId` is per person (1008405205, 1008701874...), and
  that person's own login/logout `description` contains the email. The report uses this to map actorId to email; the dashboard shows `NR user <actorId>`.
  actorIds 1005947234 and 1005952438 only emit `trial_start`/`trial_end` (provisioning), so `trial_*` is excluded from changes.
- Queries with no `user` at all (about 19k in 30 days, `nerdgraph`/`other`: alerts, system, anonymous API) belong to neither cohort and are left out.

The same UI-only rule applies to both cohorts, and NrAuditEvent changes are split by origin (`actorType = 'api_key'` vs user).
The dashboard's cohort section (bottom) has these widgets:
- prospect share of UI user-minutes, for 30d and 7d
- prospect share of config changes, all and UI-only
- people per cohort
- daily user-minutes and daily people per cohort
- capabilities by cohort
- channel mix by cohort/person
- config changes by cohort, origin and person

The report has the same widgets, plus small multiples on one scale, a per-cohort session row and flagged NR sessions.
Share rule of thumb: prospect >= 50% good, 25-49% "New Relic does most of the work", below 25% "New Relic does almost all the clicking".

prospect, last 30 days (UI only):

| Cohort | People | UI user-minutes | Est. sessions | Median session | Config changes UI / API key |
|---|---|---|---|---|---|
| Prospect | 6 | 135 | 29 | 2 min | 7 / 0 |
| New Relic | 2 (1 human) | 1,567 | 88 | 19 min | 71 / 154 |

- **Prospect share: 8% of UI user-minutes** (7.6% in NRQL; the report and NRQL day boundaries differ slightly), **0% in the last 7 days**,
  and 3% of all config changes (9% of UI-made ones).
- Leaving out dashboard views, the share is still only 11%. 12 of the 88 NR sessions are flagged, mostly as dashboards left open (auto-refresh).



- **Dashboard:** "Daily active prospect users" is a stacked bar faceted by user (each segment = one user, so hover shows the name).
  Next to it, "Who was active on which day" is a `FACET dateOf(timestamp), user` table.
  Five user-faceted tables are linked to the dashboard itself, so **clicking a row filters the whole dashboard** to that facet.
- **Report:** click a column in either daily chart to list the names (with active minutes, or with the channel for API/AI) under it.

## NRQL used

Prospect filter: `user LIKE '%@prospect.example.com'` (NrdbQuery), `actorEmail LIKE '%@prospect.example.com'` (NrAuditEvent).
Human filter (added to most widgets): `source.userApiKeyId IS NULL AND (source.name IS NULL OR source.name != 'nerdgraph')`.
All widget queries are in `queries.py` (`WIDGETS`). Main ones:

```sql
-- active users 7d (vs previous week)
FROM NrdbQuery SELECT uniqueCount(user) WHERE user LIKE '%@prospect.example.com' SINCE 7 days ago COMPARE WITH 1 week ago
-- daily active users
FROM NrdbQuery SELECT uniqueCount(user) WHERE user LIKE '%@prospect.example.com' SINCE 30 days ago TIMESERIES 1 day WITH TIMEZONE 'UTC'
-- effort proxy: distinct (user, minute) pairs with at least one query
FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE user LIKE '%@prospect.example.com' FACET productCapabilityId SINCE 30 days ago
-- per user
FROM NrdbQuery SELECT uniqueCount(floor(timestamp/86400000)) AS 'Active days', uniqueCount(floor(timestamp/3600000)) AS 'Active hours',
  uniqueCount(productCapabilityId) AS 'Capabilities', latest(timestamp) AS 'Last seen' WHERE user LIKE '%@prospect.example.com' FACET user SINCE 30 days ago
-- config changes
FROM NrAuditEvent SELECT count(*) WHERE actorEmail LIKE '%@prospect.example.com' AND actionIdentifier NOT IN ('user.login','user.logout')
  AND isReadAudit IS NOT true FACET actorEmail, actionIdentifier SINCE 30 days ago
-- logins with session id
FROM NrAuditEvent SELECT timestamp, actorEmail, actionIdentifier, aparse(description, '%Session Id: *')
  WHERE actorEmail LIKE '%@prospect.example.com' AND actionIdentifier IN ('user.login','user.logout') SINCE 30 days ago
```

## Engagement score (v3)

**The number** measures how much real testing is happening, with recent activity counting most. **The colour** next to it measures
quality (depth of interaction), not headcount. 15 first-time log-ins raise the number a little but stay **yellow** ("enrolled, not yet
engaged"). When those users start engaging, both the number and the colour improve.

### Parts (points out of max)

All usage inputs are recency-weighted (0.5^(age / 7 days)). Each part is max x (1 - e^(-x/k)), which is 63% of the max at k and 95% at 3k, with no caps.

| Part | Max | Input | k |
|---|---|---|---|
| People | 25 | each user once, by their best recent week: engaged 1.0, light 0.3, passive 0.05 | 5 |
| Depth | 25 | recent real user-minutes (homepage/admin/plan pages excluded) | 300 |
| Breadth | 15 | per capability min(1, recent minutes / 30); brief touches count a little | 5 |
| Building | 15 | recent builds; alerts and rules 1, dashboards 0.5 | 4 |
| Momentum | 20 | onboarding milestones, fading with a 14-day half-life: ingest key created, first data of a type, first entity of a type, ingest-growth week (0.5) | 3 |

### Multipliers (each with a reason in the UI)
- **Concentration**: 1 - max(0, top-user share - 0.5). One person doing everything costs up to x0.5.
- **Share**: 0.5 + min(prospect share of recent UI time vs New Relic staff, 0.5). New Relic doing all the clicking costs up to x0.5.
- **Sustained**: 0.7 + 0.1 x the number of the last 3 weeks with 2+ engaged users. High scores need weeks of multi-person depth, not one busy week.
- **Stall**: x0.85 once the PoV is 6+ weeks old with no new milestone for 21 days. Flat ingest from the same sources stops being green.

### Quality colour (per week, for now = last 7 days, and for the period)
- **Green**: 3+ engaged users who are at least 40% of active users, or 3+ users who moved up to engaged.
- **Red**: nobody engaged.
- **Yellow**: in between. The label gives the reason, e.g. "Yellow: 12 new users enrolled, only 3 engaged".

The period colour is green or red if at least half its weeks are, otherwise yellow.

### Bands
**45-100 strong**, **15-44 partial**, **0-14 weak**. Sample PoVs run through `engagement_score`:
- 8 engaged users x 1 h/week with builds and new sources: 65
- 5 engaged x 40 min: 48
- the 65-point team after one silent week: 45; after two: 24
- 3 users, or a New Relic-led PoV: 17
- 2 people with an 80% champion: 8
- 15 new log-ins + 1 engaged user: 10, and yellow
- one champion alone: 2

### Why the old weekly scores (55-72) were too high, and what changed

v2's peak week (w/c 08-31, 65 with tiers) decomposed as People 19.9 + Depth 13.9 + Breadth 15.6 + Building 15.1, x1.0 x1.0, for a
week with 109 team minutes, of which 4-5 people were briefly engaged. The causes:

1. **Bug: the share factor was 1.0 for every week before 09-28.** New Relic staff activity was only fetched for the last 30 days, so
   early weeks looked prospect-led. In fact the prospect was 23% of UI time in w/c 08-31 and only 2-6% from 09-07 on. Fix: NR minutes
   are now fetched for the whole PoV (`fetch_nr_minutes`, hourly buckets).
2. **Constants too low, so modest activity saturated.** 109 minutes earned 56% of Depth, and capabilities touched for 10 minutes earned
   full Breadth. Fix: Depth k 120 -> 300 (full marks need hours a week), a capability needs 30 recent minutes, Breadth max 20 -> 15.
3. **Passive and light users counted too much.** Weights went from 0.1/0.5 to 0.05/0.3. A 2-minute visit with one typed query is now Light, not Engaged.
4. **One-off dashboard creation kept Building high.** Dashboards now count half; Building max 25 -> 15.
5. **No requirement for sustained activity.** The new Sustained multiplier (x0.7-1.0) rewards weeks in a row with 2+ engaged users.
6. **Onboarding wasn't time-sensitive.** The new Momentum part rewards early keys, data types and sources, fades, and is followed by a stall penalty.

prospect weekly scores, before -> after:

| Week | 08-17 | 08-24 | 08-31 | 09-07 | 09-14 | 09-21 | 09-28 | 10-05 | 10-12 | 10-19 | 10-26 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| v2 | 15 | 44 | 65 | 48 | 57 | 45 | 48 | 25 | 14 | 8 | 7 |
| v3 | 11 | 30 | 34 | 20 | 22 | 15 | 21 | 13 | 8 | 4 | 3 |
| colour | yellow | yellow | yellow | yellow | green | yellow | yellow | yellow | red | red | red |

No prospect week is "strong" any more. The best was 34 (partial). The only green week, w/c 09-14 (4 of 8 active users engaged), still
scores 22, because New Relic staff did 98% of the clicking that month.
**Now: 3/100, weak, red** ("nobody from the prospect active in the last 7 days"). Subtotal 11.9 x0.83 (the champion 67%) x0.54 (prospect 4%) x0.70 (no sustained weeks) x0.85 (stalled since 09-29).

### PoV-period score
`POV_START` / `POV_END` (in `.env`, or `report.py --pov-start/--pov-end`). The period score is the average of the weekly scores in the period,
with lowest/highest, trend (slope of the last 4 weeks: rising > +3/week, falling < -3/week) and the period colour.
The report has two date inputs that recompute all of this in the browser from the embedded weekly data, and dim/hide weeks outside
the period. The dashboard shows the configured period in the snapshot.
prospect 08-17 to 10-27: **average 16, lowest 3, highest 34, falling, yellow** (1 green, 7 yellow, 3 red weeks).

### Progression
The report shows, per week:
- how many users were new (and how many of them engaged), moved up (to engaged), stayed, moved down or dropped (were engaged)
- an enrolment-cohort table: size, ever light+, ever engaged, engaged now

the prospect's biggest cohort (w/c 08-24, 12 users) produced 4 people who were ever engaged and 0 engaged now.

### Onboarding milestones
These come from:
- `NrAuditEvent api_key_service.ingest_key.create` by prospect users
- first week with data per `NrConsumption usageMetric`
- the first `EntityAudits` CREATION per entity domain/type, decoded from the GUID prefix and excluding dashboards, alerts and pipeline rules, which are building
- weeks where ingest grew more than 25% and more than 1 TB

prospect had 15 milestones: Metrics data and ingest keys in week 1, infra/Prometheus/CoreDNS entities on 08-20, Logging and Tracing on 08-30,
ingest growth to 19 TB/week by 09-13, and the last ones on 09-29 (an ingest key by avivk, and the first APM application). Nothing new since, hence the stall.

## Activity tiers (per user, per week)

- **Engaged**: 10+ real minutes, or 3+ real minutes with a hand-written query (query builder, autocomplete excluded) or a build
- **Light**: 3-9 real minutes, or a typed query or build in a shorter visit
- **Passive**: under 3 real minutes, or a login with no queries

Calibrated on the prospect's 49 user-weeks. Real minutes cluster at 1-2 (25), 3-7 (8) and 12+ (16), with nothing between 7 and 12, hence the cut-offs at 3 and 10.
prospect engaged users per week: 1, 3, 4, 2, 4, 1, 2, 1, 0, 0, 0. 8 of 21 were ever engaged.
The dashboard computes tiers in NRQL with nested aggregation (`queries.TIER_INNER` / `TIER_CASES`). It matches the report for Engaged and Light;
it can't see builds or login-only weeks, so Passive can be 1 lower than in the report.

## Report (out/report.html)

**Architecture.** `report.py` fetches and pre-computes, then writes:
- `out/report.json`: all the data, including prospect emails and query text
- `out/report.data.js`: the same JSON as `window.REPORT = ...`, because browsers block `fetch()` on `file://` pages
- `out/report.html`: a copy of `report_template.html`, which contains no data and no PII

`out/` is gitignored. In the browser, the page recomputes tiers, the engagement score (a JavaScript port of `pov.engagement_score`),
the weekly series, the period score and the KPIs. On load it checks itself against Python's numbers: the header shows
"browser = Python", or a mismatch warning. After a template-only change, copy the template into `out/` and reload; no refetch is needed.

**Layout.** There's a sticky nav (Summary / People / What they did / Data / Appendix), plus **Hide users**, Expand all, Collapse all and Theme.
- **Summary** (fits one screen at 1440 and 760): the score number and band, the quality colour with its reason, and the top 3 plain reasons.
  "Show the arithmetic" and "ⓘ How to read this score" are collapsed. Next to it: the top 5 actions (3 on narrow screens; "Show all"),
  then 4 KPIs: active users in the last 7 days, last prospect activity, prospect share of UI time, and ingest vs use last week.
- **People**:
  - one combined chart: a score panel (0-100, dots coloured by quality, band lines) above a people-by-tier panel, sharing the week axis.
    These are two panels, not a dual axis.
  - the PoV period, shown as "16 / 100 average (Partial band is 15-44; Strong is 45+)", with date inputs
  - a progression chart: new and moved-up above the line, moved-down and dropped-out below
  - the invite funnel
  - collapsed: week-by-week detail, enrolment cohorts, and who to call
- **What they did**: the daily active users chart (the only daily chart), the capabilities chart (the only one; top 10 plus all), and collapsed
  sections for when they work (heatmap), the hand-written NRQL digest (one section per user) and what they built.
- **Data**: ingest vs use (two panels on the same weeks), the milestone timeline, and the collapsed weekly detail table.
- **Appendix** (all collapsed): New Relic staff vs prospect, every estimated session, API-key and AI/MCP activity, channel mix,
  all config changes, logins and native sessions, and how the report works.

Every card can be collapsed. Tables are ranked: the top 3 rows are highlighted, the first 5 are shown, and "Show all" reveals the rest.
Collapsed sections are rendered the first time they're opened.

**Hide users.** Pick users (prospect or New Relic staff, e.g. a test user or the `+prospect+noalert` alias). They disappear from every chart,
table and KPI, and the score is **recomputed** in the browser. The action list is generated by Python, so it stays unfiltered, and
says so. The choice is saved in `localStorage` per prospect domain.
Example: hiding the champion takes the current score from 3 to 2.

## Whole-PoV views (since POV_START)

- **Weekly cohorts** (Monday weeks, local time): active, new (first week seen), returning (seen before), lapsed (active the
  week before, not this week), UI user-minutes, ingest, score. prospect weekly active users: 2, 13, 11, 3, 8, 4, 5, 2, 1, 0, 0.
  21 distinct prospect users ever, vs 6 in the last 30 days.
- **Invite funnel** (`NrAuditEvent user.create` / `user.delete` descriptions name the invitee): invited, ever logged in, ever used the UI,
  active now (UI use in the last 14 days), plus a "who to call" list. A deleted-then-never-active address (e.g. a typo replaced by
  the right one) is dropped. prospect: 31 invited (all by one user), 20 ever used the UI, **0 active now**, 11 never activated.
- **Prospect-built assets**: build actions by prospect users, and for each dashboard they created, views by the creator vs others
  (`NrdbQuery source.dashboardId`), weeks viewed and last view. prospect: 5 dashboards. "Kubernetes / Compute Resources" (one user) was
  viewed by 4 others; none of the 5 has been viewed since 2026-09-14, and 2 were never opened. Also 2 E2M rules, a pipeline cloud rule and log pipeline settings.
- **Ingest vs usage**: weekly TB from `NrConsumption` (`productLine = 'DataPlatform'`, the cheapest per-week source) next to weekly
  active users. Flag "data flowing, nobody looking" = last full week > 100 GB and at most 1 prospect user active.
  prospect: 11.8 TB in the week of 10-19 with 0 users.
- **Hand-written NRQL digest (local report only)**: query builder (`productFeature = 'Query your data'`), NerdGraph with a user API key,
  and Ask AI (labelled), per user. Autocomplete and schema lookups (`SHOW ...`, `keyset()`, metric pickers) are excluded. Time clauses are
  stripped before de-duplicating. Shows the top queries, the event types, and the attributes used in WHERE/FACET (string literals removed;
  "likely custom" = not in a small list of standard attributes). The dashboard shows **counts only**.
- **SE action panel**: rule-based lines, each with a suggested next step (in `pov.actions`):
  no prospect UI activity for 7+ days; champion at risk (top user > 50% of recent activity, flagged critical if they've lapsed);
  never-activated invitees; lapsed users (14+ days); data flowing, nobody looking; New Relic share above 75%;
  prospect dashboards not viewed for 14+ days; success criteria not mapped.
- **PoV success criteria (hook, item deferred)**: put a `criteria.json` next to the scripts:
  `[{"name": "Log search at scale", "capabilities": ["LOGS"]}, {"name": "K8s visibility", "capabilities": ["KUBERNETES", "INFRA"]}]`.
  The report then shows prospect UI minutes and users per criterion. Mapping to NRQL proofs comes once the criteria are provided.

## Sessions (report.py only, ESTIMATE)

Per user, `NrdbQuery` events are sorted and split where the gap is more than 30 minutes (`--gap`). Session length
= last query - first query, minimum 1 minute. This is a **lower bound**: reading a page without triggering
queries isn't seen, and a single-page visit counts as about 1 minute.
The report lists every session (user, start, end, duration, active minutes, queries, distinct queries, capabilities) and flags them:
- **repetitive: possible auto-refresh**: 30+ minutes and under 20% distinct queries.
- **long, mostly idle**: 60+ minutes with activity in under 25% of its minutes.

"Active min" counts the calendar minutes touched, so it can be 1 more than a short session's duration.

## Results on prospect (2026-10-27)

- 30 days: 6 prospect users, 11 active days, 19 UI capabilities (21 counting API/AI channels), 7 config changes (4 of them admin),
  ~29 estimated sessions (median 2 min). Score v3 (whole PoV, decayed) **3**; v2 said 8 and v1 said 88.
- 7 days: **no prospect activity** (last query 2026-10-13, by user@prospect.example.com). The only prospect audit events in the last 7 days
  are "session has expired" logouts, which the dashboard and report label as non-activity.
- Activity is concentrated in one user (the champion: 18 of 29 sessions, 11 active days). The others were each active on 1-2 days.

## Retention and metrics

- Queryable data starts 2026-08-17, the account creation date (first `trial_start` audit event). 71 days are available today,
  so the 30-day window is comfortable. The real retention ceiling for `NrdbQuery`/`NrAuditEvent` can't be observed yet.
  Neither is listed in the account's `dataManagement.customizableRetention` namespaces. They likely follow
  platform-defined retention; check the docs before relying on more than ~90 days.
- **No metric equivalent exists.** `Metric` has no `NrdbQuery`/audit/user-activity series (only `newrelic.resourceConsumption.*`,
  `newrelic.goldenmetrics.*`, `newrelic.error.group.*`), and none of the 8 existing E2M rules cover usage data.
- For long retention you'd have to create your own **events-to-metrics rules** (not created, per the brief). E2M supports only
  `count`/`summary`/`distribution`/`uniqueCount` with `FACET`. Candidates:
  `SELECT uniqueCount(user) AS 'pov.users.active' FROM NrdbQuery WHERE user NOT LIKE '%@newrelic.com' FACET productCapabilityId`
  and `SELECT count(*) AS 'pov.audit.changes' FROM NrAuditEvent WHERE actionIdentifier NOT IN ('user.login','user.logout') FACET actionIdentifier`.
  Facetting by `user` keeps per-user detail but adds cardinality (fine for a handful of PoV users). Sessions can't be built from metrics.

## Known limits

- Users are identified by email domain only. Contractors or personal emails on other domains are missed: add them to the filter.
- `NrdbQuery` misses UI actions that don't query NRDB, and API calls that aren't NRQL.
- NR dashboards can't filter by clicking a time bucket, so a day-click can't filter the dashboard. Use the day x user table or the stacked-bar hover.
  Clicking a row in a multi-facet table (day + user) filters on the row's facets: confirm in the UI that it does what you want.
- Ask AI / MCP is excluded from "human" even though a person probably typed the prompt. Add it with `--channels "UI,AI / MCP"` if you want it counted.
- Dashboard widgets with no prospect activity show 0 (correct for the prospect's last 7 days, not a bug).
- The render-check runs each deployed widget's NRQL via NerdGraph. It confirms data, not visual layout in the UI.
- `out/` contains prospect user emails and is gitignored. Don't commit it.
