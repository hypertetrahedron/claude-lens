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

Sources: by default this reads every Claude directory `sources.py` can find -
the primary ~/.claude, sibling .claude* directories, any configured extra
locations, and any configured remote machines (fetched over SSH first). Roots
other than the primary carry a label that is prepended to their project names,
so a dashboard row always shows where it came from.
"""
import argparse
import glob
import json
import os
import time

import db
import sources

# Claude Code's config dir: ~/.claude by default, overridable via
# CLAUDE_CONFIG_DIR. Kept as the single-directory default for callers passing
# root= explicitly; normal runs go through sources.discover_local().
PROJECTS_ROOT = os.path.join(sources.primary_dir(), "projects")


def qualify(label, project):
    """Project name as stored: prefixed with its source label, if any.

    The primary ~/.claude has an empty label, so its names are untouched and
    rows ingested before this feature existed keep matching.
    """
    return f"{label}/{project}" if label else project


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
            name = blk.get("name", "?")
            detail = None
            if name == "Skill":
                inp = blk.get("input")
                if isinstance(inp, dict):
                    detail = inp.get("skill")
            db.insert_tool_call(con, blk["id"], prompt_id, session_id, ts,
                                name, agent, "jsonl", detail=detail)
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


def ingest_main_file(con, path, label=""):
    project = qualify(label, os.path.basename(os.path.dirname(path)))
    session = os.path.splitext(os.path.basename(path))[0]
    current_pid = None
    for e in iter_jsonl(path):
        cwd = e.get("cwd")
        if cwd:
            db.upsert_session(con, session, project=project, cwd=cwd,
                              source_label=label)
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
    db.upsert_session(con, session, project=project, source_label=label)


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


def ingest_tree(con, projects_dir, label="", force=False):
    """Ingest every transcript under one `projects/` directory."""
    scanned = ingested = 0
    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    for path in sorted(glob.glob(pattern, recursive=True)):
        scanned += 1
        if not force and not file_changed(con, path):
            continue
        if os.sep + "subagents" + os.sep in path:
            ingest_subagent_file(con, path)
        else:
            ingest_main_file(con, path, label)
        mark_ingested(con, path)
        ingested += 1
    return scanned, ingested


def fetch_remotes(con, cfg, respect_backoff=False):
    """Refresh the local cache for every configured host, one SSH call each.

    A host that is down, unreachable, or has no Claude directory is reported
    and skipped - it must not stop the rest of the report being built. Its
    last-successful-fetch marker is left alone on failure, so the next run
    asks for everything this one would have brought.

    respect_backoff=True (what the receiver uses) skips hosts that are parked
    after an earlier failure, and stops the whole pass once cfg.remote_budget
    seconds have gone on remote work. Between the two, a background pass costs
    near-nothing however badly the remotes are configured. An interactive run
    leaves it False: the user asked for this host *now*, so try it.
    """
    results = []
    now = time.time()
    deadline = time.monotonic() + max(cfg.remote_budget, 1)
    for host in cfg.hosts():
        state = db.get_remote_state(con, host)
        if respect_backoff and state["next_attempt"] > now:
            wait = sources.fmt_duration(state["next_attempt"] - now)
            results.append({"host": host, "files": 0, "error": None,
                            "skipped": f"backing off, retry in {wait}",
                            "fail_count": state["fail_count"]})
            continue
        if respect_backoff and time.monotonic() >= deadline:
            results.append({"host": host, "files": 0, "error": None,
                            "skipped": "remote time budget spent"})
            continue

        last = 0 if cfg.remote_full else state["last_fetch"]
        since = max(last - sources.REMOTE_SKEW_S, 0) if last else 0
        # Never let one host eat the whole budget when others are waiting.
        timeout = cfg.ssh_timeout
        if respect_backoff:
            timeout = max(int(min(timeout, deadline - time.monotonic())), 5)
        started = time.time()
        res = sources.fetch_remote(
            host, since=since, timeout=timeout,
            connect_timeout=cfg.ssh_connect_timeout, ssh_opts=cfg.ssh_options)
        entry = {"host": host, "files": res["files"], "error": res["error"],
                 "elapsed": res["elapsed"]}
        if res["error"]:
            delay = sources.retry_delay(res["kind"], state["fail_count"] + 1)
            db.record_remote_failure(con, host, res["error"], time.time() + delay)
            entry.update(kind=res["kind"], retry_in=sources.fmt_duration(delay))
            sources._warn(f"{host}: {res['error']} "
                          f"({res['kind']}; next try in "
                          f"{sources.fmt_duration(delay)})")
        else:
            db.record_remote_success(con, host, started)
        con.commit()
        results.append(entry)
    return results


def run(force=False, root=None, config=None, skip_remote_fetch=False):
    """Ingest every configured source into metrics.db.

    root=   ingest a single `projects/` directory unlabeled, skipping all
            discovery (the original single-directory behaviour).
    config= a sources.SourceConfig; defaults to sources.json beside this file,
            which is how the receiver picks up extra dirs and remotes.
    skip_remote_fetch=
            ingest whatever is already in the remote cache without contacting
            any host. The receiver uses this to keep minutes-long SSH
            transfers off the lock that live telemetry needs.
    """
    con = db.connect()
    cfg = config if config is not None else sources.SourceConfig.load()

    remotes = []
    if root is not None:
        targets = [sources.Root(root, "", "primary")]
    else:
        remotes = [] if skip_remote_fetch else fetch_remotes(con, cfg)
        roots = sources.discover_local(cfg.extra_locations, cfg.scan_siblings,
                                       cfg.depth)
        # A host that failed this run still has its previous cache on disk;
        # ingest it so one unreachable machine doesn't make its history
        # vanish from the report.
        hosts = (cfg.hosts() if skip_remote_fetch
                 else [r["host"] for r in remotes])
        for host in hosts:
            roots += sources.remote_roots(host)
        roots = sources.dedupe_labels(roots)
        targets = [sources.Root(os.path.join(r.path, "projects"), r.label,
                                r.origin) for r in roots]

    scanned = ingested = 0
    used = []
    for target in targets:
        n_scanned, n_ingested = ingest_tree(con, target.path, target.label,
                                            force)
        scanned += n_scanned
        ingested += n_ingested
        used.append({"label": target.label or "(primary)",
                     "origin": target.origin, "path": target.path,
                     "transcripts": n_scanned})
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("prompts", "api_requests", "tool_calls", "edits", "sessions")}
    con.close()
    out = {"scanned": scanned, "ingested": ingested, "sources": used, **counts}
    if remotes:
        out["remotes"] = remotes
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Ingest Claude Code transcripts into metrics.db.")
    ap.add_argument("--force", action="store_true",
                    help="re-parse every transcript, ignoring the change cache")
    ap.add_argument("--extra-dir", action="append", default=[], metavar="PATH",
                    help="also search PATH for Claude directories; PATH may "
                         "sit well above the real one (repeatable)")
    ap.add_argument("--depth", type=int, default=None, metavar="N",
                    help="levels to search below each extra dir "
                         f"(default {sources.DEFAULT_DEPTH})")
    ap.add_argument("--no-siblings", action="store_true",
                    help="skip sibling .claude* directories next to ~/.claude")
    ap.add_argument("--remote", action="append", default=[], metavar="HOST",
                    help="collect usage from HOST over SSH (repeatable)")
    ap.add_argument("--ssh-config", action="store_true",
                    help="collect from every host named in ~/.ssh/config")
    ap.add_argument("--list-ssh-hosts", action="store_true",
                    help="print the hosts --ssh-config would use, then exit")
    ap.add_argument("--remote-status", action="store_true",
                    help="print each host's last fetch, failures and backoff, "
                         "then exit")
    ap.add_argument("--remote-full", action="store_true",
                    help="re-fetch all remote transcripts, not just new ones")
    ap.add_argument("--ssh-timeout", type=int, default=None, metavar="SECONDS",
                    help="per-host time limit "
                         f"(default {sources.DEFAULT_SSH_TIMEOUT})")
    return ap.parse_args(argv)


def config_from_args(args):
    """sources.json as the base; anything given on the CLI adds to or wins."""
    cfg = sources.SourceConfig.load()
    cfg.extra_locations += [d for d in args.extra_dir
                            if d not in cfg.extra_locations]
    cfg.remotes += [h for h in args.remote if h not in cfg.remotes]
    if args.depth is not None:
        cfg.depth = args.depth
    if args.ssh_timeout is not None:
        cfg.ssh_timeout = args.ssh_timeout
    if args.no_siblings:
        cfg.scan_siblings = False
    if args.ssh_config:
        cfg.use_ssh_config = True
    cfg.remote_full = args.remote_full
    return cfg


def remote_status():
    """Human-readable table of what each host is doing, and why."""
    con = db.connect()
    rows = db.all_remote_state(con)
    con.close()
    if not rows:
        return "No remote host has been contacted yet."
    now = time.time()
    out = [f"{'HOST':<28} {'LAST OK':<20} {'FAILS':>5}  STATUS"]
    for r in rows:
        last = (time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(r["last_fetch"]))
                if r["last_fetch"] else "never")
        if r["next_attempt"] > now:
            status = (f"backing off {sources.fmt_duration(r['next_attempt'] - now)}"
                      f" - {r['last_error']}")
        elif r["last_error"]:
            status = f"will retry - {r['last_error']}"
        else:
            status = "ok"
        out.append(f"{r['host']:<28} {last:<20} {r['fail_count']:>5}  {status}")
    out.append("")
    out.append("A parked host is still reported from its existing cache; "
               "an explicit --remote run ignores the backoff.")
    return "\n".join(out)


if __name__ == "__main__":
    _args = parse_args()
    if _args.list_ssh_hosts:
        print(json.dumps(sources.ssh_config_hosts(), indent=2))
    elif _args.remote_status:
        print(remote_status())
    else:
        print(json.dumps(run(force=_args.force, config=config_from_args(_args)),
                         indent=2))
