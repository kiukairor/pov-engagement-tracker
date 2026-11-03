"""Generate the PoV engagement dashboard JSON locally and render-check every widget's NRQL (read-only).

This copy never creates or updates a dashboard in New Relic.

  python3 build_dashboard.py            # write dashboard.json only
  python3 build_dashboard.py --check    # also run every widget's NRQL and report rows returned (read-only)
"""
import argparse
import json
from pathlib import Path

import time
from datetime import datetime, timedelta

import nr
import pov
import queries

HERE = Path(__file__).parent
NAME = f"PoV engagement - {queries.DOMAIN}"
HEADER_ROWS = 4      # intro markdown
SCORE_ROWS = 9       # score snapshot (columns 1-5) and "how to read" (columns 6-8), top-left
SCORE_COLS = 5
SIDE_COLS = 4        # action panel width, right-hand columns 9-12
PANEL_MIN_ROWS = 16  # keep stacking widget rows beside the panel until it's at least this tall


QDOT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def score_markdown(A):
    """Score in plain language, computed now. NRQL can't weight recent activity more, so this is a stamped snapshot."""
    sc, f, prd = A["score"], A["funnel"], A["period"]
    as_of = datetime.fromtimestamp(A["as_of"] / 1000, pov.TZ)
    parts, mult = pov.explain(sc)
    rows = "\n".join(f"| {name} | **{pts:.1f}** / {mx} | {why} |" for name, pts, mx, why in parts)
    mrows = "\n".join(f"| × {fct:.2f} | | {why} |" for fct, why in mult)
    weeks = " · ".join(f"{w['week']:%b %d} {QDOT[w['colour']]} {w['score']}" for w in A["weekly"])
    period = (f"**PoV-period score ({pov.POV_START} to {A['pov_end']:%Y-%m-%d}):** average **{prd['mean']}**, lowest {prd['min']}, highest {prd['max']}, "
              f"trend {prd['trend']} · {QDOT[prd['colour']]} {prd['reason']}" if prd else "")
    return (f"## Engagement score now: **{sc['score']} / 100**, {pov.verdict(sc['score'])[1]}\n"
            f"{QDOT[sc['colour']]} **{sc['reason']}**  \n*As of {as_of:%Y-%m-%d %H:%M} {queries.TZ}. The colour is quality (depth of interaction), not headcount.*\n\n"
            f"| Part | Points | Why |\n|---|---|---|\n{rows}\n| **Subtotal** | **{sc['base']:.1f}** / 100 | |\n{mrows}\n| **Score** | **{sc['score']}** / 100 | |\n\n"
            f"{period}  \n"
            f"**Score at the end of each week** (as if computed then; recent weeks count more): {weeks}  \n"
            f"**Engaged users per week:** {' · '.join(str(len(w['tiers']['Engaged'])) for w in A['weekly'])} · "
            f"**Invites:** {f['invited']} invited, {f['ever_used_ui']} ever used the UI, {f['active_now']} active now")


def guide_markdown():
    return (f"### How to read this score\n{pov.READING_GUIDE}\n\n"
            f"| Score | Meaning |\n|---|---|\n| **{pov.BAND_STRONG}-100** | Strong: several people testing in depth every week |\n"
            f"| **{pov.BAND_PARTIAL}-{pov.BAND_STRONG - 1}** | Partial: real testing, but thin, one-person or New Relic-led |\n"
            f"| **0-{pov.BAND_PARTIAL - 1}** | Weak: a champion alone, glances, or nobody lately |\n\n"
            f"*How the bands were chosen:* {pov.BANDS_HOW} The exact formula is in the README.")


def actions_markdown(A):
    as_of = datetime.fromtimestamp(A["as_of"] / 1000, pov.TZ)
    mark = {"critical": "🔴", "warning": "🟠", "info": "🔵", "good": "🟢"}
    acts = "\n\n".join(f"{mark[lvl]} **{msg}**  \n{nxt}" for lvl, msg, nxt in A["actions"])
    return f"### SE action panel\n*As of {as_of:%Y-%m-%d %H:%M}*\n\n{acts}"


def row_groups(widgets):
    """Group widget specs by their row; each group is one horizontal band of the grid."""
    groups = {}
    for spec in widgets:
        groups.setdefault(spec[4], []).append(spec)
    return [groups[r] for r in sorted(groups)]


