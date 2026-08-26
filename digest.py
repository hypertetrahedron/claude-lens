"""Weekly digest: a static snapshot report for the last 7 full days (UTC).

Each digest is written to reports/digest-<YYYY>-W<week>.html; if that name is
already taken (re-run, manual run) a -HHMMSS suffix is added so an existing
digest is NEVER overwritten. reports/index.html (regenerated each run) links
every digest, newest first, and the project-root index.html is refreshed too
so the new digest shows up next to the live dashboard.

Run manually:  python digest.py
Scheduled:     Task Scheduler job ClaudeMetricsDigest (Mondays 08:00)
"""
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

# Rows carry the product that produced them; a digest covering more than one
# says so rather than silently pooling Cowork spend into Claude Code totals.
PRODUCT_NAMES = {"code": "Claude Code", "cowork": "Claude Cowork"}

# Shared with index.html so every generated page looks like one system.
CSS = report_index.CSS


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


def build_digest(now=None):
    now = now or datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    iso = (end - timedelta(days=1)).isocalendar()
    base_name = f"digest-{iso.year}-W{iso.week:02d}"

    con = db.connect()
    rows, _ = build_dashboard.collect(con)
    con.close()
    lo, hi = start.isoformat(), end.isoformat()
    rows = [r for r in rows if r["ts"] and lo <= r["ts"] < hi]

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
    top_rows = [[r["ts"][:10], r["project"],
                 (r["text"][:90] + "…") if len(r["text"]) > 90 else r["text"],
                 fmt(r["out"]), fmt_cost(r["cost"], r["est"])] for r in top]

    tiles = "".join(
        f"<div class='tile'><div class='l'>{l}</div><div class='v'>{v}</div></div>"
        for l, v in [
            ("Prompts", f"{tot['prompts']:,}"),
            ("Output tokens", fmt(tot["out"])),
            ("Input tokens", fmt(tot["inp"])),
            ("Lines written", fmt(tot["ladd"])),
            ("File edits", fmt(tot["files"])),
            ("Subagent runs", fmt(tot["agents"])),
            ("Cost", fmt_cost(tot["cost"], tot["est"])),
        ])

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
{by_product}
<h2>By project</h2>
{table(['Project', 'Prompts', 'Input', 'Output', 'Lines ±', 'Cost'], proj_rows, {1,2,3,4,5})}
<h2>By model family</h2>
{table(['Family', 'API calls', 'Output', 'Cost'], fam_rows, {1,2,3})}
<h2>Most expensive prompts</h2>
{table(['Day', 'Project', 'Prompt', 'Output', 'Cost'], top_rows, {3,4})}
<h2>Daily totals</h2>
{table(['Day', 'Prompts', 'Output', 'Cost'], day_rows, {1,2,3})}
</div></body></html>"""

    path = unique_path(base_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    rebuild_index()
    index = report_index.build()
    return {"digest": path, "prompts": tot["prompts"], "index": index}


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


if __name__ == "__main__":
    print(json.dumps(build_digest(), indent=2))
