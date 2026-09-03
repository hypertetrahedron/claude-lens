"""Per-prompt conversation pages written next to dashboard.html.

The dashboard says a prompt cost $4.80 and made 61 API calls. The obvious next
question - *what did it actually do* - used to mean opening a 40 MB JSONL file
and reading it with your eyes. These pages answer it: one self-contained HTML
file per prompt, rendering the exchange the way it happened, with what each
turn cost beside it.

Design rules, all of them load-bearing:

- **The transcript is the source, the database is the index.** Costs, models
  and subagent types come from `metrics.db` (they are already reconciled with
  the CLI's own figures); the words come from the JSONL, because that is the
  only place they exist.
- **Nothing user-written reaches the page unescaped.** Every value goes
  through `html.escape(..., quote=True)`, so a prompt containing `<script>`
  renders as text and does nothing. There is no innerHTML here and no
  scripting at all - these pages carry no JS.
- **Prompt text is prompt text.** `--no-prompt-text` writes no pages, because
  a conversation page is nothing but the thing that flag exists to withhold.
- **Bounded.** Assistant blocks are cut at 4000 characters, tool inputs
  summarised at 200, and a page stops at about a megabyte with a notice
  saying so. A page nobody can open is not a record of anything.
- **Incremental.** A page is rewritten only when its transcript moved, so a
  rebuild every minute does not rewrite three hundred files every minute.
"""
import html
import json
import os
from datetime import datetime, timezone

import pricing

# Per assistant text/thinking block, and per tool-call input summary.
MAX_BLOCK = 4000
MAX_INPUT_SUMMARY = 200

# A page past this is not a document, it is a denial of service against the
# person who clicked the link. Rendering stops and says it stopped.
MAX_PAGE_BYTES = 1_000_000

# Where a session's subagent transcripts live, relative to the main file's
# directory: <dir>/<session id>/subagents/agent-<agent id>.jsonl
SUBAGENT_DIR = "subagents"

# The tools that launch a subagent, so a boundary can be drawn at one.
AGENT_TOOLS = frozenset({"Agent", "Task"})

# Theme tokens copied from template.html's :root, so a conversation page and
# the dashboard are visibly the same product in either colour scheme. Kept as
# a copy rather than an import: these pages must stand alone on disk, and the
# template is a single file with its CSS inline.
CSS = """
:root {
  color-scheme: light;
  --page:#f9f9f7; --surface-1:#fcfcfb; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --text-muted:#898781; --grid:#e1e0d9;
  --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-2:#1baf7a; --series-3:#eda100;
  --series-5:#4a3aa7; --hover-wash:rgba(11,11,11,0.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:#0d0d0d; --surface-1:#1a1a19; --text-primary:#ffffff;
    --text-secondary:#c3c2b7; --text-muted:#898781; --grid:#2c2c2a;
    --baseline:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-2:#199e70; --series-3:#c98500;
    --series-5:#9085e9; --hover-wash:rgba(255,255,255,0.06);
  }
}
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--page); color:var(--text-primary);
  font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:900px; margin:0 auto; }
a { color:var(--series-1); }
h1 { font-size:19px; margin:0 0 2px; }
.sub { color:var(--text-muted); font-size:12px; margin-bottom:16px; }
.meta { display:flex; flex-wrap:wrap; gap:14px; font-size:12px;
  color:var(--text-secondary); margin-bottom:18px; }
.meta b { font-weight:600; color:var(--text-primary); font-variant-numeric:tabular-nums; }
.turn { border:1px solid var(--border); border-radius:10px;
  background:var(--surface-1); margin:10px 0; overflow:hidden; }
.turn > .hd { display:flex; justify-content:space-between; gap:10px;
  padding:6px 12px; border-bottom:1px solid var(--grid); font-size:12px;
  color:var(--text-muted); }
.turn > .hd .who { font-weight:600; color:var(--text-secondary); }
.turn .body { padding:10px 12px; }
pre { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font:13px/1.55
  ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.user > .hd .who { color:var(--series-1); }
.thinking pre { color:var(--text-muted); font-style:italic; }
.tool { display:flex; gap:8px; align-items:baseline; padding:4px 12px;
  border-top:1px solid var(--grid); font-size:12.5px; }
.tool .nm { font-weight:600; }
.tool .arg { color:var(--text-secondary); overflow-wrap:anywhere; flex:1; }
.tool .sz { color:var(--text-muted); white-space:nowrap;
  font-variant-numeric:tabular-nums; }
.tool.err { background:var(--hover-wash); }
.tool.err .nm { color:#c0392b; }
.agent { margin:10px 0 10px 18px; border-left:3px solid var(--series-5);
  padding-left:12px; }
.agent > .cap { font-size:12px; color:var(--series-5); font-weight:600;
  margin-bottom:4px; }
.cost { font-variant-numeric:tabular-nums; white-space:nowrap; }
.note { color:var(--text-muted); font-size:12px; margin:14px 0; }
table { border-collapse:collapse; width:100%; font-size:13px;
  background:var(--surface-1); border:1px solid var(--border);
  border-radius:10px; overflow:hidden; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--grid);
  vertical-align:top; }
th { color:var(--text-muted); font-weight:500; font-size:12px; }
td.n,th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
.muted { color:var(--text-muted); }
"""


