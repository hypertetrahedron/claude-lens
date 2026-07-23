"""Backfill / reconciliation ingester: parses Claude Code JSONL transcripts
into metrics.db.

Serves two roles:
1. One-time historical backfill (OTel only captures data from enablement on).
2. Ongoing reconciliation: heals gaps if the OTel receiver was down while
   Claude Code ran. INSERT OR IGNORE semantics on request_id / tool_use_id
   make re-runs and overlap with OTel rows harmless.

Validated schema facts (see project memory):
- Real user prompts: type=="user" with origin.kind=="human"; carry promptId.
- Assistant entries in main files have no promptId -> attribute sequentially
  to the last human prompt above them.
- One API request spans multiple JSONL lines (one per content block) sharing
  requestId with identical usage -> dedupe by requestId.
- Subagent transcripts live in <proj>/<sessionId>/subagents/agent-*.jsonl and
  carry the parent prompt's promptId.
"""
import glob
import json
import os
import sys

import db

# Claude Code's config dir: ~/.claude by default, overridable via CLAUDE_CONFIG_DIR
PROJECTS_ROOT = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")),
    "projects")


def iter_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def prompt_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_human_prompt(entry):
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    origin = entry.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def handle_assistant(con, entry, prompt_id, session_id, agent=None):
    msg = entry.get("message") or {}
    ts = entry.get("timestamp")
    for blk in msg.get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("id"):
            db.insert_tool_call(con, blk["id"], prompt_id, session_id, ts,
                                blk.get("name", "?"), agent, "jsonl")
    usage = msg.get("usage")
    rid = entry.get("requestId")
    if not usage or not rid:
        return
    cc = usage.get("cache_creation") or {}
    db.upsert_request(con, {
        "request_id": rid,
        "prompt_id": prompt_id,
        "session_id": session_id,
        "ts": ts,
        "model": msg.get("model", "?"),
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_5m_tokens": cc.get("ephemeral_5m_input_tokens", 0) or 0,
        "cache_1h_tokens": cc.get("ephemeral_1h_input_tokens", 0) or 0,
        "cost_usd": None,
        "duration_ms": None,
        "query_source": "subagent" if agent else "main",
        "agent_name": agent,
    }, "jsonl")


def handle_tool_result(con, entry, prompt_id, session_id, agent=None):
    """Record file edits from Edit/Write tool results (type create/update).

    The result rides a user entry whose message content holds the matching
    tool_result block with the tool_use_id. structuredPatch hunks carry
    unified-diff lines ('+'/'-' prefixed); creates without a patch carry the
    full file content instead. Changes made via Bash are not visible here.

    Shape varies by CLI version: Write results carry type "create"/"update";
    Edit results in newer transcripts have NO type field — just filePath +
    oldString/newString + structuredPatch. Detect by filePath + patch.
    (Read results also carry filePath but never a structuredPatch.)
    """
    r = entry.get("toolUseResult")
    if not isinstance(r, dict) or not r.get("filePath"):
        return
    kind = r.get("type")
    if kind not in ("create", "update"):
        if "structuredPatch" not in r:
            return
        kind = "update"
    tuid = None
    for b in (entry.get("message") or {}).get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            tuid = b.get("tool_use_id")
            break
    if not tuid:
        return
    add = rem = chars = 0
    patch = r.get("structuredPatch") or []
    for h in patch:
        for ln in h.get("lines", []) if isinstance(h, dict) else []:
            if ln.startswith("+"):
                add += 1
                chars += len(ln) - 1
            elif ln.startswith("-"):
                rem += 1
    if kind == "create" and not patch:
        content = r.get("content") or ""
        add = content.count("\n") + (1 if content else 0)
        chars = len(content)
    db.insert_edit(con, tuid, prompt_id, session_id, entry.get("timestamp"),
                   r.get("filePath"), kind, add, rem, chars, agent, "jsonl")


def file_changed(con, path):
    try:
        st = os.stat(path)
    except OSError:
        return False
    row = con.execute("SELECT size, mtime FROM ingest_state WHERE path=?",
                      (path,)).fetchone()
    if row and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1e-6:
        return False
    return True


def mark_ingested(con, path):
    try:
        st = os.stat(path)
    except OSError:
        return
    con.execute("INSERT OR REPLACE INTO ingest_state (path, size, mtime) VALUES (?,?,?)",
                (path, st.st_size, st.st_mtime))


def ingest_main_file(con, path):
    project = os.path.basename(os.path.dirname(path))
    session = os.path.splitext(os.path.basename(path))[0]
    current_pid = None
    for e in iter_jsonl(path):
        cwd = e.get("cwd")
        if cwd:
            db.upsert_session(con, session, project=project, cwd=cwd)
        if is_human_prompt(e):
            current_pid = e.get("promptId")
            if current_pid:
                db.upsert_prompt(con, current_pid, session_id=session,
                                 project=project, ts=e.get("timestamp"),
                                 text=prompt_text(e.get("message") or {}),
                                 source="jsonl")
        elif e.get("type") == "assistant" and current_pid:
            handle_assistant(con, e, current_pid, session)
        elif e.get("type") == "user" and current_pid:
            handle_tool_result(con, e, current_pid, session)
    db.upsert_session(con, session, project=project)


def ingest_subagent_file(con, path):
    session = os.path.basename(os.path.dirname(os.path.dirname(path)))
    agent = os.path.splitext(os.path.basename(path))[0]
    entries = list(iter_jsonl(path))
    pid = next((e["promptId"] for e in entries if e.get("promptId")), None)
    if not pid:
        return
    for e in entries:
        if e.get("type") == "assistant":
            handle_assistant(con, e, e.get("promptId") or pid, session, agent=agent)
        elif e.get("type") == "user":
            handle_tool_result(con, e, e.get("promptId") or pid, session, agent=agent)


def run(force=False, root=PROJECTS_ROOT):
    con = db.connect()
    scanned = ingested = 0
    all_files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    for path in sorted(all_files):
        scanned += 1
        if not force and not file_changed(con, path):
            continue
        if os.sep + "subagents" + os.sep in path:
            ingest_subagent_file(con, path)
        else:
            ingest_main_file(con, path)
        mark_ingested(con, path)
        ingested += 1
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("prompts", "api_requests", "tool_calls", "edits", "sessions")}
    con.close()
    return {"scanned": scanned, "ingested": ingested, **counts}


if __name__ == "__main__":
    force = "--force" in sys.argv
    print(json.dumps(run(force=force), indent=2))
