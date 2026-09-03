"""SQLite storage shared by the OTel receiver, the JSONL backfill, and the
dashboard generator.

Dedupe strategy:
- api_requests keyed by the Anthropic request_id. Both sources see the same id,
  so OTel + JSONL never double count. OTel rows win on conflict (they carry an
  authoritative cost_usd); a JSONL row can only overwrite another JSONL row,
  and only when it carries at least as many output tokens (schema v8) - which
  is how a re-parse corrects a row written from a half-streamed request.
- tool_calls keyed by tool_use_id (toolu_...), present in both sources.
- prompts keyed by prompt_id; conflicting inserts merge, filling missing fields.
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")

# Environment override for the database location, so a machine whose checkout
# sits on a network share can keep SQLite's WAL on local disk.
DB_ENV_VAR = "CLAUDE_LENS_DB"


def resolve_path(explicit=None):
    """Where metrics.db lives: --db, then $CLAUDE_LENS_DB, then sources.json.

    Every entry point resolves through this so one setting moves the database
    for the ingester, the receiver, the dashboard and the digest at once.
    `connect()` keeps its own default, so callers that pass a path explicitly
    (tests, migrations against a copy) are unaffected.
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(DB_ENV_VAR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    try:
        import sources
        configured = sources.config_db_path()
    except Exception:                       # sources.py absent or unreadable
        configured = None
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return DB_PATH

# ---------------------------------------------------------------------------
# Schema versioning (stored in SQLite's PRAGMA user_version).
#
# A database with user_version 0 predates version tracking and is treated as
# VERSION 1. connect() migrates any older database up to SCHEMA_VERSION before
# returning, so every entry point (ingester, receiver, dashboard, digest) is
# covered automatically.
#
# To change the schema: add a _migrate_to_N() function, register it in
# MIGRATIONS, bump SCHEMA_VERSION, update the CREATE TABLE statements below to
# match (fresh databases are created current and stamped directly), and add a
# line to the changelog here and in README.md.
#
# Version changelog:
#   1 — initial schema (implicit; user_version 0)
#   2 — tool_calls.detail column (skill name for Skill calls); tool-name
#       upserts so transcript-derived specifics (mcp__server__tool, skill
#       names) upgrade generic live-telemetry rows; clears ingest_state to
#       force a one-time transcript re-parse that backfills both.
#   3 — multi-source ingest: sessions.source_label records which Claude
#       directory (or remote machine) a session came from, and remote_state
#       tracks the last successful SSH fetch per host so transfers stay
#       incremental. Existing rows keep an empty label = the primary
#       ~/.claude, which is exactly what they were.
#   4 - remote_state.fail_count / next_attempt: a host that cannot be reached
#       (missing key, powered off, no Claude directory) is backed off instead
#       of retried on every pass, so a misconfigured remote costs the
#       background receiver nothing.
#   5 - no column change: clears ingest_state to force one re-parse of every
#       transcript. Transcripts written before the origin marker existed had
#       their human prompts go unrecognised, so their API usage was dropped
#       entirely; the ingester now recognises them, and only a re-parse can
#       backfill what was missed.
#   6 - sessions.title (the human-readable name Claude Desktop gives a
#       session), run_cost (CLI-reported cost per session, used only where it
#       provably covers every run), and an index on api_requests.session_id
#       so per-session work stops being a full scan.
#   7 - provider awareness: api_requests.provider and .model_raw. Claude Code
#       can reach the same model through the Anthropic API, Bedrock or Vertex,
#       and each decorates the id differently; ids are now stored canonically
#       with the original kept alongside. Existing rows are normalised in
#       place, which is the only way to reprice Bedrock history - api_requests
#       rows are insert-or-ignore, so a re-parse would never touch them.
#   8 - what a request cost and what it was doing: api_requests gains effort,
#       speed, thinking_tokens, stop_reason, server_tool_requests,
#       service_tier, inference_geo, context_tokens, cost_basis and error;
#       tool_calls gains input_bytes, result_bytes, is_error, duration_ms and
#       error_type; sessions gains git_branch, cli_version, entrypoint,
#       permission_mode, transcript_path, first_ts and last_ts; prompts gains
#       kind. New tables `agents` (one row per subagent launch, with the
#       requested and resolved model) and `session_events` (context readings,
#       compactions, model/effort/speed switches). Transcript inserts stop
#       being insert-or-ignore: a JSONL row now updates an existing JSONL row
#       when it carries at least as many output tokens, so re-parsing corrects
#       rows captured mid-stream. Clears ingest_state to force that re-parse,
#       and strips the `agent-` filename prefix from agent_name so subagent
#       rows join the new agents table on agentId.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    prompt_id     TEXT PRIMARY KEY,
    session_id    TEXT,
    project       TEXT,
    ts            TEXT,
    text          TEXT DEFAULT '',
    source        TEXT,
    injected      INTEGER DEFAULT 0,
    canonical_id  TEXT,
    kind          TEXT
);
CREATE TABLE IF NOT EXISTS api_requests (
    request_id        TEXT PRIMARY KEY,
    prompt_id         TEXT,
    session_id        TEXT,
    ts                TEXT,
    model             TEXT,
    input_tokens      INTEGER DEFAULT 0,
    output_tokens     INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_create_tokens INTEGER DEFAULT 0,
    cache_5m_tokens   INTEGER DEFAULT 0,
    cache_1h_tokens   INTEGER DEFAULT 0,
    cost_usd          REAL,
    duration_ms       INTEGER,
    query_source      TEXT,
    agent_name        TEXT,
    source            TEXT,
    model_raw         TEXT,
    provider          TEXT,
    effort            TEXT,
    speed             TEXT,
    thinking_tokens   INTEGER,
    stop_reason       TEXT,
    server_tool_requests INTEGER,
    service_tier      TEXT,
    inference_geo     TEXT,
    context_tokens    INTEGER,
    cost_basis        TEXT,
    error             TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    prompt_id   TEXT,
    session_id  TEXT,
    ts          TEXT,
    tool_name   TEXT,
    agent_name  TEXT,
    source      TEXT,
    detail      TEXT,
    input_bytes  INTEGER,
    result_bytes INTEGER,
    is_error     INTEGER,
    duration_ms  INTEGER,
    error_type   TEXT
);
CREATE TABLE IF NOT EXISTS edits (
    tool_use_id  TEXT PRIMARY KEY,
    prompt_id    TEXT,
    session_id   TEXT,
    ts           TEXT,
    file_path    TEXT,
    kind         TEXT,
    lines_added  INTEGER DEFAULT 0,
    lines_removed INTEGER DEFAULT 0,
    chars_added  INTEGER DEFAULT 0,
    agent_name   TEXT,
    source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_edit_prompt ON edits(prompt_id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project      TEXT,
    cwd          TEXT,
    source_label TEXT DEFAULT '',
    title        TEXT,
    git_branch      TEXT,
    cli_version     TEXT,
    entrypoint      TEXT,
    permission_mode TEXT,
    transcript_path TEXT,
    first_ts        TEXT,
    last_ts         TEXT
);
-- One row per subagent launch. `agent_id` is the CLI's agentId, which is also
-- what api_requests.agent_name / tool_calls.agent_name carry for work done in
-- <session>/subagents/agent-<agentId>.jsonl, so the two join directly.
CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    session_id     TEXT,
    prompt_id      TEXT,
    ts             TEXT,
    subagent_type  TEXT,
    requested_model TEXT,
    resolved_model TEXT,
    description    TEXT,
    tool_use_id    TEXT,
    source         TEXT
);
-- Things that happened to a session rather than to one request: how much
-- context was in play, when it was compacted, and when the model, effort or
-- speed changed under it. `value` is kind-specific (tokens for "context").
CREATE TABLE IF NOT EXISTS session_events (
    id         INTEGER PRIMARY KEY,
    session_id TEXT,
    prompt_id  TEXT,
    ts         TEXT,
    kind       TEXT,
    detail     TEXT,
    value      INTEGER
);
-- Re-ingesting a transcript must not multiply its events, and the table has
-- no natural key, so identity is the whole tuple. Every column is COALESCEd:
-- SQLite treats NULLs as distinct in a UNIQUE index, so a bare column here
-- would let an event with no timestamp - a compaction summary entry carries
-- none - be re-inserted on every re-parse until the session claimed hundreds
-- of compactions.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_uniq
    ON session_events(COALESCE(session_id,''), COALESCE(ts,''),
                      COALESCE(kind,''), COALESCE(detail,''),
                      COALESCE(value,-1));
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id, ts);
-- Cost a CLI reported for a whole session, with the number of runs it covers.
-- Only trusted when `runs` matches the prompts actually seen for that session;
-- a partial record would silently under-report. See build_dashboard.collect().
CREATE TABLE IF NOT EXISTS run_cost (
    session_id TEXT PRIMARY KEY,
    cost_usd   REAL,
    runs       INTEGER,
    source     TEXT
);
CREATE TABLE IF NOT EXISTS remote_state (
    host         TEXT PRIMARY KEY,
    last_fetch   REAL,
    last_error   TEXT,
    fail_count   INTEGER DEFAULT 0,
    next_attempt REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingest_state (
    path  TEXT PRIMARY KEY,
    size  INTEGER,
    mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_req_prompt ON api_requests(prompt_id);
CREATE INDEX IF NOT EXISTS idx_tool_prompt ON tool_calls(prompt_id);
CREATE INDEX IF NOT EXISTS idx_prompt_ts ON prompts(ts);
CREATE INDEX IF NOT EXISTS idx_req_session ON api_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_req_ts ON api_requests(ts);
"""


def _migrate_to_2(con):
    """tool_calls.detail + one-time re-parse to backfill skill/MCP names."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(tool_calls)")]
    if "detail" not in cols:
        con.execute("ALTER TABLE tool_calls ADD COLUMN detail TEXT")
    con.execute("DELETE FROM ingest_state")


