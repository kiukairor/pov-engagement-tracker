"""NRQL used by the dashboard and the HTML report. One place to change the prospect filter.

Prospect users are identified by email domain:
  NrdbQuery.user and NrAuditEvent.actorEmail (NR employees show up as 'New Relic Employee'
  in NrAuditEvent and as @newrelic.com in NrdbQuery, so a positive domain match excludes them).

Channels (NrdbQuery), verified on prospect:
  API key     source.userApiKeyId set: a script/tool calling NerdGraph with that user's key
  AI / MCP    source.name 'nerdgraph' without a key: Ask AI chat or the New Relic MCP server
  UI          everything else (New Relic User Interface, Distributed Tracing, errors-inbox, dashboard, other)
"Human activity" = UI only. APM/infra agents ingest with license keys and never appear in NrdbQuery.

Cohorts: Prospect (@DOMAIN) vs New Relic (@newrelic.com in NrdbQuery; actorEmail 'New Relic Employee' in
NrAuditEvent, where individuals are only distinguishable by actorId). Queries with no user (alerts, system,
anonymous API) belong to neither and are left out of the comparison. The engagement score stays prospect-only.
"""
import os

DOMAIN = os.environ.get("PROSPECT_DOMAIN", "prospect.example.com")
TZ = os.environ.get("PROSPECT_TZ", "UTC")

Q_USER = f"user LIKE '%@{DOMAIN}'"
UI_ONLY = "source.userApiKeyId IS NULL AND (source.name IS NULL OR source.name != 'nerdgraph')"
Q_HUMAN = f"{Q_USER} AND {UI_ONLY}"
Q_NON_UI = f"{Q_USER} AND NOT ({UI_ONLY})"
CHANNEL = ("if(source.userApiKeyId IS NOT NULL, 'API key', if(source.name = 'nerdgraph', 'AI / MCP', 'UI'))")
NR_DOMAIN = os.environ.get("NR_STAFF_DOMAIN", "newrelic.com")
Q_NR = f"user LIKE '%@{NR_DOMAIN}'"
Q_BOTH_HUMAN = f"({Q_USER} OR {Q_NR}) AND {UI_ONLY}"
COHORT = f"if({Q_USER}, 'Prospect', 'New Relic')"  # only used together with Q_BOTH_* filters
A_USER = f"actorEmail LIKE '%@{DOMAIN}'"
A_NR = "actorEmail = 'New Relic Employee'"
A_COHORT = f"if({A_USER}, 'Prospect', 'New Relic')"
A_ORIGIN = "if(actorType = 'api_key', 'API key', 'UI')"
# trial_* are account provisioning events logged against NR system users, not anyone's work.
A_CHANGE_BOTH = (f"({A_USER} OR {A_NR}) AND actionIdentifier NOT IN ('user.login','user.logout') "
                 "AND actionIdentifier NOT LIKE 'trial_%' AND isReadAudit IS NOT true")
# Config changes = audit events by prospect users that aren't sessions or reads.
A_CHANGE = f"{A_USER} AND actionIdentifier NOT IN ('user.login','user.logout') AND isReadAudit IS NOT true"


