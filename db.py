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
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 2

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
    source            TEXT
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
    session_id TEXT PRIMARY KEY,
    project    TEXT,
    cwd        TEXT
);
CREATE TABLE IF NOT EXISTS ingest_state (
    path  TEXT PRIMARY KEY,
    size  INTEGER,
    mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_req_prompt ON api_requests(prompt_id);
CREATE INDEX IF NOT EXISTS idx_tool_prompt ON tool_calls(prompt_id);
CREATE INDEX IF NOT EXISTS idx_prompt_ts ON prompts(ts);
"""


def _migrate_to_2(con):
    """tool_calls.detail + one-time re-parse to backfill skill/MCP names."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(tool_calls)")]
    if "detail" not in cols:
        con.execute("ALTER TABLE tool_calls ADD COLUMN detail TEXT")
    con.execute("DELETE FROM ingest_state")


MIGRATIONS = {2: _migrate_to_2}


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


def upsert_prompt(con, prompt_id, session_id=None, project=None, ts=None,
                  text="", source=None, injected=0, canonical_id=None):
    con.execute(
        """INSERT INTO prompts (prompt_id, session_id, project, ts, text, source, injected, canonical_id)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO UPDATE SET
             session_id   = COALESCE(prompts.session_id, excluded.session_id),
             project      = COALESCE(prompts.project, excluded.project),
             ts           = COALESCE(prompts.ts, excluded.ts),
             text         = CASE WHEN prompts.text = '' THEN excluded.text ELSE prompts.text END,
             injected     = MAX(prompts.injected, excluded.injected),
             canonical_id = COALESCE(prompts.canonical_id, excluded.canonical_id)
        """,
        (prompt_id, session_id, project, ts, text, source, injected, canonical_id),
    )


def upsert_request(con, row, source):
    """row: dict with api_requests columns (minus source). OTel wins conflicts."""
    cols = ("request_id", "prompt_id", "session_id", "ts", "model",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_create_tokens", "cache_5m_tokens", "cache_1h_tokens",
            "cost_usd", "duration_ms", "query_source", "agent_name")
    vals = [row.get(c) for c in cols]
    if source == "otel":
        con.execute(
            f"""INSERT INTO api_requests ({','.join(cols)}, source)
                VALUES ({','.join('?' * len(cols))}, 'otel')
                ON CONFLICT(request_id) DO UPDATE SET
                  {','.join(f'{c}=excluded.{c}' for c in cols[1:])}, source='otel'
            """, vals)
    else:
        con.execute(
            f"""INSERT OR IGNORE INTO api_requests ({','.join(cols)}, source)
                VALUES ({','.join('?' * len(cols))}, 'jsonl')""", vals)


def insert_tool_call(con, tool_use_id, prompt_id, session_id, ts, tool_name,
                     agent_name, source, detail=None):
    # OTel tool_result events name MCP calls generically ('mcp_tool') and know
    # nothing of skill names; the transcript carries the specifics. On conflict,
    # let a specific name upgrade the generic one and fill a missing detail.
    con.execute(
        """INSERT INTO tool_calls
           (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source, detail)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(tool_use_id) DO UPDATE SET
             tool_name = CASE
               WHEN tool_calls.tool_name = 'mcp_tool'
                    AND excluded.tool_name LIKE 'mcp__%'
               THEN excluded.tool_name ELSE tool_calls.tool_name END,
             detail = COALESCE(tool_calls.detail, excluded.detail)""",
        (tool_use_id, prompt_id, session_id, ts, tool_name, agent_name, source,
         detail),
    )


def insert_edit(con, tool_use_id, prompt_id, session_id, ts, file_path, kind,
                lines_added, lines_removed, chars_added, agent_name, source):
    con.execute(
        """INSERT OR IGNORE INTO edits
           (tool_use_id, prompt_id, session_id, ts, file_path, kind,
            lines_added, lines_removed, chars_added, agent_name, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (tool_use_id, prompt_id, session_id, ts, file_path, kind,
         lines_added, lines_removed, chars_added, agent_name, source),
    )


def upsert_session(con, session_id, project=None, cwd=None):
    con.execute(
        """INSERT INTO sessions (session_id, project, cwd) VALUES (?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             project = COALESCE(sessions.project, excluded.project),
             cwd     = COALESCE(sessions.cwd, excluded.cwd)""",
        (session_id, project, cwd),
    )