def _migrate_to_3(con):
    """sessions.source_label + remote_state (multi-source ingest)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    if "source_label" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN source_label TEXT DEFAULT ''")
    con.execute("""CREATE TABLE IF NOT EXISTS remote_state (
                       host       TEXT PRIMARY KEY,
                       last_fetch REAL,
                       last_error TEXT)""")


def _migrate_to_4(con):
    """remote_state gains failure backoff (fail_count + next_attempt)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(remote_state)")]
    if "fail_count" not in cols:
        con.execute("ALTER TABLE remote_state ADD COLUMN fail_count INTEGER DEFAULT 0")
    if "next_attempt" not in cols:
        con.execute("ALTER TABLE remote_state ADD COLUMN next_attempt REAL DEFAULT 0")


def _migrate_to_5(con):
    """One-time re-parse so legacy transcripts are picked up (see changelog)."""
    con.execute("DELETE FROM ingest_state")


def _migrate_to_6(con):
    """sessions.title, run_cost, and an index on api_requests.session_id."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    if "title" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS run_cost (
                       session_id TEXT PRIMARY KEY,
                       cost_usd   REAL,
                       runs       INTEGER,
                       source     TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_req_session "
                "ON api_requests(session_id)")


def _migrate_to_7(con):
    """api_requests.provider / .model_raw, and normalise the ids already stored.

    Rewriting in place rather than re-parsing: api_requests rows from
    transcripts are insert-or-ignore, so a re-parse leaves an existing row's
    model untouched and Bedrock history would stay uncosted forever.
    """
    import pricing
    cols = [r[1] for r in con.execute("PRAGMA table_info(api_requests)")]
    if "model_raw" not in cols:
        con.execute("ALTER TABLE api_requests ADD COLUMN model_raw TEXT")
    if "provider" not in cols:
        con.execute("ALTER TABLE api_requests ADD COLUMN provider TEXT")
    seen = [r[0] for r in con.execute(
        "SELECT DISTINCT model FROM api_requests WHERE model IS NOT NULL")]
    for raw in seen:
        canon, provider = pricing.canonical_model(raw)
        if canon is None and provider is None:
            continue                      # "<synthetic>" and friends: leave be
        con.execute(
            """UPDATE api_requests SET model=?, model_raw=?, provider=?
               WHERE model=?""",
            (canon or raw, raw, provider, raw))


_V8_COLUMNS = {
    "api_requests": [
        ("effort", "TEXT"), ("speed", "TEXT"), ("thinking_tokens", "INTEGER"),
        ("stop_reason", "TEXT"), ("server_tool_requests", "INTEGER"),
        ("service_tier", "TEXT"), ("inference_geo", "TEXT"),
        ("context_tokens", "INTEGER"), ("cost_basis", "TEXT"),
        ("error", "TEXT")],
    "tool_calls": [
        ("input_bytes", "INTEGER"), ("result_bytes", "INTEGER"),
        ("is_error", "INTEGER"), ("duration_ms", "INTEGER"),
        ("error_type", "TEXT")],
    "sessions": [
        ("git_branch", "TEXT"), ("cli_version", "TEXT"),
        ("entrypoint", "TEXT"), ("permission_mode", "TEXT"),
        ("transcript_path", "TEXT"), ("first_ts", "TEXT"),
        ("last_ts", "TEXT")],
    "prompts": [("kind", "TEXT")],
}


def _migrate_to_8(con):
    """Per-request effort/speed/context, tool sizes, agents and session events.

    The re-parse is the point of the release as much as the columns are: every
    new field is only in the transcripts, and the upsert rule that lands with
    it (a JSONL row may replace a JSONL row with no more output tokens) means
    a re-parse can finally correct rows written from a half-streamed request.
    """
    for table, columns in _V8_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    con.executescript(SCHEMA)               # new tables + indexes
    # Fill context_tokens for history that will never be re-parsed (the
    # transcript is gone); rows that are re-parsed get the same answer.
    con.execute("""UPDATE api_requests SET context_tokens =
                     COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0)
                     + COALESCE(cache_create_tokens,0)
                   WHERE context_tokens IS NULL""")
    # Subagent work used to be labelled by its transcript's filename; the
    # agents table is keyed on the CLI's agentId, so drop the file prefix and
    # the two join without a rule to remember.
    for table in ("api_requests", "tool_calls", "edits"):
        con.execute(f"UPDATE {table} SET agent_name = substr(agent_name, 7) "
                    f"WHERE agent_name LIKE 'agent-%'")
    con.execute("DELETE FROM ingest_state")


MIGRATIONS = {2: _migrate_to_2, 3: _migrate_to_3, 4: _migrate_to_4,
              5: _migrate_to_5, 6: _migrate_to_6, 7: _migrate_to_7,
              8: _migrate_to_8}


def connect(path=DB_PATH, cross_thread=False):
    # cross_thread=True is safe only when the caller serializes all access
    # (the receiver guards every use with a lock).
    con = sqlite3.connect(path, timeout=30, check_same_thread=not cross_thread)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    fresh = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompts'"
    ).fetchone() is None
    con.executescript(SCHEMA)
    if fresh:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        return con
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        version = 1  # databases from before version tracking
    if version > SCHEMA_VERSION:
        con.close()  # a connection left open here blocks rmtree on Windows
        raise RuntimeError(
            f"metrics.db is schema v{version} but this code only knows "
            f"v{SCHEMA_VERSION} — update the project before running.")
    while version < SCHEMA_VERSION:
        version += 1
        MIGRATIONS[version](con)
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
        # Name the file: the tests and any --db run migrate copies, and a
        # message that always says "metrics.db" reads as if the real one had
        # just been rewritten.
        print(f"{os.path.basename(path)} migrated to schema v{version}")
    return con


PROMPT_COLS = ("prompt_id", "session_id", "project", "ts", "text", "source",
               "injected", "canonical_id", "kind")

PROMPT_SQL = """INSERT INTO prompts (prompt_id, session_id, project, ts, text, source, injected, canonical_id, kind)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO UPDATE SET
             session_id   = COALESCE(prompts.session_id, excluded.session_id),
             project      = COALESCE(prompts.project, excluded.project),
             ts           = COALESCE(prompts.ts, excluded.ts),
             text         = CASE WHEN prompts.text = '' THEN excluded.text ELSE prompts.text END,
             injected     = MAX(prompts.injected, excluded.injected),
             canonical_id = COALESCE(prompts.canonical_id, excluded.canonical_id),
             kind         = COALESCE(excluded.kind, prompts.kind)
        """


def _pad(row, width):
    """A caller's tuple widened to `width` with NULLs.

    Columns are only ever appended, so a tuple built against an older column
    list stays valid; the receiver and any out-of-tree caller keep working
    across a schema bump instead of raising a binding error.
    """
    if type(row) is tuple and len(row) == width:
        return row
    row = tuple(row)
    if len(row) == width:
        return row
    if len(row) > width:
        raise ValueError(f"row has {len(row)} values, expected at most {width}")
    return row + (None,) * (width - len(row))


def upsert_prompt(con, prompt_id, session_id=None, project=None, ts=None,
                  text="", source=None, injected=0, canonical_id=None,
                  kind=None):
    con.execute(
        PROMPT_SQL,
        (prompt_id, session_id, project, ts, text, source, injected,
         canonical_id, kind),
    )


def upsert_prompts(con, rows):
    """Batch of PROMPT_SQL parameter tuples, applied in order."""
    con.executemany(PROMPT_SQL, (_pad(r, len(PROMPT_COLS)) for r in rows))


# Column order used by every api_requests write. The two statements below are
# built once at import instead of being re-assembled (four string joins) on
# every row: the transcript ingester writes tens of thousands per run.
#
# New columns are appended, never inserted: a caller that builds a positional
# tuple against an older column list is padded rather than broken.
REQUEST_COLS = ("request_id", "prompt_id", "session_id", "ts", "model",
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_create_tokens", "cache_5m_tokens", "cache_1h_tokens",
                "cost_usd", "duration_ms", "query_source", "agent_name",
                "model_raw", "provider",
                # v8
                "effort", "speed", "thinking_tokens", "stop_reason",
                "server_tool_requests", "service_tier", "inference_geo",
                "context_tokens", "cost_basis", "error")
_REQ_PLACEHOLDERS = ",".join("?" * len(REQUEST_COLS))
_REQ_NAMES = ",".join(REQUEST_COLS)
_CTX_IDX = REQUEST_COLS.index("context_tokens")
_TOKEN_IDX = (REQUEST_COLS.index("input_tokens"),
              REQUEST_COLS.index("cache_read_tokens"),
              REQUEST_COLS.index("cache_create_tokens"))

# Columns an OTel event does not carry. It has one cache-write total and no
# TTL split, and it knows nothing of thinking tokens, stop reasons, server
# tool use, service tier or inference geo - all of which only the transcript
# records. Overwriting them with NULL when a live row lands on top of a
# transcript row would, for the cache split, silently bill the whole write at
# the 1h rate; so these fill rather than replace, and everything else still
# takes the OTel value (it carries the CLI's own cost).
OTEL_FILLS_ONLY = frozenset({
    "cache_5m_tokens", "cache_1h_tokens", "thinking_tokens", "stop_reason",
    "server_tool_requests", "service_tier", "inference_geo"})


def _otel_assignment(col):
    if col in OTEL_FILLS_ONLY:
        return f"{col}=COALESCE(excluded.{col},api_requests.{col})"
    return f"{col}=excluded.{col}"


REQUEST_SQL_OTEL = (
    f"INSERT INTO api_requests ({_REQ_NAMES}, source)\n"
    f"VALUES ({_REQ_PLACEHOLDERS}, 'otel')\n"
    "ON CONFLICT(request_id) DO UPDATE SET "
    + ",".join(_otel_assignment(c) for c in REQUEST_COLS[1:])
    + ", source='otel'")

# Transcript rows may correct each other but never an OTel row, and only
# upwards: one API request is written to the transcript once per content block
# and the last block carries the final usage, so the row with the most output
# tokens is the complete one. Without the guard, re-parsing a file whose first
# chunk was captured live would overwrite the full figures with a stub.
REQUEST_SQL_JSONL = (
    f"INSERT INTO api_requests ({_REQ_NAMES}, source)\n"
    f"VALUES ({_REQ_PLACEHOLDERS}, 'jsonl')\n"
    "ON CONFLICT(request_id) DO UPDATE SET "
    + ",".join(f"{c}=excluded.{c}" for c in REQUEST_COLS[1:])
    + "\nWHERE api_requests.source='jsonl'"
      " AND excluded.output_tokens >= api_requests.output_tokens")


def _with_context(vals):
    """context_tokens, computed at insert when the caller did not supply it."""
    if vals[_CTX_IDX] is None:
        vals[_CTX_IDX] = sum((vals[i] or 0) for i in _TOKEN_IDX)
    return vals


def upsert_request(con, row, source):
    """One api_requests row from a dict of column names. OTel wins conflicts.

    Unknown-to-the-caller columns default to NULL, so a producer written
    against an older schema keeps working.
    """
    vals = _with_context([row.get(c) for c in REQUEST_COLS])
    con.execute(REQUEST_SQL_OTEL if source == "otel" else REQUEST_SQL_JSONL,
                vals)


def insert_requests_jsonl(con, rows):
    """Batch of REQUEST_COLS-ordered tuples (or dicts) from transcripts.

    The ingester already hands over full-width tuples with context_tokens
    filled, and it hands over tens of thousands per run, so that case passes
    straight through; only a short tuple or a dict pays for normalisation.
    """
    width = len(REQUEST_COLS)

    def prepared():
        for row in rows:
            if type(row) is tuple and len(row) == width \
                    and row[_CTX_IDX] is not None:
                yield row
            elif isinstance(row, dict):
                yield _with_context([row.get(c) for c in REQUEST_COLS])
            else:
                yield _with_context(list(_pad(row, width)))

    con.executemany(REQUEST_SQL_JSONL, prepared())


TOOL_CALL_COLS = ("tool_use_id", "prompt_id", "session_id", "ts", "tool_name",
                  "agent_name", "source", "detail",
                  # v8
                  "input_bytes", "result_bytes", "is_error", "duration_ms",
                  "error_type")

# The two sources know different halves of a tool call: the transcript has the
# input and result bytes and the specific name, OTel has the duration and the
# error type. Neither is ever authoritative over the other, so each write fills
# NULLs and leaves everything already known alone.
TOOL_CALL_SQL = """INSERT INTO tool_calls
           (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source, detail,
            input_bytes, result_bytes, is_error, duration_ms, error_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(tool_use_id) DO UPDATE SET
             tool_name = CASE
               WHEN tool_calls.tool_name = 'mcp_tool'
                    AND excluded.tool_name LIKE 'mcp__%'
               THEN excluded.tool_name ELSE tool_calls.tool_name END,
             detail       = COALESCE(tool_calls.detail, excluded.detail),
             input_bytes  = COALESCE(tool_calls.input_bytes, excluded.input_bytes),
             result_bytes = COALESCE(tool_calls.result_bytes, excluded.result_bytes),
             is_error     = COALESCE(tool_calls.is_error, excluded.is_error),
             duration_ms  = COALESCE(tool_calls.duration_ms, excluded.duration_ms),
             error_type   = COALESCE(tool_calls.error_type, excluded.error_type)"""


def insert_tool_call(con, tool_use_id, prompt_id, session_id, ts, tool_name,
                     agent_name, source, detail=None, input_bytes=None,
                     result_bytes=None, is_error=None, duration_ms=None,
                     error_type=None):
    # OTel tool_result events name MCP calls generically ('mcp_tool') and know
    # nothing of skill names; the transcript carries the specifics. On conflict,
    # let a specific name upgrade the generic one and fill missing fields.
    con.execute(
        TOOL_CALL_SQL,
        (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source,
         detail, input_bytes, result_bytes, is_error, duration_ms, error_type),
    )


def insert_tool_calls(con, rows):
    """Batch of TOOL_CALL_SQL parameter tuples, applied in order."""
    width = len(TOOL_CALL_COLS)
    con.executemany(TOOL_CALL_SQL, (_pad(r, width) for r in rows))


AGENT_SQL = """INSERT INTO agents
           (agent_id, session_id, prompt_id, ts, subagent_type,
            requested_model, resolved_model, description, tool_use_id, source)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id) DO UPDATE SET
             session_id      = COALESCE(agents.session_id, excluded.session_id),
             prompt_id       = COALESCE(agents.prompt_id, excluded.prompt_id),
             ts              = COALESCE(agents.ts, excluded.ts),
             subagent_type   = COALESCE(agents.subagent_type, excluded.subagent_type),
             requested_model = COALESCE(agents.requested_model, excluded.requested_model),
             resolved_model  = COALESCE(agents.resolved_model, excluded.resolved_model),
             description     = COALESCE(agents.description, excluded.description),
             tool_use_id     = COALESCE(agents.tool_use_id, excluded.tool_use_id)"""

AGENT_COLS = ("agent_id", "session_id", "prompt_id", "ts", "subagent_type",
              "requested_model", "resolved_model", "description",
              "tool_use_id", "source")


def upsert_agent(con, agent_id, session_id=None, prompt_id=None, ts=None,
                 subagent_type=None, requested_model=None, resolved_model=None,
                 description=None, tool_use_id=None, source="jsonl"):
    """One subagent launch. The launch and its result are two transcript
    entries and the subagent's own file is a third, so every write fills gaps
    rather than replacing what an earlier one already established."""
    con.execute(AGENT_SQL, (agent_id, session_id, prompt_id, ts, subagent_type,
                            requested_model, resolved_model, description,
                            tool_use_id, source))


def upsert_agents(con, rows):
    """Batch of AGENT_SQL parameter tuples."""
    width = len(AGENT_COLS)
    con.executemany(AGENT_SQL, (_pad(r, width) for r in rows))


# session_events has no natural key, so re-ingesting a transcript would
# otherwise multiply its events; idx_events_uniq makes the whole tuple the
# identity and this insert is a no-op on the second pass.
EVENT_SQL = """INSERT OR IGNORE INTO session_events
           (session_id, prompt_id, ts, kind, detail, value)
           VALUES (?,?,?,?,?,?)"""

EVENT_COLS = ("session_id", "prompt_id", "ts", "kind", "detail", "value")


def insert_event(con, session_id, ts, kind, prompt_id=None, detail=None,
                 value=None):
    con.execute(EVENT_SQL, (session_id, prompt_id, ts, kind, detail, value))


def insert_events(con, rows):
    """Batch of EVENT_SQL parameter tuples (session_id, prompt_id, ts, kind,
    detail, value)."""
    width = len(EVENT_COLS)
    con.executemany(EVENT_SQL, (_pad(r, width) for r in rows))


EDIT_SQL = """INSERT OR IGNORE INTO edits
           (tool_use_id, prompt_id, session_id, ts, file_path, kind,
            lines_added, lines_removed, chars_added, agent_name, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)"""


def insert_edit(con, tool_use_id, prompt_id, session_id, ts, file_path, kind,
                lines_added, lines_removed, chars_added, agent_name, source):
    con.execute(
        EDIT_SQL,
        (tool_use_id, prompt_id, session_id, ts, file_path, kind,
         lines_added, lines_removed, chars_added, agent_name, source),
    )


def insert_edits(con, rows):
    """Batch of EDIT_SQL parameter tuples."""
    con.executemany(EDIT_SQL, rows)


def set_session_title(con, session_id, title):
    """Name a session (Claude Desktop shows one; the CLI does not)."""
    con.execute(
        """INSERT INTO sessions (session_id, title) VALUES (?,?)
           ON CONFLICT(session_id) DO UPDATE SET title = excluded.title""",
        (session_id, title))


def set_run_cost(con, session_id, cost_usd, runs, source):
    """Record a CLI-reported cost covering `runs` completed runs."""
    con.execute(
        """INSERT INTO run_cost (session_id, cost_usd, runs, source)
           VALUES (?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             cost_usd = excluded.cost_usd, runs = excluded.runs,
             source = excluded.source""",
        (session_id, cost_usd, runs, source))


def upsert_session(con, session_id, project=None, cwd=None, source_label=None,
                   git_branch=None, cli_version=None, entrypoint=None,
                   permission_mode=None, transcript_path=None, first_ts=None,
                   last_ts=None):
    # source_label is authoritative on every write: it names the Claude
    # directory the transcript was just read from, so a session whose origin
    # is renamed (a host alias changed in ~/.ssh/config, a folder relabeled)
    # re-labels instead of keeping a stale prefix. NULL = caller doesn't know.
    #
    # The v8 descriptors follow "last seen wins" where the answer can change
    # during a session (branch, CLI version, permission mode) and MIN/MAX for
    # the timestamps, so a transcript ingested in two passes still reports the
    # full span.
    con.execute(
        """INSERT INTO sessions (session_id, project, cwd, source_label,
             git_branch, cli_version, entrypoint, permission_mode,
             transcript_path, first_ts, last_ts)
           VALUES (?,?,?,COALESCE(?,''),?,?,?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             project      = COALESCE(sessions.project, excluded.project),
             cwd          = COALESCE(sessions.cwd, excluded.cwd),
             source_label = COALESCE(?, sessions.source_label),
             git_branch      = COALESCE(excluded.git_branch, sessions.git_branch),
             cli_version     = COALESCE(excluded.cli_version, sessions.cli_version),
             entrypoint      = COALESCE(excluded.entrypoint, sessions.entrypoint),
             permission_mode = COALESCE(excluded.permission_mode, sessions.permission_mode),
             transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
             first_ts = MIN(COALESCE(excluded.first_ts, sessions.first_ts),
                            COALESCE(sessions.first_ts, excluded.first_ts)),
             last_ts  = MAX(COALESCE(excluded.last_ts, sessions.last_ts),
                            COALESCE(sessions.last_ts, excluded.last_ts))""",
        (session_id, project, cwd, source_label, git_branch, cli_version,
         entrypoint, permission_mode, transcript_path, first_ts, last_ts,
         source_label),
    )


REMOTE_STATE_DEFAULT = {"last_fetch": 0.0, "last_error": None,
                        "fail_count": 0, "next_attempt": 0.0}


def get_remote_state(con, host):
    """Fetch bookkeeping for one host; defaults for a host never contacted."""
    row = con.execute(
        """SELECT last_fetch, last_error, fail_count, next_attempt
           FROM remote_state WHERE host=?""", (host,)).fetchone()
    if not row:
        return dict(REMOTE_STATE_DEFAULT)
    return {"last_fetch": row[0] or 0.0, "last_error": row[1],
            "fail_count": row[2] or 0, "next_attempt": row[3] or 0.0}


def all_remote_state(con):
    """Every host's bookkeeping, for `--remote-status`."""
    return [{"host": r[0], "last_fetch": r[1] or 0.0, "last_error": r[2],
             "fail_count": r[3] or 0, "next_attempt": r[4] or 0.0}
            for r in con.execute(
                """SELECT host, last_fetch, last_error, fail_count, next_attempt
                   FROM remote_state ORDER BY host""")]


def record_remote_success(con, host, when):
    """Clear the failure state and advance the incremental watermark."""
    con.execute(
        """INSERT INTO remote_state
             (host, last_fetch, last_error, fail_count, next_attempt)
           VALUES (?,?,NULL,0,0)
           ON CONFLICT(host) DO UPDATE SET
             last_fetch = excluded.last_fetch,
             last_error = NULL, fail_count = 0, next_attempt = 0""",
        (host, when))


def record_remote_failure(con, host, error, next_attempt):
    """Count the failure and park the host until `next_attempt`.

    last_fetch is deliberately left alone: whatever this attempt would have
    brought is simply requested again by the run that eventually succeeds.
    """
    con.execute(
        """INSERT INTO remote_state
             (host, last_fetch, last_error, fail_count, next_attempt)
           VALUES (?,NULL,?,1,?)
           ON CONFLICT(host) DO UPDATE SET
             last_error   = excluded.last_error,
             fail_count   = remote_state.fail_count + 1,
             next_attempt = excluded.next_attempt""",
        (host, error, next_attempt))
