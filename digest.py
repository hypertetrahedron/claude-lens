"""Weekly digest: a static snapshot report for the last 7 full days (UTC).

Each digest is written to reports/digest-<YYYY>-W<week>.html; if that name is
already taken (re-run, manual run) a -HHMMSS suffix is added so an existing
digest is NEVER overwritten. reports/index.html (regenerated each run) links
every digest, newest first, and the project-root index.html is refreshed too
so the new digest shows up next to the live dashboard.

Every headline figure is shown against the week before it, because a number on
its own ("$91") says nothing a person can act on and the same number with
"+38% on last week" says everything.

Run manually:  python digest.py
               python digest.py --db PATH
Scheduled:     Task Scheduler job ClaudeMetricsDigest (Mondays 08:00)
"""
import argparse
import html
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import build_dashboard
import db
import report_index

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, "reports")

# collect() is given a lower bound so it does not read a year of history to
# report on a fortnight. The margin covers a prompt that started just before
# the earlier week and kept working into it: its requests would otherwise be
# clipped. Rows are still windowed here, by their own timestamp.
COLLECT_MARGIN_DAYS = 2

# Rows carry the product that produced them; a digest covering more than one
# says so rather than silently pooling Cowork spend into Claude Code totals.
PRODUCT_NAMES = {"code": "Claude Code", "cowork": "Claude Cowork"}

# Shared with index.html so every generated page looks like one system, plus
# the few rules only the digest needs: the week-over-week line under each tile
# and the note under the tile row. Green/red are not the whole signal - the
# text always says the direction in words too - so a colour-blind reader loses
# nothing.
CSS = report_index.CSS + """
.tile .up { color:#c0392b; } .tile .down { color:#1b7f4b; }
.tile .flat { color:var(--muted); }
.note { color:var(--ink2); font-size:12px; margin:14px 0 4px; }
"""


def fmt(n):
    if n is None:
        return "–"
    a = abs(n)
    if a >= 1e9: return f"{n/1e9:.1f}B"
    if a >= 1e6: return f"{n/1e6:.1f}M"
    if a >= 1e4: return f"{n/1e3:.1f}K"
    return f"{n:,.0f}"


def fmt_cost(c, est):
    p = "~$" if est else "$"
    return p + (f"{c:.0f}" if c >= 100 else f"{c:.2f}")


def fmt_pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def delta(now, before):
    """"+38%" against last week, or an honest dash when there is no last week.

    A percentage of nothing is not a large number, it is a meaningless one, so
    a week with no history says "new" rather than "+infinity".
    """
    if before in (None, 0):
        return ("new", "up") if now else ("-", "flat")
    if now is None:
        return ("-", "flat")
    change = (now - before) / abs(before)
    if abs(change) < 0.005:
        return ("no change", "flat")
    return (f"{change * 100:+.0f}% on last week", "up" if change > 0 else "down")


def conversation_href(prompt_id):
    """`../conversations/<id>.html` when a build wrote that page, else None.

    The digest does not write conversation pages - a build does - so this is a
    link only where the target exists, and the digest stays readable on a
    machine where they were never generated.
    """
    if not prompt_id:
        return None
    path = os.path.join(BASE, "conversations", f"{prompt_id}.html")
    return f"../conversations/{prompt_id}.html" if os.path.exists(path) else None


def cache_stats(rows):
    """(hit fraction, cache-write tokens, uncached input) over `rows`."""
    cr = sum(r["cr"] for r in rows)
    cw = sum(m["cw"] for r in rows for m in r["models"])
    uncached = sum(m["in"] for r in rows for m in r["models"])
    total = cr + cw + uncached
    return (cr / total if total else None), cr, cw, uncached


def unique_path(base_name):
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, base_name + ".html")
    if not os.path.exists(path):
        return path
    stamp = datetime.now().strftime("%H%M%S")
    return os.path.join(REPORTS, f"{base_name}-{stamp}.html")


def table(headers, rows, num_cols):
    h = "".join(f"<th class='{'n' if i in num_cols else ''}'>{html.escape(str(x))}</th>"
                for i, x in enumerate(headers))
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td class='{'n' if i in num_cols else ''}'>{html.escape(str(x))}</td>"
            for i, x in enumerate(row)) + "</tr>"
    return f"<table><tr>{h}</tr>{body}</table>"


