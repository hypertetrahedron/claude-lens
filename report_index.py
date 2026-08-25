"""index.html: one landing page linking every report this project produces.

Written next to dashboard.html on every dashboard build and every digest run,
so whichever ran last, the index is current. It lists the live dashboard plus
each archived report under reports/, newest first, with the time it was
generated - a single bookmark that never goes stale as digests accumulate.

Also owns the shared stylesheet for the static reports (digest.py imports it).
"""
import html
import os
import re
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, "reports")
OUTPUT = os.path.join(BASE, "index.html")

CSS = """
:root { color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b;
  --ink2:#52514e; --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10); }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --page:#0d0d0d;
  --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --grid:#2c2c2a;
  --border:rgba(255,255,255,.10); } }
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--page); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:900px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 2px; } .sub { color:var(--muted); font-size:12px; margin-bottom:18px; }
h2 { font-size:13px; color:var(--ink2); margin:22px 0 8px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:10px 12px; }
.tile .l { color:var(--muted); font-size:11px; } .tile .v { font-size:22px; font-weight:600; }
table { border-collapse:collapse; width:100%; font-size:13px; background:var(--surface);
  border:1px solid var(--border); border-radius:10px; overflow:hidden; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--grid); }
th { color:var(--muted); font-weight:500; font-size:12px; }
td.n,th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
.muted { color:var(--muted); } a { color:inherit; }
.primary a { font-weight:600; }
"""

# reports/digest-2026-W33.html, or -HHMMSS-suffixed when the week already had one
DIGEST_RE = re.compile(r"^digest-(\d{4})-W(\d{2})(?:-(\d{6}))?\.html$")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _stamp(mtime):
    if not mtime:
        return "-"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _describe(name):
    """(title, kind) for a file in reports/."""
    m = DIGEST_RE.match(name)
    if m:
        year, week, rerun = m.groups()
        title = f"Week {week} of {year}"
        return (title + (" (re-run)" if rerun else ""), "Weekly digest")
    return (name[:-5].replace("-", " ").replace("_", " "), "Report")


def entries():
    """Every report to link, dashboard first then reports/ newest-first."""
    out = []
    dash = os.path.join(BASE, "dashboard.html")
    if os.path.exists(dash):
        out.append({"href": "dashboard.html", "title": "Usage dashboard",
                    "kind": "Live dashboard", "mtime": _mtime(dash),
                    "primary": True})
    try:
        names = os.listdir(REPORTS)
    except OSError:
        names = []
    archived = []
    for name in names:
        if not name.endswith(".html") or name == "index.html":
            continue
        title, kind = _describe(name)
        archived.append({"href": f"reports/{name}", "title": title,
                         "kind": kind, "mtime": _mtime(os.path.join(REPORTS, name)),
                         "primary": False})
    archived.sort(key=lambda e: (-e["mtime"], e["title"]))
    return out + archived


def build(output=OUTPUT):
    """(Re)write index.html. Returns the path written."""
    items = entries()
    rows = "".join(
        "<tr class='{cls}'><td><a href='{href}'>{title}</a></td>"
        "<td class='muted'>{kind}</td><td class='n muted'>{when}</td></tr>".format(
            cls="primary" if e["primary"] else "",
            href=html.escape(e["href"], quote=True),
            title=html.escape(e["title"]),
            kind=html.escape(e["kind"]),
            when=_stamp(e["mtime"]))
        for e in items)
    if not rows:
        rows = ("<tr><td colspan='3' class='muted'>No reports yet - run "
                "the generate-dashboard script.</td></tr>")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Lens</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Claude Lens</h1>
<div class="sub">{len(items)} report(s) &middot; index rebuilt {now}</div>
<table><tr><th>Report</th><th>Kind</th><th class="n">Generated</th></tr>{rows}</table>
</div></body></html>"""
    tmp = output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, output)
    return output


if __name__ == "__main__":
    print(build())
