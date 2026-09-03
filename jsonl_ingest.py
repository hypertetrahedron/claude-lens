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
import pricing
import sources

# Claude Code's config dir: ~/.claude by default, overridable via
# CLAUDE_CONFIG_DIR. Kept as the single-directory default for callers passing
# root= explicitly; normal runs go through sources.discover_local().
PROJECTS_ROOT = os.path.join(sources.primary_dir(), "projects")


# origin.kind values meaning "a person typed this". Anything else on a modern
# transcript is a harness-injected turn whose usage folds into the prompt that
# caused it.
HUMAN_ORIGINS = frozenset({"human"})

# promptSource values the harness uses for turns it generated itself.
SYSTEM_PROMPT_SOURCES = frozenset({"system"})

# Openers of harness-injected turns, used two ways: receiver.py folds live
# OTel prompts starting with these into their parent, and legacy transcripts
# (which have no origin marker) are filtered by them. Every entry here was
# observed in real transcripts, not guessed.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<teammate-message",
    "<system-reminder>",
    "<command-name>",
    "<local-command-caveat>",
    "<ide_opened_file>",
    "Caveat: The messages below",
    "This session is being continued",
    "Another Claude session sent a message:",
    "[Request interrupted",
)


def _loads(line):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


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


def has_tool_result(msg):
    c = msg.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


# A transcript line can only produce a row if it mentions one of these keys:
# requestId (an API request), "tool_use" (a tool call block - note the closing
# quote, so "tool_use_id" inside a tool_result does not match), filePath (an
# edit result) or origin (a human prompt on a modern transcript). Everything
# else - attachments, ai-title, last-prompt, queue-operation, file-history,
# and tool_result lines carrying whole files - is decoded only to be thrown
# away, and those lines are half of a real transcript by both count and bytes.
# The test is deliberately over-inclusive: it may keep a line that turns out to
# be irrelevant, but it can never drop one that mattered.
def relevant(line):
    return ('"requestId"' in line or '"tool_use"' in line
            or '"filePath"' in line or '"origin"' in line)


def scan_header(path, default_session):
    """A transcript's session id, cwd and vintage, without decoding the file.

    Returns (session_id, legacy, cwd). `legacy` is True when no user entry in
    the file carries an origin marker, meaning human prompts have to be
    recognised by shape instead (see is_human_prompt).

    Only lines that could carry one of the three answers are decoded, and the
    scan stops as soon as all three are known - on a modern transcript that is
    within the first handful of lines, so the header costs nothing. A legacy
    transcript has no origin marker to find and is scanned to the end, but its
    lines are only substring-searched, never parsed.

    Vintage is decided per file rather than by version number: the marker
    appeared partway through the 2.1.x series and the exact build is not worth
    guessing, whereas "does this file use it" is directly observable.
    """
    session = cwd = None
    legacy = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                want_s = session is None and '"sessionId"' in line
                want_c = cwd is None and '"cwd"' in line
                want_o = legacy and '"origin"' in line
                if not (want_s or want_c or want_o):
                    continue
                e = _loads(line)
                if not isinstance(e, dict):
                    continue
                if want_s and e.get("sessionId"):
                    session = e["sessionId"]
                if want_c and e.get("cwd"):
                    cwd = e["cwd"]
                if (want_o and e.get("type") == "user"
                        and isinstance(e.get("origin"), dict)
                        and e["origin"].get("kind") in HUMAN_ORIGINS):
                    legacy = False
                if session is not None and cwd is not None and not legacy:
                    break
    except OSError:
        pass
    return session or default_session, legacy, cwd


def first_prompt_id(path):
    """The first promptId in a subagent transcript, without parsing the rest."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"promptId"' not in line:
                    continue
                e = _loads(line)
                if isinstance(e, dict) and e.get("promptId"):
                    return e["promptId"]
    except OSError:
        return None
    return None


# canonical_model parses and re-assembles a model id; a transcript names the
# same half-dozen models tens of thousands of times, so the answer is cached.
_MODEL_CACHE = {}


def canon_model(raw):
    hit = _MODEL_CACHE.get(raw)
    if hit is None:
        hit = _MODEL_CACHE[raw] = pricing.canonical_model(raw)
    return hit


class Rows:
    """Rows accumulated for one transcript, written in four executemany calls.

    Requests are keyed by requestId rather than appended: one API request spans
    several JSONL lines sharing that id, so the buffer collapses them before
    they reach SQLite - which both removes ~half the inserts and makes the
    surviving row the *last* one seen for that id instead of the first, which
    is what a streamed transcript means by "the state of this request".
    """

    __slots__ = ("prompts", "requests", "tool_calls", "edits")

    def __init__(self):
        self.prompts = []
        self.requests = {}
        self.tool_calls = []
        self.edits = []

    def flush(self, con):
        if self.prompts:
            db.upsert_prompts(con, self.prompts)
            del self.prompts[:]
        if self.requests:
            db.insert_requests_jsonl(con, self.requests.values())
            self.requests.clear()
        if self.tool_calls:
            db.insert_tool_calls(con, self.tool_calls)
            del self.tool_calls[:]
        if self.edits:
            db.insert_edits(con, self.edits)
            del self.edits[:]


def is_human_prompt(entry, legacy=False):
    """Does this user entry start a new prompt?

    Modern transcripts say so outright: origin.kind == "human". Transcripts
    written before that marker existed carry no origin at all, and their real
    prompts are indistinguishable from tool results by field presence alone -
    both are type "user" with a promptId. For those, `legacy` switches to
    recognising a prompt by shape: a user turn that is not a tool result, not
    harness-injected, not a subagent's own turn, and actually has text.

    The loose rule is deliberately NOT applied to modern transcripts. There it
    would invent prompts out of /clear wrappers, compaction summaries and
    "[Request interrupted by user]" notices, all of which are user entries
    with plain text and no origin.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") in HUMAN_ORIGINS
    if not legacy or entry.get("isSidechain"):
        return False
    msg = entry.get("message") or {}
    if has_tool_result(msg):
        return False
    if entry.get("promptSource") in SYSTEM_PROMPT_SOURCES:
        return False
    text = prompt_text(msg).strip()
    return bool(text) and not text.startswith(INJECTED_PREFIXES)


