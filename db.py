"""SQLite storage shared by the OTel receiver, the JSONL backfill, and the
dashboard generator.

Dedupe strategy:
- api_requests keyed by the Anthropic request_id. Both sources see the same id,
  so OTel + JSONL never double count. OTel rows win on conflict (they carry an
  authoritative cost_usd); JSONL rows never overwrite OTel rows.
- tool_calls keyed by tool_use_id (toolu_...), present in both sources.
- prompts keyed by prompt_id; conflicting inserts merge, filling missing fields.
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")

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
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    prompt_id     TEXT PRIMARY KEY,
    session_id    TEXT,
    project       TEXT,
    ts            TEXT,
    text          TEXT DEFAULT '',
    source        TEXT,
    injected      INTEGER DEFAULT 0,
    canonical_id  TEXT
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
    provider          TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    prompt_id   TEXT,
    session_id  TEXT,
    ts          TEXT,
    tool_name   TEXT,
    agent_name  TEXT,
    source      TEXT,
    detail      TEXT
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
    title        TEXT
);
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


MIGRATIONS = {2: _migrate_to_2, 3: _migrate_to_3, 4: _migrate_to_4,
              5: _migrate_to_5, 6: _migrate_to_6, 7: _migrate_to_7}


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
        raise RuntimeError(
            f"metrics.db is schema v{version} but this code only knows "
            f"v{SCHEMA_VERSION} — update the project before running.")
    while version < SCHEMA_VERSION:
        version += 1
        MIGRATIONS[version](con)
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
        print(f"metrics.db migrated to schema v{version}")
    return con


PROMPT_SQL = """INSERT INTO prompts (prompt_id, session_id, project, ts, text, source, injected, canonical_id)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO UPDATE SET
             session_id   = COALESCE(prompts.session_id, excluded.session_id),
             project      = COALESCE(prompts.project, excluded.project),
             ts           = COALESCE(prompts.ts, excluded.ts),
             text         = CASE WHEN prompts.text = '' THEN excluded.text ELSE prompts.text END,
             injected     = MAX(prompts.injected, excluded.injected),
             canonical_id = COALESCE(prompts.canonical_id, excluded.canonical_id)
        """


def upsert_prompt(con, prompt_id, session_id=None, project=None, ts=None,
                  text="", source=None, injected=0, canonical_id=None):
    con.execute(
        PROMPT_SQL,
        (prompt_id, session_id, project, ts, text, source, injected, canonical_id),
    )


def upsert_prompts(con, rows):
    """Batch of PROMPT_SQL parameter tuples, applied in order."""
    con.executemany(PROMPT_SQL, rows)


# Column order used by every api_requests write. The two statements below are
# built once at import instead of being re-assembled (four string joins) on
# every row: the transcript ingester writes tens of thousands per run.
REQUEST_COLS = ("request_id", "prompt_id", "session_id", "ts", "model",
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_create_tokens", "cache_5m_tokens", "cache_1h_tokens",
                "cost_usd", "duration_ms", "query_source", "agent_name",
                "model_raw", "provider")
_REQ_PLACEHOLDERS = ",".join("?" * len(REQUEST_COLS))
_REQ_NAMES = ",".join(REQUEST_COLS)

REQUEST_SQL_OTEL = (
    f"INSERT INTO api_requests ({_REQ_NAMES}, source)\n"
    f"VALUES ({_REQ_PLACEHOLDERS}, 'otel')\n"
    "ON CONFLICT(request_id) DO UPDATE SET "
    + ",".join(f"{c}=excluded.{c}" for c in REQUEST_COLS[1:])
    + ", source='otel'")

REQUEST_SQL_JSONL = (
    f"INSERT OR IGNORE INTO api_requests ({_REQ_NAMES}, source)\n"
    f"VALUES ({_REQ_PLACEHOLDERS}, 'jsonl')")


def upsert_request(con, row, source):
    """row: dict with api_requests columns (minus source). OTel wins conflicts."""
    vals = [row.get(c) for c in REQUEST_COLS]
    con.execute(REQUEST_SQL_OTEL if source == "otel" else REQUEST_SQL_JSONL,
                vals)


def insert_requests_jsonl(con, rows):
    """Batch of REQUEST_COLS-ordered tuples from transcripts (OTel rows win)."""
    con.executemany(REQUEST_SQL_JSONL, rows)


TOOL_CALL_SQL = """INSERT INTO tool_calls
           (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source, detail)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(tool_use_id) DO UPDATE SET
             tool_name = CASE
               WHEN tool_calls.tool_name = 'mcp_tool'
                    AND excluded.tool_name LIKE 'mcp__%'
               THEN excluded.tool_name ELSE tool_calls.tool_name END,
             detail = COALESCE(tool_calls.detail, excluded.detail)"""


def insert_tool_call(con, tool_use_id, prompt_id, session_id, ts, tool_name,
                     agent_name, source, detail=None):
    # OTel tool_result events name MCP calls generically ('mcp_tool') and know
    # nothing of skill names; the transcript carries the specifics. On conflict,
    # let a specific name upgrade the generic one and fill a missing detail.
    con.execute(
        TOOL_CALL_SQL,
        (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source,
         detail),
    )


def insert_tool_calls(con, rows):
    """Batch of TOOL_CALL_SQL parameter tuples, applied in order."""
    con.executemany(TOOL_CALL_SQL, rows)


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


def upsert_session(con, session_id, project=None, cwd=None, source_label=None):
    # source_label is authoritative on every write: it names the Claude
    # directory the transcript was just read from, so a session whose origin
    # is renamed (a host alias changed in ~/.ssh/config, a folder relabeled)
    # re-labels instead of keeping a stale prefix. NULL = caller doesn't know.
    con.execute(
        """INSERT INTO sessions (session_id, project, cwd, source_label)
           VALUES (?,?,?,COALESCE(?,''))
           ON CONFLICT(session_id) DO UPDATE SET
             project      = COALESCE(sessions.project, excluded.project),
             cwd          = COALESCE(sessions.cwd, excluded.cwd),
             source_label = COALESCE(?, sessions.source_label)""",
        (session_id, project, cwd, source_label, source_label),
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