def fit(widths, total):
    """Scale widths to sum to total (largest remainder, each >= 1)."""
    raw = [w * total / sum(widths) for w in widths]
    out = [max(1, int(x)) for x in raw]
    for k in sorted(range(len(raw)), key=lambda k: raw[k] - int(raw[k]), reverse=True):
        if sum(out) >= total:
            break
        out[k] += 1
    while sum(out) > total:
        out[out.index(max(out))] -= 1
    return out


def build(account_id, A=None):
    """A = pov.analyze() result for the snapshots and whole-PoV widgets.
    Layout: score top-left, action panel down the right-hand side, every other widget reflowed into bands with no gaps."""
    main = 12 - SIDE_COLS
    md = lambda title, text, col, row, w, h: {"title": title, "layout": {"column": col, "row": row, "width": w, "height": h},
                                              "visualization": {"id": "viz.markdown"}, "rawConfiguration": {"text": text}}
    widgets = [md("", score_markdown(A), 1, 1, SCORE_COLS, SCORE_ROWS),
               md("", guide_markdown(), SCORE_COLS + 1, 1, main - SCORE_COLS, SCORE_ROWS),
               md("", queries.MARKDOWN, 1, 1 + SCORE_ROWS, main, HEADER_ROWS)]
    as_of = datetime.fromtimestamp(A["as_of"] / 1000, pov.TZ).date()
    next_monday = as_of + timedelta(days=7 - as_of.weekday())
    last_row = max(r + h for _, _, _, _, r, _, h, _ in queries.WIDGETS)
    extra = queries.pov_widgets(pov.POV_START, next_monday.isoformat(), [d["guid"] for d in A["dash_rows"]], last_row)
    y = 1 + SCORE_ROWS + HEADER_ROWS
    panel_rows = None
    for group in row_groups(queries.WIDGETS + extra):
        beside = panel_rows is None
        width = main if beside else 12
        h = max(spec[6] for spec in group)
        col = 1
        for (title, viz, q, _, _, w0, _, filters), w in zip(group, fit([spec[5] for spec in group], width)):
            cfg = {"nrqlQueries": [{"accountIds": [account_id], "query": q}], "platformOptions": {"ignoreTimeRange": True}}
            if viz == "viz.line":
                cfg["legend"] = {"enabled": True}
            widget = {"title": title, "layout": {"column": col, "row": y, "width": w, "height": h},
                      "visualization": {"id": viz}, "rawConfiguration": cfg}
            widgets.append(widget)
            col += w
        y += h
        if beside and y - 1 >= PANEL_MIN_ROWS:
            panel_rows = y - 1  # the panel ends exactly where this band ends: no gap below it
    widgets.insert(2, md("", actions_markdown(A), main + 1, 1, SIDE_COLS, panel_rows))
    return {"name": NAME, "permissions": "PUBLIC_READ_WRITE",
            "description": "Prospect platform engagement during a PoV (NrdbQuery + NrAuditEvent).",
            "pages": [{"name": "Engagement", "widgets": widgets}]}


def check(A):
    """Render-check: run each widget's NRQL from the locally built dashboard."""
    items = [(w["title"], w["rawConfiguration"]["nrqlQueries"][0]["query"]) for w in build(nr.ACCOUNT_ID, A)["pages"][0]["widgets"]
             if "nrqlQueries" in w["rawConfiguration"]]
    ok = 0
    for title, q in items:
        try:
            rows = nr.nrql(q)
            nonzero = any(v for r in rows for k, v in r.items()
                          if k not in ("beginTimeSeconds", "endTimeSeconds", "comparison") and not isinstance(v, bool))
            status = "DATA" if rows and nonzero else ("ZEROS" if rows else "EMPTY")
            ok += status == "DATA"
            print(f"  {status:5} {len(rows):4} rows  {title}")
        except Exception as e:
            print(f"  ERROR          {title}: {e}")
    print(f"{ok}/{len(items)} widgets return non-zero data")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    A = pov.analyze(int(time.time() * 1000))
    dash = build(nr.ACCOUNT_ID, A)
    (HERE / "dashboard.json").write_text(json.dumps(dash, indent=2))
    print("Wrote dashboard.json")
    if a.check:
        check(A)
