"""index.html: one landing page linking every report this project produces.

Written next to dashboard.html on every dashboard build and every digest run,
so whichever ran last, the index is current. It lists the live dashboard, the
conversation pages when a build wrote them, Claude Code's own /insights report
when that exists, and each archived report under reports/, newest first, with
the time it was generated - a single bookmark that never goes stale as digests
accumulate.

Nothing here opens metrics.db, so there is no --db flag: the index is built
from what is on disk next to it. Directories are read with os.scandir and the
mtimes come off that same read, so listing a hundred archived digests costs
one directory walk rather than one stat() per file.

Also owns the shared stylesheet for the static reports (digest.py imports it).
"""
import html
import os
import re
from datetime import datetime
from urllib.request import pathname2url

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


def _file_url(path):
    """A file: URL for a target outside this directory.

    The /insights report lives under the Claude config directory, which may be
    on another drive; a relative href would be wrong there and unreadable
    everywhere. pathname2url handles the Windows drive-letter form.
    """
    return "file:" + pathname2url(os.path.abspath(path))


def _config_dir():
    """Claude Code's own config directory: $CLAUDE_CONFIG_DIR or ~/.claude.

    Deliberately a copy of the two lines in sources.py rather than an import -
    this module is pulled in by digest.py and build_dashboard.py, and it has no
    other reason to load the discovery machinery.
    """
    return os.path.expanduser(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join("~", ".claude"))


def _insights_report():
    """Claude Code's built-in /insights report, if the CLI has written one.

    It answers a different question than this dashboard does - the CLI's own
    view of the account - and it is easy to forget it exists, so link it rather
    than reproduce it.
    """
    path = os.path.join(_config_dir(), "usage-data", "report.html")
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"href": _file_url(path), "title": "Claude Code insights",
            "kind": "CLI /insights report", "mtime": st.st_mtime,
            "primary": False}


def _conversations(base):
    """The per-prompt conversation pages a build may have written.

    (mtime, href) or None. A directory listing renders fine over file://, but
    an index page is linked in preference to one when the build wrote it.
    """
    folder = os.path.join(base, "conversations")
    try:
        st = os.stat(folder)
        if not os.path.isdir(folder):
            return None
    except OSError:
        return None
    href = ("conversations/index.html"
            if os.path.exists(os.path.join(folder, "index.html"))
            else "conversations/")
    return {"href": href, "title": "Conversations",
            "kind": "Per-prompt transcripts", "mtime": st.st_mtime,
            "primary": False}


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
    """Every report to link: the live pages first, then reports/ newest-first."""
    out = []
    dash = os.path.join(BASE, "dashboard.html")
    if os.path.exists(dash):
        out.append({"href": "dashboard.html", "title": "Usage dashboard",
                    "kind": "Live dashboard", "mtime": _mtime(dash),
                    "primary": True})
    for extra in (_conversations(BASE), _insights_report()):
        if extra:
            out.append(extra)

    archived = []
    try:
        with os.scandir(REPORTS) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(".html") or name == "index.html":
                    continue
                # st_mtime comes off the directory read; on Windows and on
                # Linux since 3.x scandir already carries it, so a folder of
                # archived digests costs one walk and no per-file stat().
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0
                title, kind = _describe(name)
                archived.append({"href": f"reports/{name}", "title": title,
                                 "kind": kind, "mtime": mtime,
                                 "primary": False})
    except OSError:
        pass
    archived.sort(key=lambda e: (-e["mtime"], e["title"]))
    return out + archived


def build(output=None):
    """(Re)write index.html. Returns the path written.

    The destination is resolved at call time rather than bound as a default,
    so redirecting the module's OUTPUT actually takes effect - which is what
    tests need in order not to write into the working copy.
    """
    output = output or OUTPUT
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
