"""Backfill / reconciliation ingester: parses Claude Code JSONL transcripts
into metrics.db.

Serves two roles:
1. One-time historical backfill (OTel only captures data from enablement on).
2. Ongoing reconciliation: heals gaps if the OTel receiver was down while
   Claude Code ran. Keying on request_id / tool_use_id, and never letting a
   transcript row overwrite an OTel one, make re-runs and overlap harmless.

Validated schema facts (checked against real transcripts, not assumed):
- Real user prompts: type=="user" with origin.kind=="human"; carry promptId.
  Other origin kinds seen: "task-notification", "coordinator".
- Assistant entries in main files have no promptId -> attribute sequentially
  to the last human prompt above them.
- One API request spans multiple JSONL lines (one per content block) sharing
  requestId; the LAST one carries the complete usage, so a re-parse may raise
  an existing row's figures but never lower them.
- Subagent transcripts live in <proj>/<sessionId>/subagents/agent-<agentId>
  .jsonl and carry the parent prompt's promptId (often a task-notification's).
- Per-request detail: `effort` is top-level on the entry; `speed`,
  `service_tier`, `inference_geo`, `output_tokens_details.thinking_tokens`
  and `server_tool_use.*` live in message.usage; `stop_reason` in message.
- The running context reading arrives as a type=="attachment" entry whose
  attachment is {"type": "total_tokens_reminder", "text": "<total_tokens>N
  tokens left</total_tokens>"} and carries no promptId.
- The Agent tool's result carries agentId / resolvedModel / description;
  its tool_use input carries subagent_type / model / description.

Sources: by default this reads every Claude directory `sources.py` can find -
the primary ~/.claude, sibling .claude* directories, any configured extra
locations, and any configured remote machines (fetched over SSH first). Roots
other than the primary carry a label that is prepended to their project names,
so a dashboard row always shows where it came from.
"""
import argparse
import difflib
import glob
import hashlib
import json
import os
import re
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
#
# `<ide_opened_file>` is deliberately NOT here. The IDE extension prepends it
# as a *separate text block* in front of a perfectly real prompt, so treating
# it as an injection marker discarded the turn the person actually typed. It
# is a wrapper (see IDE_WRAPPERS) and is stripped instead.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<teammate-message",
    "<system-reminder>",
    "<command-name>",
    "<local-command-caveat>",
    "Caveat: The messages below",
    "This session is being continued",
    "Another Claude session sent a message:",
    "[Request interrupted",
)

# Text blocks the editor integration and the harness add around a prompt.
# They are context for the model, not something anyone typed, and they are
# long: left in place they push the real prompt past the 400 characters the
# dashboard shows and make the row unsearchable.
IDE_WRAPPERS = ("<ide_", "<system-reminder>")

# prompts.kind. `origin.kind` answers it outright on modern transcripts; the
# text openers below cover files written before the marker existed and turns
# that arrive through a queue rather than as an origin-marked entry.
KNOWN_KINDS = frozenset({"human", "task-notification", "coordinator", "loop",
                         "scheduled", "team", "other"})
ORIGIN_KIND_ALIASES = {"teammate": "team", "teammate-message": "team",
                       "cron": "scheduled", "schedule": "scheduled"}
KIND_PREFIXES = (
    ("<task-notification>", "task-notification"),
    ("<teammate-message", "team"),
    ("Another Claude session sent a message:", "team"),
    ("<coordinator", "coordinator"),
    ("<loop", "loop"),
    ("<scheduled", "scheduled"),
    ("<cron", "scheduled"),
)

# Tools whose calls change a file on disk.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_TC = {name: i for i, name in enumerate(db.TOOL_CALL_COLS)}
_TC_RESULT_BYTES = _TC["result_bytes"]
_TC_IS_ERROR = _TC["is_error"]

# "<total_tokens>15000000 tokens left</total_tokens>", the harness's own
# running context reading, delivered as an attachment entry.
_TOTAL_TOKENS_RE = re.compile(r"<total_tokens>\s*([0-9][0-9_,]*)")


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