def inline_agent(entry):
    """Agent name for subagent work recorded inline in a main transcript.

    Current CLIs write subagent turns to <session>/subagents/agent-*.jsonl;
    older layouts interleaved them into the main file flagged isSidechain.
    Naming the agent keeps that work attributed to a subagent instead of
    silently inflating the main thread's own usage. Returns None for ordinary
    main-thread entries, which is what `agent=` already expects.
    """
    if not entry.get("isSidechain"):
        return None
    for key in ("attributionAgent", "agentId"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return "subagent"


def handle_assistant(rows, entry, prompt_id, session_id, agent=None):
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
            rows.tool_calls.append((blk["id"], prompt_id, session_id, ts,
                                    name, agent, "jsonl", detail))
    usage = msg.get("usage")
    rid = entry.get("requestId")
    if not usage or not rid:
        return
    cc = usage.get("cache_creation") or {}
    # Store the id canonically with the original alongside: Bedrock and Vertex
    # decorate the same model differently, and pricing is keyed on the plain
    # Anthropic form.
    raw_model = msg.get("model", "?")
    canon, provider = canon_model(raw_model)
    # Tuple order is db.REQUEST_COLS.
    rows.requests[rid] = (
        rid, prompt_id, session_id, ts, canon or raw_model,
        usage.get("input_tokens", 0) or 0,
        usage.get("output_tokens", 0) or 0,
        usage.get("cache_read_input_tokens", 0) or 0,
        usage.get("cache_creation_input_tokens", 0) or 0,
        cc.get("ephemeral_5m_input_tokens", 0) or 0,
        cc.get("ephemeral_1h_input_tokens", 0) or 0,
        None, None,
        "subagent" if agent else "main", agent,
        raw_model, provider)


def handle_tool_result(rows, entry, prompt_id, session_id, agent=None):
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
    rows.edits.append((tuid, prompt_id, session_id, entry.get("timestamp"),
                       r.get("filePath"), kind, add, rem, chars, agent, "jsonl"))


def file_changed(con, path, st=None):
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return False
    row = con.execute("SELECT size, mtime FROM ingest_state WHERE path=?",
                      (path,)).fetchone()
    if row and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1e-6:
        return False
    return True


def mark_ingested(con, path, st=None):
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return
    con.execute("INSERT OR REPLACE INTO ingest_state (path, size, mtime) VALUES (?,?,?)",
                (path, st.st_size, st.st_mtime))


def ingest_main_file(con, path, label="", project_override=None):
    """Ingest one main transcript.

    project_override names the project explicitly instead of deriving it from
    the transcript folder. Cowork uses it: every sandbox's cwd is the same
    ".../outputs" path, so the recorded cwd is suppressed too, letting the
    dashboard fall back to this name rather than showing 14 projects all
    called "outputs".
    """
    project = qualify(label, project_override or
                      os.path.basename(os.path.dirname(path)))
    session, legacy, cwd = scan_header(
        path, os.path.splitext(os.path.basename(path))[0])
    rows = Rows()
    current_pid = None
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        db.upsert_session(con, session, project=project, source_label=label)
        return
    with f:
        for line in f:
            # Legacy transcripts recognise prompts by shape, so every line has
            # to be looked at; modern ones carry markers and can be filtered.
            if not legacy and not relevant(line):
                continue
            e = _loads(line)
            if not isinstance(e, dict):
                continue
            if cwd is None:
                cwd = e.get("cwd") or None
            if is_human_prompt(e, legacy):
                # promptId is universal on the transcripts seen so far; uuid is
                # the fallback for any older build that predates it, and is
                # equally unique per entry.
                current_pid = e.get("promptId") or e.get("uuid")
                if current_pid:
                    rows.prompts.append(
                        (current_pid, session, project, e.get("timestamp"),
                         prompt_text(e.get("message") or {}), "jsonl", 0, None))
            elif e.get("type") == "assistant" and current_pid:
                handle_assistant(rows, e, current_pid, session,
                                 agent=inline_agent(e))
            elif e.get("type") == "user" and current_pid:
                handle_tool_result(rows, e, current_pid, session,
                                   agent=inline_agent(e))
    rows.flush(con)
    # One session row per transcript instead of one per line: the project, cwd
    # and label are the same for every entry in the file, and upsert_session
    # keeps the first cwd it is given anyway.
    db.upsert_session(con, session, project=project,
                      cwd=None if project_override else cwd,
                      source_label=label)


def ingest_subagent_file(con, path):
    session = os.path.basename(os.path.dirname(os.path.dirname(path)))
    agent = os.path.splitext(os.path.basename(path))[0]
    pid = first_prompt_id(path)
    if not pid:
        return
    rows = Rows()
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if not relevant(line):
                continue
            e = _loads(line)
            if not isinstance(e, dict):
                continue
            if e.get("type") == "assistant":
                handle_assistant(rows, e, e.get("promptId") or pid, session,
                                 agent=agent)
            elif e.get("type") == "user":
                handle_tool_result(rows, e, e.get("promptId") or pid, session,
                                   agent=agent)
    rows.flush(con)


def ingest_tree(con, projects_dir, label="", force=False, project_override=None):
    """Ingest every transcript under one `projects/` directory."""
    scanned = ingested = 0
    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    for path in sorted(glob.glob(pattern, recursive=True)):
        scanned += 1
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not force and not file_changed(con, path, st):
            continue
        if os.sep + "subagents" + os.sep in path:
            ingest_subagent_file(con, path)
        else:
            ingest_main_file(con, path, label, project_override)
        mark_ingested(con, path, st)
        ingested += 1
    return scanned, ingested


def apply_session_titles(con, cfg):
    """Name CLI sessions that Claude Desktop launched.

    The desktop app titles every session it starts; the CLI does not. Applying
    those titles turns a UUID column into something readable. Returns how many
    known sessions were named.
    """
    titles = sources.code_session_titles(cfg.code_session_paths)
    if not titles:
        return 0
    known = {r[0] for r in con.execute("SELECT session_id FROM sessions")}
    n = 0
    for sid, title in titles.items():
        if sid in known:
            db.set_session_title(con, sid, title)
            n += 1
    return n


def ingest_cowork(con, cfg, force=False):
    """Ingest Claude Desktop's Cowork sandboxes as one labeled source.

    Every sandbox is its own Claude directory, but they are not 14 separate
    sources to a reader - they are 14 Cowork sessions. They therefore share
    the `cowork` label and are told apart by the desktop app's own session
    title, giving `cowork/Install SearXNG search provider` rather than
    `local_ff2ffe59-.../outputs`.
    """
    scanned = ingested = 0
    sessions = sources.cowork_sessions(cfg.cowork_paths)
    for sess in sessions:
        n_scanned, n_ingested = ingest_tree(
            con, os.path.join(sess.claude_dir, "projects"),
            sources.COWORK_LABEL, force, project_override=sess.title)
        scanned += n_scanned
        ingested += n_ingested
        # The sandbox's signed audit log knows what each completed run cost.
        # Recorded with its run count; collect() spends it only where that
        # count covers every prompt in the session.
        for cli_sid, (cost, runs) in sources.audit_run_costs(
                sess.claude_dir).items():
            db.set_run_cost(con, cli_sid, cost, runs, "cowork-audit")
            db.set_session_title(con, cli_sid, sess.title)
    return scanned, ingested, len(sessions)


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

    if root is None and cfg.cowork:
        n_scanned, n_ingested, n_sessions = ingest_cowork(con, cfg, force)
        if n_sessions:
            scanned += n_scanned
            ingested += n_ingested
            used.append({"label": sources.COWORK_LABEL, "origin": "cowork",
                         "path": ", ".join(sources.cowork_stores(cfg.cowork_paths)),
                         "sessions": n_sessions, "transcripts": n_scanned})

    titled = apply_session_titles(con, cfg) if root is None else 0
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("prompts", "api_requests", "tool_calls", "edits", "sessions")}
    con.close()
    out = {"scanned": scanned, "ingested": ingested, "sources": used, **counts}
    if titled:
        out["titled_sessions"] = titled
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
    ap.add_argument("--no-cowork", action="store_true",
                    help="skip Claude Desktop's Cowork sessions (on by default "
                         "when the desktop app is installed)")
    ap.add_argument("--cowork-dir", action="append", default=[], metavar="PATH",
                    help="Cowork session store to read instead of the "
                         "platform default (repeatable)")
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
    if args.no_cowork:
        cfg.cowork = False
    cfg.cowork_paths += [d for d in args.cowork_dir
                         if d not in cfg.cowork_paths]
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