def build_digest(now=None, db_path=None):
    now = now or datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    prev_start = start - timedelta(days=7)
    iso = (end - timedelta(days=1)).isocalendar()
    base_name = f"digest-{iso.year}-W{iso.week:02d}"

    resolved = db.resolve_path(db_path)
    con = db.connect() if resolved == db.DB_PATH else db.connect(resolved)
    since = (prev_start - timedelta(days=COLLECT_MARGIN_DAYS)).isoformat()
    all_rows, _ = build_dashboard.collect(con, since=since)
    extras = dict(build_dashboard.EXTRAS)
    con.close()
    lo, hi = start.isoformat(), end.isoformat()
    plo = prev_start.isoformat()
    rows = [r for r in all_rows if r["ts"] and lo <= r["ts"] < hi]
    prev_rows = [r for r in all_rows if r["ts"] and plo <= r["ts"] < lo]
    # Session totals over this week's prompts only, so a long-running session
    # is reported by what it did in the week rather than in its whole life.
    sessions = build_dashboard.session_rows(rows)

    tot = {
        "prompts": len(rows),
        "out": sum(r["out"] for r in rows),
        "inp": sum(r["inp"] for r in rows),
        "cost": sum(r["cost"] for r in rows),
        "est": any(r["est"] for r in rows),
        "ladd": sum(r["ladd"] for r in rows),
        "lrem": sum(r["lrem"] for r in rows),
        "files": sum(r["files"] for r in rows),
        "agents": sum(len(r["agents"]) for r in rows),
    }

    prod = defaultdict(lambda: defaultdict(float))
    proj = defaultdict(lambda: defaultdict(float))
    fam = defaultdict(lambda: defaultdict(float))
    days = defaultdict(lambda: defaultdict(float))
    for r in rows:
        k = prod[PRODUCT_NAMES.get(r.get("kind"), r.get("kind") or "Claude Code")]
        k["prompts"] += 1; k["out"] += r["out"]; k["inp"] += r["inp"]
        k["cost"] += r["cost"]; k["lines"] += r["ladd"] + r["lrem"]
        p = proj[r["project"]]
        p["prompts"] += 1; p["out"] += r["out"]; p["inp"] += r["inp"]
        p["cost"] += r["cost"]; p["lines"] += r["ladd"] + r["lrem"]
        d = days[r["ts"][:10]]
        d["prompts"] += 1; d["out"] += r["out"]; d["cost"] += r["cost"]
        for m in r["models"]:
            name = m["model"]
            f = ("opus" if "opus" in name else
                 "fable" if ("fable" in name or "mythos" in name) else
                 "sonnet" if "sonnet" in name else
                 "haiku" if "haiku" in name else "other")
            fam[f]["out"] += m["out"]; fam[f]["cost"] += m["cost"]
            fam[f]["calls"] += m["calls"]

    prod_rows = [[k, int(v["prompts"]), fmt(v["out"]), fmt(v["lines"]),
                  fmt_cost(v["cost"], tot["est"])]
                 for k, v in sorted(prod.items(), key=lambda kv: -kv[1]["cost"])]
    proj_rows = [[k, int(v["prompts"]), fmt(v["inp"]), fmt(v["out"]),
                  fmt(v["lines"]), fmt_cost(v["cost"], tot["est"])]
                 for k, v in sorted(proj.items(), key=lambda kv: -kv[1]["cost"])]
    fam_rows = [[k, int(v["calls"]), fmt(v["out"]), fmt_cost(v["cost"], tot["est"])]
                for k, v in sorted(fam.items(), key=lambda kv: -kv[1]["cost"])]
    day_rows = [[k, int(v["prompts"]), fmt(v["out"]), fmt_cost(v["cost"], tot["est"])]
                for k, v in sorted(days.items())]
    top = sorted(rows, key=lambda r: -r["cost"])[:10]
    top_html = ""
    for r in top:
        text = (r["text"][:90] + "…") if len(r["text"]) > 90 else r["text"]
        cell = html.escape(text)
        href = conversation_href(r["id"])
        if href:
            cell = f"<a href='{html.escape(href, quote=True)}'>{cell}</a>"
        top_html += (
            "<tr><td>{d}</td><td>{p}</td><td>{t}</td>"
            "<td class='n'>{o}</td><td class='n'>{c}</td></tr>".format(
                d=html.escape(r["ts"][:10]), p=html.escape(r["project"]),
                t=cell, o=html.escape(fmt(r["out"])),
                c=html.escape(fmt_cost(r["cost"], r["est"]))))
    top_table = (
        "<table><tr><th>Day</th><th>Project</th><th>Prompt</th>"
        f"<th class='n'>Output</th><th class='n'>Cost</th></tr>{top_html}</table>")

    # Week over week. Four figures earn the comparison: what it cost, how much
    # was asked of it, how much came back, and how well the cache held.
    hit, cr, cw, uncached = cache_stats(rows)
    prev_hit, _, prev_cw, _ = cache_stats(prev_rows)
    prev_tot = {
        "cost": sum(r["cost"] for r in prev_rows),
        "prompts": len(prev_rows),
        "out": sum(r["out"] for r in prev_rows),
    }
    misses = sum(r.get("misses", 0) for r in rows)
    miss_cost = sum(r.get("cache_miss_cost", 0.0) for r in rows)
    tool_errors = sum(r.get("errors", 0) for r in rows)
    tool_calls = sum(n for r in rows for _, n in r["tools"])
    truncations = sum(r.get("max_tokens_stops", 0) for r in rows)
    api_errors = (extras.get("errors") or {}).get("api", 0)

    tiles = "".join(
        "<div class='tile'><div class='l'>{l}</div><div class='v'>{v}</div>"
        "<div class='l {cls}'>{d}</div></div>".format(
            l=html.escape(l), v=html.escape(v), cls=cls, d=html.escape(d))
        for l, v, (d, cls) in [
            ("Prompts", f"{tot['prompts']:,}",
             delta(tot["prompts"], prev_tot["prompts"])),
            ("Output tokens", fmt(tot["out"]),
             delta(tot["out"], prev_tot["out"])),
            ("Input tokens", fmt(tot["inp"]), ("", "flat")),
            ("Lines written", fmt(tot["ladd"]), ("", "flat")),
            ("File edits", fmt(tot["files"]), ("", "flat")),
            ("Subagent runs", fmt(tot["agents"]), ("", "flat")),
            ("Cache hit", fmt_pct(hit), delta(hit, prev_hit)),
            ("Cost", fmt_cost(tot["cost"], tot["est"]),
             delta(tot["cost"], prev_tot["cost"])),
        ])

    basis = extras.get("cost_basis")
    if basis == "contracted":
        basis_note = ("Costs are the CLI's own figures at your contracted "
                      "rates.")
    elif basis == "mixed":
        basis_note = ("Costs mix contracted rates reported by the CLI with "
                      "list-price estimates.")
    elif tot["est"]:
        basis_note = ("Costs marked <b>~</b> are estimated from public list "
                      "prices, not billed amounts.")
    else:
        basis_note = "Costs are the figures the CLI reported."

    cache_rows = [
        ["Cache reads", fmt(cr), "billed at a tenth of the input rate"],
        ["Cache writes", fmt(cw),
         f"{fmt(prev_cw)} last week" if prev_rows else "no prior week"],
        ["Uncached input", fmt(uncached), "paid in full"],
        ["Cache misses", f"{misses:,}",
         f"{fmt_cost(miss_cost, tot['est'])} of cache writes re-done"],
    ]

    sess_rows = [[(s["title"] or s["project"]),
                  (s["first_prompt_text"][:70] or "-"),
                  int(s["prompts"]), fmt(s["out"]), fmt_pct(s["hit"]),
                  fmt_cost(s["cost"], s["est"])]
                 for s in sorted(sessions, key=lambda s: -s["cost"])[:10]]

    reliability = (
        f"{tool_errors:,} of {tool_calls:,} tool calls failed"
        + (f" ({tool_errors / tool_calls * 100:.1f}%)" if tool_calls else "")
        + f"; {api_errors:,} API errors; {truncations:,} responses hit the "
          "output-token ceiling and had to be continued.")

    # only worth a section when more than one product is in play
    by_product = ("<h2>By product</h2>" + table(
        ["Product", "Prompts", "Output", "Lines +/-", "Cost"], prod_rows,
        {1, 2, 3, 4})) if len(prod_rows) > 1 else ""
    period = f"{start.date()} – {(end - timedelta(days=1)).date()}"
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Lens digest {period}</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Claude Lens weekly digest</h1>
<div class="sub">{period} (UTC) · generated {now.strftime('%Y-%m-%d %H:%M')} ·
<a href="index.html">all digests</a> · <a href="../index.html">all reports</a></div>
<div class="tiles">{tiles}</div>
<div class="note">{basis_note} {reliability}</div>
{by_product}
<h2>By project</h2>
{table(['Project', 'Prompts', 'Input', 'Output', 'Lines ±', 'Cost'], proj_rows, {1,2,3,4,5})}
<h2>By session</h2>
{table(['Session', 'Opened with', 'Prompts', 'Output', 'Cache hit', 'Cost'], sess_rows, {2,3,4,5})}
<h2>By model family</h2>
{table(['Family', 'API calls', 'Output', 'Cost'], fam_rows, {1,2,3})}
<h2>Caching</h2>
{table(['', 'Tokens', 'Note'], cache_rows, {1})}
<h2>Most expensive prompts</h2>
{top_table}
<h2>Daily totals</h2>
{table(['Day', 'Prompts', 'Output', 'Cost'], day_rows, {1,2,3})}
</div></body></html>"""

    path = unique_path(base_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    rebuild_index()
    index = report_index.build()
    return {"digest": path, "prompts": tot["prompts"], "index": index,
            "cost": round(tot["cost"], 4),
            "cost_delta": delta(tot["cost"], prev_tot["cost"])[0],
            "cache_hit": None if hit is None else round(hit, 4),
            "cache_misses": misses,
            "sessions": len(sessions),
            "tool_errors": tool_errors}


def rebuild_index():
    """Regenerate reports/index.html linking every digest, newest first."""
    files = sorted((f for f in os.listdir(REPORTS)
                    if re.match(r"digest-.*\.html$", f)), reverse=True)
    items = "".join(f"<tr><td><a href='{html.escape(f)}'>{html.escape(f[:-5])}</a></td></tr>"
                    for f in files)
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Claude Lens digests</title><style>{CSS}</style></head><body>
<div class="wrap"><h1>Claude Lens — weekly digests</h1><div class="sub">{len(files)} reports</div>
<table><tr><th>Digest</th></tr>{items}</table></div></body></html>"""
    with open(os.path.join(REPORTS, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Write a weekly digest for the last 7 full UTC days.")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to read (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, then metrics.db)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    print(json.dumps(build_digest(db_path=parse_args().db), indent=2))