def _text_blocks(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return [c]
    if isinstance(c, list):
        return [b.get("text") or "" for b in c
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def is_wrapper(text):
    """A harness/IDE envelope rather than something a person typed."""
    return text.lstrip().startswith(IDE_WRAPPERS)


def strip_wrapper(text):
    """The inside of a `<tag>...</tag>` envelope, or the text unchanged.

    Deliberately not a regex: a lazily-matched body with an optional closing
    tag backtracks badly, and some of these envelopes carry a whole file.
    """
    stripped = text.strip()
    if not stripped.startswith("<"):
        return stripped
    close = stripped.find(">")
    if close < 1:
        return stripped
    tag = stripped[1:close].split(" ", 1)[0]
    inner = stripped[close + 1:]
    closing = "</%s>" % tag
    if tag and inner.endswith(closing):
        inner = inner[:-len(closing)]
    return inner.strip()


def prompt_text(msg):
    """What the person typed, as far as the transcript can tell.

    A prompt from the IDE extension arrives as several text blocks: one or
    more `<ide_opened_file>` / `<ide_selection>` / `<ide_diagnostics>`
    envelopes describing the editor's state, then the prompt itself. Joining
    them all (which is what this used to do) buried every real prompt behind
    several hundred characters of file path, so the dashboard showed rows of
    identical "The user opened the file ..." text and searching for a phrase
    someone typed found nothing.

    Wrappers are dropped; if that leaves nothing at all - a turn that really
    was only an envelope - the envelope's contents stand in, so the row is
    never blank.
    """
    blocks = _text_blocks(msg)
    kept = [b for b in blocks if b.strip() and not is_wrapper(b)]
    if kept:
        return "\n".join(kept)
    inner = [strip_wrapper(b) for b in blocks if b.strip()]
    return "\n".join(i for i in inner if i)


def prompt_kind(entry, text=None):
    """One of KNOWN_KINDS for a user turn."""
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        kind = str(origin["kind"])
        kind = ORIGIN_KIND_ALIASES.get(kind, kind)
        return kind if kind in KNOWN_KINDS else "other"
    if text is None:
        text = prompt_text(entry.get("message") or {})
    stripped = text.lstrip()
    for prefix, kind in KIND_PREFIXES:
        if stripped.startswith(prefix):
            return kind
    return None


def is_injected_text(msg):
    """True when every text block is an envelope or a known harness opener."""
    blocks = [b for b in _text_blocks(msg) if b.strip()]
    if not blocks:
        return False
    return all(is_wrapper(b) or b.lstrip().startswith(INJECTED_PREFIXES)
               for b in blocks)


def content_len(content):
    """Characters a tool result put in front of the model.

    Results are usually one string; some are a list of text/image blocks, and
    an image's cost is the base64 payload, not the wrapper.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, str):
                total += len(b)
            elif isinstance(b, dict):
                text = b.get("text")
                if isinstance(text, str):
                    total += len(text)
                    continue
                src = b.get("source")
                if isinstance(src, dict) and isinstance(src.get("data"), str):
                    total += len(src["data"])
                else:
                    total += len(json.dumps(b, default=str))
        return total
    return len(json.dumps(content, default=str))


def has_tool_result(msg):
    c = msg.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


# A transcript line can only produce a row if it mentions one of these keys:
# requestId (an API request), "tool_use" (a tool call block - note the closing
# quote, so "tool_use_id" alone does not match), "tool_result" (a result, for
# its size and error flag), filePath (an edit result), origin (a prompt on a
# modern transcript), the attachment carrying the running context reading, a
# compaction marker, or a file-history record. Everything else - ai-title,
# last-prompt, queue-operation, atis-latch - is decoded only to be thrown
# away. The test is deliberately over-inclusive: it may keep a line that turns
# out to be irrelevant, but it can never drop one that mattered.
# Ordered by where the key sits in a line, not by importance: a tool_result
# carrying a whole file is megabytes long and its marker is in the first few
# hundred characters, so testing for it first turns the most expensive lines
# into the cheapest decisions.
_RELEVANT_KEYS = ('"tool_result"', '"requestId"', '"tool_use"', '"origin"',
                  '"filePath"', '"total_tokens_reminder"',
                  '"isCompactSummary"', '"compactMetadata"',
                  '"compact-boundary"', '"file-history-')

# Every marker above appears within the entry's own envelope, ahead of any
# large payload, on every transcript seen. The head is therefore enough to
# *accept* a line; only a line the head rejects is scanned in full, which
# keeps the guarantee that nothing relevant is ever dropped.
_HEAD = 4096


def relevant(line):
    head = line[:_HEAD]
    for key in _RELEVANT_KEYS:
        if key in head:
            return True
    if len(line) <= _HEAD:
        return False
    for key in _RELEVANT_KEYS:
        if key in line:
            return True
    return False


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
    """Rows accumulated for one transcript, written in one executemany each.

    Requests are keyed by requestId rather than appended: one API request spans
    several JSONL lines sharing that id, so the buffer collapses them before
    they reach SQLite - which both removes ~half the inserts and makes the
    surviving row the *last* one seen for that id instead of the first, which
    is what a streamed transcript means by "the state of this request".

    Tool calls are keyed the same way and for the same reason: the size and
    error flag of a result arrive on a later line than the call itself, so the
    row has to still be reachable when they do.
    """

    __slots__ = ("prompts", "requests", "tool_calls", "edits", "agents",
                 "events", "pending_agents")

    def __init__(self):
        self.prompts = []
        self.requests = {}
        self.tool_calls = {}
        self.edits = []
        self.agents = {}
        self.events = []
        self.pending_agents = {}

    def flush(self, con):
        if self.prompts:
            db.upsert_prompts(con, self.prompts)
            del self.prompts[:]
        if self.requests:
            db.insert_requests_jsonl(con, self.requests.values())
            self.requests.clear()
        if self.tool_calls:
            db.insert_tool_calls(con, self.tool_calls.values())
            self.tool_calls.clear()
        if self.edits:
            db.insert_edits(con, self.edits)
            del self.edits[:]
        if self.agents:
            db.upsert_agents(con, self.agents.values())
            self.agents.clear()
        if self.events:
            db.insert_events(con, self.events)
            del self.events[:]
        self.pending_agents.clear()


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
    if is_injected_text(msg):
        return False
    return bool(prompt_text(msg).strip())


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


def handle_assistant(rows, entry, prompt_id, session_id, agent=None,
                     switches=None):
    """Tool calls and the API request an assistant entry records.

    switches= a SwitchTracker for the session's main conversation; it turns a
    change of model, effort or speed between consecutive requests into a
    session_event, which is what later explains a cache miss.
    """
    msg = entry.get("message") or {}
    ts = entry.get("timestamp")
    for blk in msg.get("content") or []:
        if not (isinstance(blk, dict) and blk.get("type") == "tool_use"
                and blk.get("id")):
            continue
        name = blk.get("name", "?")
        inp = blk.get("input")
        detail = inp.get("skill") if (name == "Skill"
                                      and isinstance(inp, dict)) else None
        try:
            input_bytes = len(json.dumps(inp, default=str)) if inp is not None else 0
        except (TypeError, ValueError):
            input_bytes = None
        # Column order is db.TOOL_CALL_COLS; kept mutable so the result's size
        # and error flag can be filled in when its line arrives.
        rows.tool_calls[blk["id"]] = [
            blk["id"], prompt_id, session_id, ts, name, agent, "jsonl", detail,
            input_bytes, None, None, None, None]
        if name == "Agent" and isinstance(inp, dict):
            # The launch knows what was asked for; the result (below) knows
            # which agent id and model it actually got.
            rows.pending_agents[blk["id"]] = (
                inp.get("subagent_type"), inp.get("model"),
                inp.get("description"), prompt_id, session_id, ts)
    usage = msg.get("usage")
    rid = entry.get("requestId")
    if not usage or not rid:
        return
    cc = usage.get("cache_creation") or {}
    otd = usage.get("output_tokens_details") or {}
    stu = usage.get("server_tool_use") or {}
    # Store the id canonically with the original alongside: Bedrock and Vertex
    # decorate the same model differently, and pricing is keyed on the plain
    # Anthropic form.
    raw_model = msg.get("model", "?")
    canon, provider = canon_model(raw_model)
    model = canon or raw_model
    inp_t = usage.get("input_tokens", 0) or 0
    read_t = usage.get("cache_read_input_tokens", 0) or 0
    create_t = usage.get("cache_creation_input_tokens", 0) or 0
    effort = entry.get("effort")
    speed = usage.get("speed")
    # server_tool_use is a map of counters (web_search_requests,
    # web_fetch_requests, ...); the total is what the dashboard charts, and
    # summing keeps a counter added later from being silently dropped.
    server_calls = sum(v for v in stu.values() if isinstance(v, int)) if stu else None
    # Tuple order is db.REQUEST_COLS.
    rows.requests[rid] = (
        rid, prompt_id, session_id, ts, model,
        inp_t,
        usage.get("output_tokens", 0) or 0,
        read_t, create_t,
        cc.get("ephemeral_5m_input_tokens", 0) or 0,
        cc.get("ephemeral_1h_input_tokens", 0) or 0,
        None, None,
        "subagent" if agent else "main", agent,
        raw_model, provider,
        effort, speed, otd.get("thinking_tokens"),
        msg.get("stop_reason"), server_calls,
        usage.get("service_tier"), usage.get("inference_geo"),
        inp_t + read_t + create_t, None, None)
    if switches is not None and agent is None:
        switches.note(rows, rid, ts, prompt_id, model, effort, speed)


class SwitchTracker:
    """Turns consecutive main-conversation requests into switch events.

    The comparison has to survive an incremental re-ingest, where the file
    being read starts in the middle of a session: the previous request is then
    in the database, not in this parse. It is fetched lazily on the first
    request seen, bounded by that request's timestamp, so a full re-parse (a
    forced run, or the v8 migration) does not compare a session's first
    request against its own last one.
    """

    __slots__ = ("session_id", "con", "seeded", "model", "effort", "speed",
                 "last_rid")

    def __init__(self, con, session_id):
        self.con = con
        self.session_id = session_id
        self.seeded = False
        self.model = self.effort = self.speed = None
        self.last_rid = None

    def _seed(self, ts):
        self.seeded = True
        if not (self.con and self.session_id and ts):
            return
        row = self.con.execute(
            """SELECT model, effort, speed FROM api_requests
               WHERE session_id=? AND query_source='main' AND ts < ?
               ORDER BY ts DESC LIMIT 1""",
            (self.session_id, ts)).fetchone()
        if row:
            self.model, self.effort, self.speed = row

    def note(self, rows, rid, ts, prompt_id, model, effort, speed):
        if rid == self.last_rid:
            return                      # same request, another content block
        if not self.seeded:
            self._seed(ts)
        self.last_rid = rid
        for kind, old, new in (("model_switch", self.model, model),
                               ("effort_switch", self.effort, effort),
                               ("speed_switch", self.speed, speed)):
            if old and new and old != new:
                rows.events.append((self.session_id, prompt_id, ts, kind,
                                    f"{old}->{new}", None))
        self.model = model or self.model
        self.effort = effort or self.effort
        self.speed = speed or self.speed


def handle_tool_result_blocks(rows, entry):
    """Size and error flag of the results riding on one user entry.

    Both sources know half of a tool call: the transcript has how much text
    came back, OTel has how long it took. The result's own block carries
    `is_error` and the content; a Bash result's real weight is in
    `toolUseResult.stdout`/`stderr`, which the block summarises.
    """
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    result = entry.get("toolUseResult")
    extra = 0
    errored = False
    if isinstance(result, dict):
        extra = len(result.get("stdout") or "") + len(result.get("stderr") or "")
        errored = bool(result.get("error") or result.get("isError"))
    for blk in content:
        if not (isinstance(blk, dict) and blk.get("type") == "tool_result"):
            continue
        row = rows.tool_calls.get(blk.get("tool_use_id"))
        if row is None:
            continue        # the call itself is in a file we are not reading
        size = max(content_len(blk.get("content")), extra)
        if row[_TC_RESULT_BYTES] is None:
            row[_TC_RESULT_BYTES] = size
        if bool(blk.get("is_error")) or errored:
            row[_TC_IS_ERROR] = 1
        elif row[_TC_IS_ERROR] is None:
            row[_TC_IS_ERROR] = 0


def handle_agent_result(rows, entry, session_id):
    """The Agent tool's result: which agent id and model the launch produced.

    Verified against real transcripts: `toolUseResult` carries `agentId`,
    `resolvedModel`, `description` and `status`, and the subagent's own
    transcript is `<session>/subagents/agent-<agentId>.jsonl`, which is what
    makes the two joinable.
    """
    result = entry.get("toolUseResult")
    if not (isinstance(result, dict) and result.get("agentId")):
        return
    tuid = None
    for blk in (entry.get("message") or {}).get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_result":
            tuid = blk.get("tool_use_id")
            break
    pending = rows.pending_agents.get(tuid) or (None,) * 6
    subagent_type, requested, description, prompt_id, sess, ts = pending
    agent_id = result["agentId"]
    rows.agents[agent_id] = (
        agent_id, sess or session_id, prompt_id, ts or entry.get("timestamp"),
        subagent_type, requested, result.get("resolvedModel"),
        result.get("description") or description, tuid, "jsonl")


def handle_attachment(rows, entry, prompt_id, session_id):
    """Context readings the harness attaches to a turn.

    Real shape (verified): a top-level entry of type "attachment" whose
    `attachment` object is `{"type": "total_tokens_reminder", "text":
    "<total_tokens>15000000 tokens left</total_tokens>"}`. The entry carries
    no promptId, so it is attributed to the turn being read when it appears.
    """
    att = entry.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "total_tokens_reminder":
        return
    m = _TOTAL_TOKENS_RE.search(att.get("text") or "")
    if not m:
        return
    rows.events.append((session_id, prompt_id, entry.get("timestamp"),
                        "context", None,
                        int(m.group(1).replace(",", "").replace("_", ""))))


def compaction_detail(entry, text=None):
    """Name the compaction marker on this entry, or None.

    Every form seen documented or in the wild is accepted, because no
    transcript on the machine this was written against had been compacted:
    the flag on the summary turn, the metadata block, the dedicated boundary
    entry, and the sentence the harness opens a continued session with.
    """
    if entry.get("isCompactSummary"):
        return "isCompactSummary"
    if entry.get("compactMetadata"):
        return "compactMetadata"
    if entry.get("type") in ("compact-boundary", "summary"):
        return entry["type"]
    if text and text.lstrip().startswith("This session is being continued"):
        return "continued"
    return None


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


class SessionInfo:
    """Descriptors that belong to the session rather than to any one entry.

    Every entry repeats them, so the last one wins for anything that can
    change mid-session (the branch, an upgraded CLI, a permission mode) and
    the timestamps keep their extremes.
    """

    __slots__ = ("git_branch", "cli_version", "entrypoint", "permission_mode",
                 "first_ts", "last_ts")
    _FIELDS = (("git_branch", "gitBranch"), ("cli_version", "version"),
               ("entrypoint", "entrypoint"),
               ("permission_mode", "permissionMode"))

    def __init__(self):
        for name in self.__slots__:
            setattr(self, name, None)

    def note(self, entry):
        ts = entry.get("timestamp")
        if ts:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        for attr, key in self._FIELDS:
            value = entry.get(key)
            if value:
                setattr(self, attr, value)


def record_injected_prompt(rows, entry, session, project, last_human):
    """A harness turn that a subagent transcript will reference by promptId.

    A background Agent reports back through a `<task-notification>` turn, and
    the subagent's own transcript is attributed to *that* prompt id. Without a
    row for it the subagent's spend has nowhere to hang; with one, and with
    canonical_id pointing at the human prompt that started the work, the
    dashboard folds it back where a reader expects to find it.

    Returns the kind, or None when the entry is an ordinary turn.
    """
    pid = entry.get("promptId")
    if not pid:
        return None
    msg = entry.get("message") or {}
    if has_tool_result(msg):
        return None
    text = prompt_text(msg)
    kind = prompt_kind(entry, text)
    if kind is None or kind == "human":
        return None
    ts = entry.get("timestamp")
    # Fold into the most recent human prompt at or before this turn; a
    # notification that somehow precedes every human prompt stands alone.
    canonical = pid
    if last_human and last_human[0]:
        if not ts or not last_human[1] or last_human[1] <= ts:
            canonical = last_human[0]
    rows.prompts.append((pid, session, project, ts, text, "jsonl", 1,
                         canonical, kind))
    if kind in ("loop", "scheduled", "team"):
        rows.events.append((session, pid, ts, kind, None, None))
    return kind


def note_file_history(fh, entry):
    """Collect the file-version bookkeeping a transcript records.

    Two entry shapes, both verified against real transcripts:
      file-history-delta    -> {trackingPath, backup:{backupFileName, version,
                                backupTime, realParentDir}}
      file-history-snapshot -> {snapshot:{trackedFileBackups: {path: backup}}}
    `backupFileName` is frequently null on a delta and present in the
    snapshot, so both are read and the answer is keyed by absolute path.
    """
    if entry.get("type") == "file-history-delta":
        items = [(entry.get("trackingPath"), entry.get("backup"))]
    else:
        snap = entry.get("snapshot")
        if not isinstance(snap, dict):
            return
        tracked = snap.get("trackedFileBackups")
        items = list(tracked.items()) if isinstance(tracked, dict) else []
    for tracked_path, backup in items:
        if not (tracked_path and isinstance(backup, dict)):
            continue
        parent = backup.get("realParentDir")
        full = (os.path.join(parent, os.path.basename(tracked_path))
                if parent else tracked_path)
        record = fh.setdefault(full, {})
        version = backup.get("version")
        if isinstance(version, int) and backup.get("backupTime"):
            record[version] = backup["backupTime"]


def ingest_main_file(con, path, label="", project_override=None, fh_maps=None):
    """Ingest one main transcript.

    project_override names the project explicitly instead of deriving it from
    the transcript folder. Cowork uses it: every sandbox's cwd is the same
    ".../outputs" path, so the recorded cwd is suppressed too, letting the
    dashboard fall back to this name rather than showing 14 projects all
    called "outputs".

    fh_maps= a dict the caller collects file-version bookkeeping into, keyed by
    session id, for the file-history pass at the end of the tree.
    """
    project = qualify(label, project_override or
                      os.path.basename(os.path.dirname(path)))
    session, legacy, cwd = scan_header(
        path, os.path.splitext(os.path.basename(path))[0])
    rows = Rows()
    info = SessionInfo()
    switches = SwitchTracker(con, session)
    fh = None if fh_maps is None else fh_maps.setdefault(session, {})
    current_pid = None
    last_human = None
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
            info.note(e)
            if cwd is None:
                cwd = e.get("cwd") or None
            etype = e.get("type")
            if etype == "attachment":
                handle_attachment(rows, e, current_pid, session)
                continue
            if etype in ("file-history-delta", "file-history-snapshot"):
                if fh is not None:
                    note_file_history(fh, e)
                continue
            if is_human_prompt(e, legacy):
                # promptId is universal on the transcripts seen so far; uuid is
                # the fallback for any older build that predates it, and is
                # equally unique per entry.
                current_pid = e.get("promptId") or e.get("uuid")
                if current_pid:
                    text = prompt_text(e.get("message") or {})
                    rows.prompts.append(
                        (current_pid, session, project, e.get("timestamp"),
                         text, "jsonl", 0, None, "human"))
                    last_human = (current_pid, e.get("timestamp"))
                    _note_compaction(rows, e, session, current_pid, text)
            elif etype == "assistant" and current_pid:
                handle_assistant(rows, e, current_pid, session,
                                 agent=inline_agent(e), switches=switches)
            elif etype == "user":
                record_injected_prompt(rows, e, session, project, last_human)
                if current_pid:
                    agent = inline_agent(e)
                    handle_tool_result(rows, e, current_pid, session, agent=agent)
                    handle_tool_result_blocks(rows, e)
                    handle_agent_result(rows, e, session)
                _note_compaction(rows, e, session, current_pid)
            else:
                _note_compaction(rows, e, session, current_pid)
    rows.flush(con)
    # One session row per transcript instead of one per line: the project, cwd
    # and label are the same for every entry in the file, and upsert_session
    # keeps the first cwd it is given anyway.
    db.upsert_session(con, session, project=project,
                      cwd=None if project_override else cwd,
                      source_label=label, git_branch=info.git_branch,
                      cli_version=info.cli_version, entrypoint=info.entrypoint,
                      permission_mode=info.permission_mode,
                      transcript_path=path, first_ts=info.first_ts,
                      last_ts=info.last_ts)


def _note_compaction(rows, entry, session, prompt_id, text=None):
    detail = compaction_detail(entry, text)
    if detail:
        rows.events.append((session, prompt_id, entry.get("timestamp"),
                            "compact", detail, None))


def agent_id_from_path(path):
    """The CLI's agentId, from `.../subagents/agent-<agentId>.jsonl`.

    Stored without the filename's prefix so `agents.agent_id` and the
    `agent_name` on requests, tool calls and edits are the same string.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem[6:] if stem.startswith("agent-") else stem


def ingest_subagent_file(con, path):
    session = os.path.basename(os.path.dirname(os.path.dirname(path)))
    agent = agent_id_from_path(path)
    pid = first_prompt_id(path)
    if not pid:
        return
    rows = Rows()
    subagent_type = resolved = None
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
                # attributionAgent is the subagent_type ("general-purpose"),
                # which is worth having even when the launch record is gone.
                subagent_type = subagent_type or e.get("attributionAgent")
                resolved = resolved or (e.get("message") or {}).get("model")
                handle_assistant(rows, e, e.get("promptId") or pid, session,
                                 agent=agent)
            elif e.get("type") == "user":
                handle_tool_result(rows, e, e.get("promptId") or pid, session,
                                   agent=agent)
                handle_tool_result_blocks(rows, e)
    # Fills only what the launch record did not already establish.
    rows.agents[agent] = (agent, session, pid, None, subagent_type, None,
                          resolved, None, None, "subagent-file")
    rows.flush(con)


def is_subagent_transcript(path):
    """Main transcript or subagent transcript, by layout then by shape."""
    if os.sep + "subagents" + os.sep in path or "/subagents/" in path:
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return False
    entry = _loads(first)
    return bool(isinstance(entry, dict) and entry.get("isSidechain")
                and entry.get("agentId"))


def ingest_file(con, path, label="", project_override=None):
    """Ingest exactly one transcript and commit it.

    The SessionEnd hook's entry point: it knows the path of the file that just
    finished and nothing else, so the shape is detected rather than assumed.
    """
    fh_maps = {}
    if is_subagent_transcript(path):
        ingest_subagent_file(con, path)
    else:
        ingest_main_file(con, path, label, project_override, fh_maps)
    mark_ingested(con, path)
    recover_file_history(con, claude_dir_of(path), fh_maps)
    con.commit()
    return True


def ingest_tree(con, projects_dir, label="", force=False, project_override=None):
    """Ingest every transcript under one `projects/` directory."""
    scanned = ingested = 0
    fh_maps = {}
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
            ingest_main_file(con, path, label, project_override, fh_maps)
        mark_ingested(con, path, st)
        ingested += 1
        # Commit per transcript: a run interrupted halfway (or a receiver
        # restarted under it) keeps everything already parsed instead of
        # rolling back an hour of work.
        con.commit()
    recover_file_history(con, os.path.dirname(os.path.abspath(projects_dir)),
                         fh_maps)
    con.commit()
    return scanned, ingested


# ---------------------------------------------------------------------------
# File-history recovery
#
# A subagent's Edit and Write calls are recorded in its own transcript, but
# the user entries there carry no `toolUseResult` - so the structuredPatch
# that every edits row is built from simply is not written for them, and the
# 110 subagent edits on the machine this was developed against produced zero
# rows. What *is* written is Claude Code's own undo history:
#
#   ~/.claude/file-history/<session-id>/<hash>@v<N>
#
# a full copy of the file at each checkpoint, where <hash> is the first 16 hex
# digits of sha256 of the file's absolute path (verified) and the highest
# version equals the file on disk. Diffing consecutive versions recovers the
# lines that changed. See ROADMAP.md for what this can and cannot attribute.
# ---------------------------------------------------------------------------

# Subagent edit calls that produced no `edits` row - the gap this pass exists
# to fill. Built from EDIT_TOOLS so the two never drift apart.
_EDIT_GAP_SQL = """SELECT COUNT(*) FROM tool_calls t
       WHERE t.session_id=? AND t.agent_name IS NOT NULL
         AND t.tool_name IN (%s)
         AND NOT EXISTS (SELECT 1 FROM edits e
                         WHERE e.tool_use_id = t.tool_use_id)""" % ",".join(
    "'%s'" % name for name in sorted(EDIT_TOOLS))


def claude_dir_of(path):
    """The Claude directory containing a transcript path, or None."""
    cur = os.path.dirname(os.path.abspath(path))
    for _ in range(6):
        if os.path.basename(cur) == "projects":
            return os.path.dirname(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def backup_hash(file_path):
    """The `<hash>` half of a file-history backup name for an absolute path."""
    return hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]


def _count_diff(old_lines, new_lines):
    added = removed = chars = 0
    for line in difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
            chars += len(line) - 1
        elif line.startswith("-"):
            removed += 1
    return added, removed, chars


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return None


def recover_file_history(con, claude_dir, fh_maps):
    """Recover subagent edits from the file-version snapshots on disk.

    Only sessions with subagent edit calls that produced no `edits` row are
    considered, and only files that session has no row for at all, so nothing
    already measured from a structuredPatch is counted twice.

    Returns the number of rows written.
    """
    if not claude_dir or not fh_maps:
        return 0
    root = os.path.join(claude_dir, "file-history")
    if not os.path.isdir(root):
        return 0
    written = 0
    for session, tracked in fh_maps.items():
        if not tracked:
            continue
        session_dir = os.path.join(root, session)
        if not os.path.isdir(session_dir):
            continue
        # Is anything actually missing for this session? A session whose edits
        # all came from tool results has nothing to recover.
        gap = con.execute(_EDIT_GAP_SQL, (session,)).fetchone()[0]
        if not gap:
            continue
        try:
            names = os.listdir(session_dir)
        except OSError:
            continue
        by_hash = {}
        for name in names:
            head, _, ver = name.partition("@v")
            if ver.isdigit():
                by_hash.setdefault(head, []).append((int(ver), name))
        prompts = con.execute(
            """SELECT ts, prompt_id FROM prompts
               WHERE session_id=? AND ts IS NOT NULL ORDER BY ts""",
            (session,)).fetchall()
        for file_path, versions in tracked.items():
            digest = backup_hash(file_path)
            available = sorted(by_hash.get(digest, []))
            if len(available) < 2:
                continue        # only one checkpoint: nothing to diff against
            have = con.execute(
                "SELECT 1 FROM edits WHERE session_id=? AND file_path=? LIMIT 1",
                (session, file_path)).fetchone()
            if have:
                continue        # already measured from a tool result
            prev = _read_lines(os.path.join(session_dir, available[0][1]))
            for number, name in available[1:]:
                current = _read_lines(os.path.join(session_dir, name))
                if prev is None or current is None:
                    prev = current
                    continue
                added, removed, chars = _count_diff(prev, current)
                prev = current
                if not (added or removed):
                    continue
                ts = versions.get(number)
                if not ts:
                    try:
                        ts = time.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z",
                            time.gmtime(os.path.getmtime(
                                os.path.join(session_dir, name))))
                    except OSError:
                        ts = None
                prompt_id = None
                for p_ts, p_id in prompts:
                    if ts and p_ts <= ts:
                        prompt_id = p_id
                    else:
                        break
                db.insert_edit(
                    con, "fh:%s:%s@v%d" % (session, digest, number), prompt_id,
                    session, ts, file_path, "update", added, removed, chars,
                    None, "file-history")
                written += 1
    return written


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