# (title, viz, nrql, column, row, width, height, filters_dashboard)
# filters_dashboard=True links the widget to its own dashboard: clicking a facet (user) filters every widget.
WIDGETS = [
    ("Active prospect users (UI)", "viz.billboard",
     f"FROM NrdbQuery SELECT uniqueCount(user) AS 'Last 7 days' WHERE {Q_HUMAN} SINCE 7 days ago COMPARE WITH 1 week ago", 1, 4, 3, 3, False),
    ("Active users, last 30 days (UI)", "viz.billboard",
     f"FROM NrdbQuery SELECT uniqueCount(user) AS 'Users', uniqueCount(floor(timestamp/86400000)) AS 'Active days', "
     f"uniqueCount(user, floor(timestamp/60000)) AS 'Active user-minutes' WHERE {Q_HUMAN} SINCE 30 days ago", 4, 4, 5, 3, False),
    ("Logins (NrAuditEvent user.login)", "viz.billboard",
     f"FROM NrAuditEvent SELECT filter(count(*), WHERE actionIdentifier='user.login') AS 'Logins 30d', "
     f"uniqueCount(actorEmail) AS 'Users with a login/logout 30d' WHERE {A_USER} AND actionIdentifier IN ('user.login','user.logout') SINCE 30 days ago",
     9, 4, 4, 3, False),

    ("Daily active prospect users (UI), hover a segment for the name", "viz.stacked-bar",
     f"FROM NrdbQuery SELECT uniqueCount(user) WHERE {Q_HUMAN} FACET user SINCE 30 days ago TIMESERIES 1 day WITH TIMEZONE '{TZ}' LIMIT 50", 1, 7, 7, 4, False),
    ("Who was active on which day (UI)", "viz.table",
     f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/60000)) AS 'Active minutes', count(*) AS 'Queries' WHERE {Q_HUMAN} "
     f"FACET dateOf(timestamp) AS 'Day', user SINCE 30 days ago WITH TIMEZONE '{TZ}' LIMIT MAX", 8, 7, 5, 4, True),

    ("Active user-minutes per day by channel (UI = human; API key / AI-MCP separate)", "viz.line",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE {Q_USER} FACET {CHANNEL} AS 'Channel' SINCE 30 days ago TIMESERIES 1 day WITH TIMEZONE '{TZ}'", 1, 11, 7, 3, False),
    ("Channel mix per user (30d, active minutes)", "viz.table",
     f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/60000)) AS 'Active minutes', count(*) AS 'Queries', uniqueCount(floor(timestamp/86400000)) AS 'Days' "
     f"WHERE {Q_USER} FACET user, {CHANNEL} AS 'Channel' SINCE 30 days ago LIMIT 100", 8, 11, 5, 3, True),

    ("Per-user activity (30d, UI)", "viz.table",
     f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/86400000)) AS 'Active days', uniqueCount(floor(timestamp/3600000)) AS 'Active hours', "
     f"uniqueCount(floor(timestamp/60000)) AS 'Active minutes', uniqueCount(productCapabilityId) AS 'Capabilities', count(*) AS 'Queries', "
     f"latest(timestamp) AS 'Last seen' WHERE {Q_HUMAN} FACET user SINCE 30 days ago LIMIT 50", 1, 14, 8, 4, True),
    ("Where they work: UI source (30d)", "viz.pie",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE {Q_HUMAN} FACET source.name SINCE 30 days ago", 9, 14, 4, 4, False),

    ("Capabilities used (30d, UI, active user-minutes)", "viz.bar",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) AS 'Active user-minutes' WHERE {Q_HUMAN} FACET productCapabilityId SINCE 30 days ago LIMIT 25", 1, 18, 6, 4, False),
    ("Features by user (30d, UI)", "viz.table",
     f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/60000)) AS 'Active minutes', count(*) AS 'Queries' WHERE {Q_HUMAN} FACET user, productCapabilityId, productFeature SINCE 30 days ago LIMIT 100", 7, 18, 6, 4, True),

    ("Activity by weekday and hour (30d, UI, local time)", "viz.heatmap",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE {Q_HUMAN} FACET weekdayOf(timestamp), hourOf(timestamp) SINCE 30 days ago WITH TIMEZONE '{TZ}' LIMIT MAX", 1, 22, 6, 4, False),
    ("Data they queried (30d, UI, top event types)", "viz.bar",
     f"FROM NrdbQuery SELECT count(*) WHERE {Q_HUMAN} FACET query.eventType SINCE 30 days ago LIMIT 15", 7, 22, 6, 4, False),

    ("Prospect share of human activity, 30d", "viz.billboard",
     f"FROM NrdbQuery SELECT filter(uniqueCount(user, floor(timestamp/60000)), WHERE {Q_USER}) * 100 / uniqueCount(user, floor(timestamp/60000)) AS 'Prospect % of UI user-minutes' "
     f"WHERE {Q_BOTH_HUMAN} SINCE 30 days ago", 1, 33, 3, 3, False),
    ("Prospect share of human activity, 7d", "viz.billboard",
     f"FROM NrdbQuery SELECT filter(uniqueCount(user, floor(timestamp/60000)), WHERE {Q_USER}) * 100 / uniqueCount(user, floor(timestamp/60000)) AS 'Prospect % of UI user-minutes' "
     f"WHERE {Q_BOTH_HUMAN} SINCE 7 days ago", 4, 33, 3, 3, False),
    ("Prospect share of config changes (30d)", "viz.billboard",
     f"FROM NrAuditEvent SELECT filter(count(*), WHERE {A_USER}) * 100 / count(*) AS 'Prospect % of all changes', "
     f"filter(count(*), WHERE {A_USER} AND actorType != 'api_key') * 100 / filter(count(*), WHERE actorType != 'api_key') AS 'Prospect % of UI changes' "
     f"WHERE {A_CHANGE_BOTH} SINCE 30 days ago", 7, 33, 3, 3, False),
    ("Active people by cohort (UI, 30d)", "viz.billboard",
     f"FROM NrdbQuery SELECT uniqueCount(user) WHERE {Q_BOTH_HUMAN} FACET {COHORT} AS 'Cohort' SINCE 30 days ago", 10, 33, 3, 3, False),
    ("Daily active user-minutes: prospect vs New Relic (UI)", "viz.line",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE {Q_BOTH_HUMAN} FACET {COHORT} AS 'Cohort' SINCE 30 days ago TIMESERIES 1 day WITH TIMEZONE '{TZ}'", 1, 36, 6, 3, False),
    ("Daily active people: prospect vs New Relic (UI)", "viz.line",
     f"FROM NrdbQuery SELECT uniqueCount(user) WHERE {Q_BOTH_HUMAN} FACET {COHORT} AS 'Cohort' SINCE 30 days ago TIMESERIES 1 day WITH TIMEZONE '{TZ}'", 7, 36, 6, 3, False),
    ("Capabilities by cohort (UI, 30d, active user-minutes)", "viz.bar",
     f"FROM NrdbQuery SELECT uniqueCount(user, floor(timestamp/60000)) WHERE {Q_BOTH_HUMAN} FACET productCapabilityId, {COHORT} AS 'Cohort' SINCE 30 days ago LIMIT 40", 1, 39, 6, 5, False),
    ("Channel mix by cohort and person (30d)", "viz.table",
     f"FROM NrdbQuery SELECT uniqueCount(floor(timestamp/60000)) AS 'Active minutes', count(*) AS 'Queries', uniqueCount(floor(timestamp/86400000)) AS 'Days' "
     f"WHERE {Q_USER} OR {Q_NR} FACET {COHORT} AS 'Cohort', user, {CHANNEL} AS 'Channel' SINCE 30 days ago LIMIT 100", 7, 39, 6, 5, True),
    ("Config changes by cohort and origin (NrAuditEvent, 30d; NR people by actorId)", "viz.table",
     f"FROM NrAuditEvent SELECT count(*) AS 'Changes' WHERE {A_CHANGE_BOTH} FACET {A_COHORT} AS 'Cohort', {A_ORIGIN} AS 'Origin', "
     f"if({A_USER}, actorEmail, concat('NR user ', actorId)) AS 'Who', actionIdentifier SINCE 30 days ago LIMIT 100", 1, 44, 12, 4, False),

    ("API-key and AI/MCP queries (not counted as human)", "viz.table",
     f"FROM NrdbQuery SELECT count(*) AS 'Queries', uniqueCount(floor(timestamp/86400000)) AS 'Days', min(timestamp) AS 'First', max(timestamp) AS 'Last' "
     f"WHERE {Q_NON_UI} FACET user, {CHANNEL} AS 'Channel', productFeature SINCE 30 days ago LIMIT 100", 1, 26, 12, 3, True),

    ("All config changes by prospect users, incl. admin (NrAuditEvent, 30d)", "viz.table",
     f"FROM NrAuditEvent SELECT count(*) AS 'Changes', latest(timestamp) AS 'Last' WHERE {A_CHANGE} FACET actorEmail, actionIdentifier SINCE 30 days ago LIMIT 100", 1, 29, 6, 4, False),
    ("Logins, logouts and session expiries (30d; expiries are not activity)", "viz.table",
     f"FROM NrAuditEvent SELECT timestamp, actorEmail, if(description LIKE '%has expired%', 'session expired (not activity)', actionIdentifier) AS 'Event', "
     f"aparse(description, '%Session Id: *') AS 'Session Id' "
     f"WHERE {A_USER} AND actionIdentifier IN ('user.login','user.logout') SINCE 30 days ago LIMIT 100", 7, 29, 6, 4, False),
]

# Short enough for NRQL's 256-char RLIKE limit; same intent as pov.BUILD_ACTION.
BUILD_RLIKE = (r"'dashboard\\.(create|update.*|add_.*)|alerts\\..*\\.(create|update)|(e2m|nrql_drop)_rule\\.create|"
               r"account\\.(create|update)_log_.*|entity_created|synthetic.*|workflow.*|notifications\\..*'")


# Activity tiers (same cut-offs as pov.tiers): real = not homepage/admin/plan pages; hand-written = query builder, not autocomplete.
REAL_CAP = "productCapabilityId IS NOT NULL AND productCapabilityId NOT IN ('HOMEPAGE','NR_UI','MANAGE_YOUR_PLAN','ADMINISTRATION')"
HANDWRITTEN = "productFeature = 'Query your data' AND query NOT LIKE 'SHOW %' AND query NOT LIKE '%keyset()%'"
TIER_INNER = (f"SELECT filter(uniqueCount(floor(timestamp/60000)), WHERE {REAL_CAP}) AS 'real_min', filter(count(*), WHERE {HANDWRITTEN}) AS 'hw', count(*) AS 'n' "
              f"FROM NrdbQuery WHERE {Q_HUMAN} FACET user")
# Nested TIMESERIES emits a zero row for every user in every week, so Passive needs n > 0 (at least one query that week).
TIER_CASES = "cases(WHERE real_min >= 10 OR (hw > 0 AND real_min >= 3) AS 'Engaged', WHERE real_min >= 3 OR hw > 0 AS 'Light', WHERE n > 0 AS 'Passive')"


def pov_widgets(start, until, dash_guids, row):
    """Whole-PoV widgets. start/until are 'YYYY-MM-DD' (until = the Monday after as-of, so weekly buckets align with the report)."""
    S = f"SINCE '{start} 00:00:00' UNTIL '{until} 00:00:00' WITH TIMEZONE '{TZ}'"
    dash_in = ", ".join(f"'{g}'" for g in dash_guids) or "'none'"
    return [
        (f"Weekly prospect users by tier (Engaged / Light / Passive; whole PoV from {start})", "viz.stacked-bar",
         f"SELECT count(*) FROM ({TIER_INNER} TIMESERIES 1 week LIMIT MAX) FACET {TIER_CASES} {S} TIMESERIES 1 week", 1, row, 6, 3, False),
        ("Weekly ingest, TB (NrConsumption DataPlatform, whole PoV)", "viz.line",
         f"FROM NrConsumption SELECT sum(GigabytesIngested) / 1000 AS 'TB ingested' WHERE productLine = 'DataPlatform' {S} TIMESERIES 1 week", 7, row, 6, 3, False),
        ("Engaged users, last 7 days (live)", "viz.billboard",
         f"SELECT count(*) AS 'Engaged users' FROM ({TIER_INNER} LIMIT MAX) WHERE real_min >= 10 OR (hw > 0 AND real_min >= 3) SINCE 7 days ago", 1, row + 3, 4, 3, False),
        ("Users by tier, last 7 days (live)", "viz.billboard",
         f"SELECT count(*) FROM ({TIER_INNER} LIMIT MAX) FACET {TIER_CASES} SINCE 7 days ago", 5, row + 3, 4, 3, False),
        ("People ever engaged in a week (whole PoV)", "viz.billboard",
         f"SELECT uniqueCount(user) AS 'Ever engaged' FROM ({TIER_INNER} TIMESERIES 1 week LIMIT MAX) WHERE real_min >= 10 OR (hw > 0 AND real_min >= 3) {S}", 9, row + 3, 4, 3, False),
        ("First and last seen per prospect user (UI, whole PoV)", "viz.table",
         f"FROM NrdbQuery SELECT min(timestamp) AS 'First seen', max(timestamp) AS 'Last seen', uniqueCount(floor(timestamp/604800000)) AS 'Active weeks', "
         f"uniqueCount(floor(timestamp/60000)) AS 'Active minutes' WHERE {Q_HUMAN} FACET user {S} LIMIT 100", 1, row + 6, 7, 4, True),
        ("Invites: users created (NrAuditEvent user.create)", "viz.table",
         f"FROM NrAuditEvent SELECT latest(timestamp) AS 'Created' WHERE actionIdentifier = 'user.create' AND description LIKE '%@{DOMAIN}%' "
         f"FACET actorEmail AS 'Invited by', aparse(description, 'User * was created%') AS 'Invitee' {S} LIMIT 100", 8, row + 6, 5, 4, False),
        ("Hand-written NRQL per user (counts only; text is in the local report)", "viz.table",
         f"FROM NrdbQuery SELECT uniqueCount(query) AS 'Distinct queries', count(*) AS 'Runs', latest(timestamp) AS 'Last' "
         f"WHERE {Q_USER} AND (productFeature = 'Query your data' OR productFeature LIKE '%Ask AI%' OR source.userApiKeyId IS NOT NULL) "
         f"AND query NOT LIKE 'SHOW %' AND query NOT LIKE '%keyset()%' FACET user {S} LIMIT 100", 1, row + 10, 6, 4, True),
        ("Build actions by prospect users (no admin; whole PoV)", "viz.table",
         f"FROM NrAuditEvent SELECT count(*) AS 'Actions', latest(timestamp) AS 'Last' WHERE {A_USER} AND actionIdentifier RLIKE {BUILD_RLIKE} "
         f"FACET actorEmail, actionIdentifier {S} LIMIT 100", 7, row + 10, 6, 4, False),
        ("Do they come back? Views of prospect-built dashboards, by week (whole PoV)", "viz.table",
         f"FROM NrdbQuery SELECT count(*) AS 'Queries', uniqueCount(user) AS 'Viewers', latest(timestamp) AS 'Last view' "
         f"WHERE source.dashboardId IN ({dash_in}) FACET source.dashboardId, weekOf(timestamp) {S} LIMIT 100", 1, row + 14, 12, 3, False),
    ]


MARKDOWN = f"""### PoV engagement: `@{DOMAIN}` users
Who on the prospect side is using New Relic, how often, and on what. Sources: **NrdbQuery** (every query a user's UI page or API call runs, attributed by `user`) and **NrAuditEvent** (logins, logouts and config changes). NR employees are excluded by the domain filter.

**Human activity = UI only.** Queries made with a user API key (scripts, Terraform, tools) and Ask AI / MCP queries are attributed to the key owner, so they're shown separately as *API key* and *AI / MCP*, never in the score. **Click a user** in any table with a user column to filter the whole dashboard to that person.

The **score and action panel** above are snapshots computed by `build_dashboard.py` at deploy time (NRQL can't weight recent activity more); they show their own as-of time. Live widgets are relative to when you open the dashboard. Weekly users are split into **Engaged** (10+ real minutes, or 3+ with a hand-written query), **Light** (3-9 min) and **Passive** (under 3 min). The report also counts builds and logins.

*Active minutes* count distinct minutes with at least one query, which is a lower bound on time spent. Sessions and the per-session list aren't possible in NRQL: run `report.py`."""