def esc(v):
    """Every piece of user content on these pages goes through this."""
    return html.escape("" if v is None else str(v), quote=True)


def fmt_tokens(n):
    if not n:
        return "0"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e4:
        return f"{n / 1e3:.1f}K"
    return f"{n:,}"


def fmt_cost(c, est=True):
    if c is None:
        return "-"
    return ("~$" if est else "$") + (f"{c:.2f}" if c >= 0.01 else f"{c:.4f}")


def _iter_jsonl(path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _blocks(message):
    c = (message or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _summarise_input(value):
    """A tool call's input as one short line.

    The whole input is often a file's contents; what identifies the call is
    the first field or two, so this prefers the fields that name a target and
    falls back to the JSON form.
    """
    if not isinstance(value, dict):
        text = "" if value is None else str(value)
    else:
        for key in ("command", "file_path", "path", "pattern", "query",
                    "prompt", "description", "url", "notebook_path"):
            if value.get(key):
                text = f"{key}={value[key]}"
                break
        else:
            try:
                text = json.dumps(value, separators=(",", ":"))
            except (TypeError, ValueError):
                text = str(value)
    text = " ".join(text.split())
    return text[:MAX_INPUT_SUMMARY] + ("..." if len(text) > MAX_INPUT_SUMMARY
                                       else "")


def _result_sizes(entries):
    """tool_use_id -> (bytes, is_error) from the tool results in `entries`."""
    out = {}
    for e in entries:
        if e.get("type") != "user":
            continue
        for b in _blocks(e.get("message")):
            if b.get("type") != "tool_result":
                continue
            content = b.get("content")
            if isinstance(content, list):
                size = sum(len(str(p.get("text") or "")) for p in content
                           if isinstance(p, dict))
            else:
                size = len(str(content or ""))
            tur = e.get("toolUseResult")
            if isinstance(tur, dict):
                size = max(size, len(str(tur.get("stdout") or ""))
                           + len(str(tur.get("stderr") or "")))
            err = b.get("is_error")
            err = str(err).lower() == "true" if err is not None else False
            if isinstance(tur, dict) and (tur.get("error") or tur.get("isError")):
                err = True
            out[b.get("tool_use_id")] = (size, err)
    return out


class Page:
    """An HTML page being accumulated, with a hard size budget."""

    def __init__(self, budget=MAX_PAGE_BYTES):
        self.parts = []
        self.size = 0
        self.budget = budget
        self.full = False

    def add(self, chunk):
        if self.full:
            return False
        self.size += len(chunk)
        if self.size > self.budget:
            self.full = True
            self.parts.append(
                "<div class='note'>This conversation is longer than one page "
                "can hold; the rest is in the transcript.</div>")
            return False
        self.parts.append(chunk)
        return True

    def html(self):
        return "".join(self.parts)


def _render_entries(page, entries, costs, agents, base_dir, session_id,
                    depth=0):
    """Render one transcript's worth of entries into `page`.

    `costs` maps a requestId to (cost, est, model); `agents` maps a tool_use_id
    to (agent_id, subagent_type, model). Subagent transcripts are rendered
    inline under the tool call that launched them, one level deep - deeper
    nesting exists but has never been observed and would not read as anything.
    """
    sizes = _result_sizes(entries)
    seen_requests = set()
    for e in entries:
        if page.full:
            return
        kind = e.get("type")
        msg = e.get("message") or {}
        blocks = _blocks(msg)
        if kind == "user":
            texts = [b.get("text") or "" for b in blocks
                     if b.get("type") == "text"]
            body = "\n".join(t for t in texts if t.strip())
            if not body:
                continue
            page.add(
                "<div class='turn user'><div class='hd'><span class='who'>"
                f"You</span><span>{esc(_short_ts(e.get('timestamp')))}</span>"
                f"</div><div class='body'><pre>{esc(_cut(body))}</pre></div>"
                "</div>")
            continue
        if kind != "assistant":
            continue
        rid = e.get("requestId")
        cost_html = ""
        if rid and rid not in seen_requests and rid in costs:
            seen_requests.add(rid)
            cost, est, model = costs[rid]
            cost_html = (f"<span class='cost'>{esc(model or '')} "
                         f"{esc(fmt_cost(cost, est))}</span>")
        head = ("<div class='turn'><div class='hd'><span class='who'>Claude"
                "</span>" + (cost_html or
                             f"<span>{esc(_short_ts(e.get('timestamp')))}</span>")
                + "</div>")
        body, tools = [], []
        for b in blocks:
            btype = b.get("type")
            if btype == "text" and (b.get("text") or "").strip():
                body.append(f"<pre>{esc(_cut(b['text']))}</pre>")
            elif btype == "thinking" and (b.get("thinking") or "").strip():
                body.append("<div class='thinking'><pre>"
                            f"{esc(_cut(b['thinking']))}</pre></div>")
            elif btype == "tool_use":
                tools.append(b)
        chunk = head
        if body:
            chunk += "<div class='body'>" + "".join(body) + "</div>"
        for b in tools:
            tid = b.get("id")
            size, err = sizes.get(tid, (None, False))
            name = b.get("name") or "?"
            chunk += (f"<div class='tool{' err' if err else ''}'>"
                      f"<span class='nm'>{esc(name)}</span>"
                      f"<span class='arg'>{esc(_summarise_input(b.get('input')))}"
                      "</span><span class='sz'>"
                      + (esc(f"{size:,} B") if size is not None else "-")
                      + (" &middot; error" if err else "") + "</span></div>")
        chunk += "</div>"
        if not page.add(chunk):
            return
        # Subagent boundaries, after the turn that launched them.
        for b in tools:
            if (b.get("name") or "") not in AGENT_TOOLS or depth:
                continue
            info = agents.get(b.get("id"))
            if not info:
                continue
            agent_id, subagent_type, model = info
            page.add(
                "<div class='agent'><div class='cap'>subagent: "
                f"{esc(subagent_type or agent_id)}"
                + (f" &middot; {esc(model)}" if model else "") + "</div>")
            sub = _subagent_path(base_dir, session_id, agent_id)
            if sub:
                _render_entries(page, list(_iter_jsonl(sub)), costs, agents,
                                base_dir, session_id, depth + 1)
            else:
                page.add("<div class='note'>Its transcript is not on disk.</div>")
            page.add("</div>")


def _cut(text):
    text = str(text)
    if len(text) <= MAX_BLOCK:
        return text
    return text[:MAX_BLOCK] + f"\n... [{len(text) - MAX_BLOCK:,} more characters]"


def _short_ts(ts):
    return (str(ts or "").replace("T", " ")[:19]) or ""


def _subagent_path(base_dir, session_id, agent_id):
    """`<dir>/<session>/subagents/agent-<id>.jsonl`, if it is still there."""
    if not (base_dir and session_id and agent_id):
        return None
    path = os.path.join(base_dir, session_id, SUBAGENT_DIR,
                        f"agent-{agent_id}.jsonl")
    return path if os.path.exists(path) else None


def _chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _folded(con, pids):
    """canonical prompt id -> every prompt id folded into it, itself included.

    A background agent's work is recorded against its own `<task-notification>`
    prompt, whose `canonical_id` points back at the human turn that caused it.
    The dashboard row is the human turn, so the page has to be too.
    """
    out = {p: [p] for p in pids}
    for chunk in _chunks(pids):
        marks = ",".join("?" * len(chunk))
        for pid, canon in con.execute(
                f"SELECT prompt_id, canonical_id FROM prompts "
                f"WHERE canonical_id IN ({marks}) AND injected = 1", chunk):
            if canon in out and pid != canon:
                out[canon].append(pid)
    return out


def _costs(con, pids):
    """request id -> (cost, is_estimate, model) for every request of `pids`."""
    out = {}
    for chunk in _chunks(pids):
        marks = ",".join("?" * len(chunk))
        for (rid, model, provider, speed, geo, ts, cost, inp, out_t, cr, cw,
             c5, c1) in con.execute(
                f"""SELECT request_id, model, provider, speed, inference_geo,
                           ts, cost_usd, COALESCE(input_tokens, 0),
                           COALESCE(output_tokens, 0),
                           COALESCE(cache_read_tokens, 0),
                           COALESCE(cache_create_tokens, 0),
                           COALESCE(cache_5m_tokens, 0),
                           COALESCE(cache_1h_tokens, 0)
                      FROM api_requests WHERE prompt_id IN ({marks})""",
                chunk):
            est = cost is None
            if est:
                cost = pricing.estimate_cost(
                    model, inp, out_t, cr, c5, c1, max(cw - c5 - c1, 0), ts,
                    provider, speed, geo)
            out[rid] = (cost, est, model)
    return out


def _agents(con, pids):
    """tool_use_id -> (agent_id, subagent_type, model) for `pids`."""
    out = {}
    for chunk in _chunks(pids):
        marks = ",".join("?" * len(chunk))
        for tuid, aid, kind, resolved, requested in con.execute(
                f"""SELECT tool_use_id, agent_id, subagent_type,
                           resolved_model, requested_model
                      FROM agents WHERE prompt_id IN ({marks})""", chunk):
            if tuid:
                out[tuid] = (aid, kind, resolved or requested)
    return out


def _document(title, sub, meta, body):
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="wrap"><h1>{esc(title)}</h1>'
        f'<div class="sub">{sub}</div>'
        f'<div class="meta">{meta}</div>{body}</div></body></html>')


def _page_is_current(page_path, transcript, session_dir):
    """True when the page is newer than everything it was rendered from."""
    try:
        made = os.path.getmtime(page_path)
    except OSError:
        return False
    newest = 0.0
    for path in (transcript, session_dir):
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest and made >= newest


def write_pages(con, out_dir, rows, limit=300, redact=False):
    """Write conversations/<prompt id>.html for the newest `limit` rows.

    `rows` are collect() rows, newest first; the ones that got a page have
    their `conv` field set to the relative href the dashboard links. Returns
    {"count", "dir", "written", "skipped_missing"}.

    A prompt whose transcript has been deleted is skipped silently in the
    sense that it is not an error - it is counted, and build() turns the count
    into a line in the notice bar.
    """
    result = {"count": 0, "written": 0, "skipped_missing": 0, "dir": None}
    if redact or not limit:
        return result
    targets = [r for r in rows[:limit] if r.get("session")]
    if not targets:
        return result
    folder = os.path.join(out_dir, "conversations")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return result
    result["dir"] = folder

    transcripts = dict(con.execute(
        "SELECT session_id, transcript_path FROM sessions"))

    # Which pages actually need rendering. Doing this before any of the
    # queries below is what keeps a once-a-minute rebuild cheap: a session
    # nobody touched costs one stat() and nothing else.
    todo, listed = [], []
    for r in targets:
        path = transcripts.get(r["session"])
        if not path or not os.path.exists(path):
            result["skipped_missing"] += 1
            continue
        page_path = os.path.join(folder, f"{r['id']}.html")
        href = "conversations/" + os.path.basename(page_path)
        r["conv"] = href
        listed.append((r, href))
        if not _page_is_current(page_path, path,
                                os.path.join(os.path.dirname(path),
                                             r["session"])):
            todo.append((r, path, page_path))
    result["count"] = len(listed)

    if todo:
        folded = _folded(con, [r["id"] for r, _, _ in todo])
        all_pids = [p for group in folded.values() for p in group]
        costs = _costs(con, all_pids)
        agent_map = _agents(con, all_pids)

        # One walk per transcript, however many of its prompts need a page.
        by_transcript = {}
        for r, path, page_path in todo:
            by_transcript.setdefault(path, []).append((r, page_path))
        for path, wanted in by_transcript.items():
            want = {}
            for r, page_path in wanted:
                for pid in folded.get(r["id"], [r["id"]]):
                    want[pid] = r["id"]
            picked = {r["id"]: [] for r, _ in wanted}
            current = None
            for e in _iter_jsonl(path):
                pid = e.get("promptId")
                if pid:
                    current = want.get(pid)
                if current is not None:
                    picked[current].append(e)
            base_dir = os.path.dirname(path)
            for r, page_path in wanted:
                _write_one(page_path, r, picked.get(r["id"]) or [], costs,
                           agent_map, base_dir)
                result["written"] += 1

    _write_index(folder, listed)
    return result


def _write_one(page_path, row, entries, costs, agents, base_dir):
    page = Page()
    _render_entries(page, entries, costs, agents, base_dir, row["session"])
    if not entries:
        page.add("<div class='note'>Nothing for this prompt survives in the "
                 "transcript - it may have been compacted away.</div>")
    text = (row.get("text") or "").strip() or "(no prompt text)"
    title = " ".join(text.split())[:80]
    tools = sum(n for _, n in row.get("tools") or ())
    meta = "".join(
        f"<span>{esc(label)} <b>{esc(value)}</b></span>" for label, value in (
            ("cost", fmt_cost(row.get("cost"), row.get("est", True))),
            ("API calls", f"{row.get('api_calls', 0):,}"),
            ("output", fmt_tokens(row.get("out", 0))),
            ("input", fmt_tokens(row.get("inp", 0))),
            ("tool calls", f"{tools:,}"),
            ("subagents", str(len(row.get("agents") or ()))),
            ("files", f"{row.get('files', 0):,}"),
            ("lines", f"+{row.get('ladd', 0):,} -{row.get('lrem', 0):,}"),
        ))
    sub = (f"{esc(_short_ts(row.get('ts')))} &middot; {esc(row.get('project'))} "
           "&middot; <a href='index.html'>all conversations</a> &middot; "
           "<a href='../dashboard.html'>dashboard</a>")
    doc = _document(title, sub, meta, page.html())
    tmp = page_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, page_path)


def _write_index(folder, listed):
    rows = "".join(
        "<tr><td class='muted'>{when}</td><td class='muted'>{project}</td>"
        "<td><a href='{href}'>{text}</a></td><td class='n'>{cost}</td>"
        "<td class='n muted'>{out}</td></tr>".format(
            when=esc(_short_ts(r.get("ts"))[:16]),
            project=esc(r.get("project")),
            href=esc(os.path.basename(href)),
            text=esc(" ".join((r.get("text") or "(no prompt text)").split())[:110]),
            cost=esc(fmt_cost(r.get("cost"), r.get("est", True))),
            out=esc(fmt_tokens(r.get("out", 0))))
        for r, href in listed)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    doc = _document(
        "Conversations",
        f"{len(listed)} prompt(s) &middot; rebuilt {esc(now)} UTC &middot; "
        "<a href='../dashboard.html'>dashboard</a> &middot; "
        "<a href='../index.html'>all reports</a>",
        "",
        "<table><tr><th>When</th><th>Project</th><th>Prompt</th>"
        f"<th class='n'>Cost</th><th class='n'>Output</th></tr>{rows}</table>")
    tmp = os.path.join(folder, "index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, os.path.join(folder, "index.html"))