def _open_db(db_path=None):
    """Open the database, honouring --db / $CLAUDE_LENS_DB / sources.json.

    When nothing overrides the location, connect() is called with no argument
    so that its own default still decides - which is what lets a test (or any
    embedding caller) swap db.connect for a fixture and have the ingester use
    it.
    """
    resolved = db.resolve_path(db_path)
    return db.connect() if resolved == db.DB_PATH else db.connect(resolved)


def run(force=False, root=None, config=None, skip_remote_fetch=False,
        db_path=None):
    """Ingest every configured source into metrics.db.

    root=   ingest a single `projects/` directory unlabeled, skipping all
            discovery (the original single-directory behaviour).
    db_path= where metrics.db lives; defaults to db.resolve_path().
    config= a sources.SourceConfig; defaults to sources.json beside this file,
            which is how the receiver picks up extra dirs and remotes.
    skip_remote_fetch=
            ingest whatever is already in the remote cache without contacting
            any host. The receiver uses this to keep minutes-long SSH
            transfers off the lock that live telemetry needs.
    """
    con = _open_db(db_path)
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
              for t in ("prompts", "api_requests", "tool_calls", "edits",
                        "sessions", "agents", "session_events")}
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
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to write (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, else metrics.db "
                         "beside this script)")
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


def remote_status(db_path=None):
    """Human-readable table of what each host is doing, and why."""
    con = _open_db(db_path)
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
        print(remote_status(_args.db))
    else:
        print(json.dumps(run(force=_args.force, config=config_from_args(_args),
                             db_path=_args.db),
                         indent=2))
