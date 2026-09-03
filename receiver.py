"""OTLP HTTP receiver for Claude Code telemetry -> metrics.db.

Listens on 127.0.0.1:4318 (OTLP/HTTP, JSON encoding). Claude Code is pointed
here via OTEL_EXPORTER_OTLP_ENDPOINT with OTEL_EXPORTER_OTLP_PROTOCOL=http/json.

Also acts as the system's scheduler:
- On startup and every RECONCILE_EVERY seconds: runs the JSONL reconciler
  (heals any gaps from receiver downtime; supplies session->project mapping).
- Every REBUILD_CHECK seconds: regenerates dashboard.html if new data arrived.

Single-instance safety: binding port 4318 fails if a receiver already runs.

Attribute names follow https://code.claude.com/docs/en/monitoring-usage
(verified 2026-09-03); anything not in that reference is not read here.
"""
import argparse
import gzip
import inspect
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import build_dashboard
import db
import jsonl_ingest
import pricing
import sources

HOST, PORT = "127.0.0.1", 4318
REBUILD_CHECK = 60          # seconds between dirty-checks for dashboard rebuild
RECONCILE_EVERY = 3600  # seconds between JSONL reconciliation passes
                        # (hourly: file-edit stats only arrive via JSONL)

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "receiver.log")

# The receiver owns dashboard.html: it rebuilds it about once a minute using
# whatever code it started with. Edit the builder or the template while it is
# running and your rebuild is silently overwritten by the old one, which is a
# genuinely baffling way to lose work. So the files are fingerprinted at
# startup, and once they change this process stops writing dashboard.html.
#
# It does not try to restart itself. Exiting and re-execing were both tried:
# Windows Task Scheduler did not restart on a non-zero exit, and a re-exec
# left nothing running at all, which is far worse than a stale page. So the
# safe half is kept - a rebuild you just ran by hand is never overwritten by
# older code - ingestion carries on, and the log says to restart.
WATCHED_FILES = ("receiver.py", "build_dashboard.py", "template.html",
                 "jsonl_ingest.py", "sources.py", "db.py", "pricing.py",
                 "report_index.py")

log = logging.getLogger("receiver")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)

# Prompts injected by the harness (not typed by the user) get their own
# prompt.id in telemetry; fold them into the preceding human prompt. Shared
# with the transcript ingester, which needs the same list to recognise
# injected turns in transcripts too old to carry an origin marker.
INJECTED_PREFIXES = jsonl_ingest.INJECTED_PREFIXES

def code_fingerprint():
    """mtimes of the files whose changes this process cannot pick up."""
    out = {}
    for name in WATCHED_FILES:
        try:
            out[name] = os.path.getmtime(os.path.join(BASE, name))
        except OSError:
            out[name] = None
    return out


_started_with = code_fingerprint()


def code_is_stale():
    """True once a watched file differs from what this process loaded.

    Logged once, then remembered: from that point the dashboard is left alone
    so a hand-run rebuild survives, while ingestion keeps going.
    """
    global _stale
    if _stale:
        return True
    changed = [n for n, m in code_fingerprint().items()
               if m != _started_with.get(n)]
    if not changed:
        return False
    _stale = True
    log.error("code changed on disk (%s); no longer rebuilding dashboard.html "
              "- restart this receiver to pick up the new version",
              ", ".join(sorted(changed)))
    return True


_stale = False            # set once watched files change under us
_db_lock = threading.Lock()
_con = None               # opened in main(), so importing this module is free
                          # of side effects (the tests rely on that) and --db
                          # can still choose the file.


def resolve_db(explicit=None):
    """Which metrics.db to use. Prefers db.resolve_path once it exists."""
    fn = getattr(db, "resolve_path", None)
    if fn is not None:
        return fn(explicit)
    return explicit or os.environ.get("CLAUDE_LENS_DB") or db.DB_PATH


# ---------------------------------------------------------------------------
# Schema tolerance
#
# The receiver writes columns that a database migrated by an older version of
# this project does not have yet (effort, speed, cost_basis, ...). Unknown keys
# in the row dict are ignored by db.upsert_request, and the follow-up UPDATEs
# below are filtered to the columns that actually exist, so a receiver never
# fails on an out-of-date file - it simply stores less.
# ---------------------------------------------------------------------------
# Keyed on the connection object, not id(con): a closed connection's id is
# reused by the next one, and a cache hit on a stale id would report another
# database's columns. sqlite3.Connection cannot be weak-referenced and carries
# no attribute dict, so the key holds a strong reference - which is also what
# stops the id from being recycled. The receiver holds two connections for its
# lifetime, so there is nothing to reclaim; reset_column_cache() is there for
# the tests and for after a migration.
_col_cache = {}
_table_cache = {}


def columns(con, table):
    """Column names of `table`, cached per connection."""
    key = (con, table)
    cols = _col_cache.get(key)
    if cols is None:
        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        _col_cache[key] = cols
    return cols


def tables(con):
    """Table names in this database, cached per connection."""
    names = _table_cache.get(con)
    if names is None:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _table_cache[con] = names
    return names


def reset_column_cache():
    """Forget cached PRAGMA results (after a migration, or between tests)."""
    _col_cache.clear()
    _table_cache.clear()


def fill_nulls(con, table, key_col, key, values):
    """Fill only the NULL columns of one row, and only ones that exist.

    Used for the extra facts OTel carries about a tool call. A transcript may
    have written the same row with better information; COALESCE keeps it.
    """
    cols = [c for c, v in values.items()
            if v is not None and c in columns(con, table)]
    if not cols:
        return
    sets = ",".join("%s=COALESCE(%s,?)" % (c, c) for c in cols)
    con.execute("UPDATE %s SET %s WHERE %s=?" % (table, sets, key_col),
                [values[c] for c in cols] + [key])


# ---------------------------------------------------------------------------
# Contracted rates
#
# An organization on contracted rates sets `modelPricing` in managed settings,
# and Claude Code then reports *its* rates in cost_usd rather than list price
# (https://code.claude.com/docs/en/settings-reference#modelpricing). The
# dashboard has to say which it is showing, so every OTel row is stamped with
# the basis in force when it arrived.
#
# `modelPricing` is a managed-scope key: Claude Code ignores it in user,
# project and local settings. The user file is still checked, because someone
# testing the setting locally would otherwise see "list" with no explanation -
# but managed sources are what actually change the numbers.
# ---------------------------------------------------------------------------
MANAGED_DIRS = ("/Library/Application Support/ClaudeCode",   # macOS
                "/etc/claude-code",                          # Linux and WSL
                r"C:\Program Files\ClaudeCode")              # Windows
SETTINGS_TTL = 300          # seconds between re-reads of the settings files


def settings_paths():
    """Every settings file that could carry `modelPricing`, in read order."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    paths = [os.path.join(home, "settings.json")]
    for d in MANAGED_DIRS:
        paths.append(os.path.join(d, "managed-settings.json"))
        # Drop-ins let several teams own parts of one policy.
        try:
            names = sorted(os.listdir(os.path.join(d, "managed-settings.d")))
        except OSError:
            continue
        paths += [os.path.join(d, "managed-settings.d", n)
                  for n in names if n.endswith(".json")]
    return paths


_basis = {"at": 0.0, "value": "list"}


def cost_basis(now=None):
    """"contracted" when a settings file defines modelPricing, else "list"."""
    now = time.time() if now is None else now
    if _basis["at"] and now - _basis["at"] < SETTINGS_TTL:
        return _basis["value"]
    value = "list"
    for path in settings_paths():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("modelPricing"):
            value = "contracted"
            break
    _basis["at"], _basis["value"] = now, value
    return value


def attr_dict(attributes):
    """OTLP JSON attribute list -> plain dict. intValue arrives as a string."""
    out = {}
    for a in attributes or []:
        v = a.get("value", {})
        if "stringValue" in v:
            out[a["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[a["key"]] = int(v["intValue"])
        elif "doubleValue" in v:
            out[a["key"]] = v["doubleValue"]
        elif "boolValue" in v:
            out[a["key"]] = v["boolValue"]
    return out


def canonical_for_session(con, session_id, ts):
    """Most recent non-injected prompt in this session at/before ts."""
    row = con.execute(
        """SELECT prompt_id FROM prompts
           WHERE session_id=? AND injected=0 AND (ts IS NULL OR ts<=?)
           ORDER BY ts DESC LIMIT 1""",
        (session_id, ts),
    ).fetchone()
    return row[0] if row else None


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request_key(attrs):
    """The id an api_request/api_error row is keyed on.

    `request_id` is the Anthropic request id and is only present when the API
    returned one - a timeout or a connection failure has none at all. Without
    a fallback those rows would all be written with a NULL primary key, which
    SQLite happily accepts as many times as it is asked, so a quiet network
    could fill the table with unjoinable rows. `client_request_id` (Claude Code
    v2.1.214+) identifies the same attempt from the client side and is used
    when the server id is missing.
    """
    return attrs.get("request_id") or attrs.get("client_request_id")


# OTel says "normal", transcripts say "standard", and both mean "not fast".
# Stored in the transcript's vocabulary so a filter or a GROUP BY sees one
# value rather than two names for the same thing.
SPEED_ALIASES = {"normal": "standard"}


def request_speed(attrs):
    speed = attrs.get("speed")
    return SPEED_ALIASES.get(speed, speed)


def request_cost(attrs):
    """cost_usd, falling back to the integer micros field."""
    cost = attrs.get("cost_usd")
    if cost is not None:
        return cost
    micros = attrs.get("cost_usd_micros")
    return None if micros is None else as_int(micros) / 1_000_000.0


def request_row(attrs, rid, prompt_id, session_id, ts):
    """An api_requests row dict from a claude_code.api_request event.

    Keys the current schema does not have (effort, speed, context_tokens,
    cost_basis, error) are ignored by db.upsert_request until they exist.
    """
    raw_model = attrs.get("model", "?")
    canon, provider = pricing.canonical_model(raw_model)
    inp = as_int(attrs.get("input_tokens"))
    cread = as_int(attrs.get("cache_read_tokens"))
    ccreate = as_int(attrs.get("cache_creation_tokens"))
    return {
        "request_id": rid,
        "prompt_id": prompt_id,
        "session_id": session_id,
        "ts": ts,
        "model": canon or raw_model,
        "model_raw": raw_model,
        "provider": provider,
        "input_tokens": inp,
        "output_tokens": as_int(attrs.get("output_tokens")),
        "cache_read_tokens": cread,
        "cache_create_tokens": ccreate,
        # OTel does not break cache writes down by TTL; the transcript does.
        # Left NULL rather than zeroed: the OTel upsert replaces every column,
        # so writing 0 here erased a transcript's real 5m/1h split and billed
        # the whole cache write at the 1h multiplier. db.REQUEST_SQL_OTEL
        # COALESCEs these two, so NULL means "keep whatever is known".
        "cache_5m_tokens": None,
        "cache_1h_tokens": None,
        "cost_usd": request_cost(attrs),
        "duration_ms": attrs.get("duration_ms"),
        "query_source": attrs.get("query_source"),
        "agent_name": attrs.get("agent.name"),
        "effort": attrs.get("effort"),
        "speed": request_speed(attrs),
        "context_tokens": inp + cread + ccreate,
        "cost_basis": cost_basis(),
        "error": None,
    }


def error_row(attrs, rid, prompt_id, session_id, ts):
    """An api_requests row for a claude_code.api_error event: no tokens, no cost."""
    raw_model = attrs.get("model", "?")
    canon, provider = pricing.canonical_model(raw_model)
    status = attrs.get("status_code")
    message = attrs.get("error") or ""
    text = ("%s: %s" % (status, message)).strip(": ") if status else message
    return {
        "request_id": rid,
        "prompt_id": prompt_id,
        "session_id": session_id,
        "ts": ts,
        "model": canon or raw_model,
        "model_raw": raw_model,
        "provider": provider,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cache_5m_tokens": 0, "cache_1h_tokens": 0,
        # A failed request bills nothing; leaving cost NULL would invite the
        # dashboard to estimate one from the (zero) tokens for no benefit.
        "cost_usd": 0.0,
        "duration_ms": attrs.get("duration_ms"),
        "query_source": attrs.get("query_source"),
        "agent_name": attrs.get("agent.name"),
        "effort": attrs.get("effort"),
        "speed": request_speed(attrs),
        "context_tokens": 0,
        "cost_basis": None,
        "error": text or "error",
    }


def record_api_error(con, row):
    """Store a failed request without ever clobbering a successful one.

    A retried attempt can carry the id of an attempt that later succeeded, and
    the OTel upsert overwrites every column - so an error arriving second would
    zero out real token counts. An existing row therefore only gains the error
    text (when the schema has somewhere to put it).
    """
    rid = row["request_id"]
    seen = con.execute("SELECT 1 FROM api_requests WHERE request_id=?",
                       (rid,)).fetchone()
    if seen:
        fill_nulls(con, "api_requests", "request_id", rid,
                   {"error": row["error"]})
        return False
    db.upsert_request(con, row, "otel")
    return True


# Slash commands that mark a whole class of prompt. `command_name` is emitted
# verbatim for built-in and bundled commands; custom, plugin and MCP commands
# collapse to "custom"/"mcp" unless OTEL_LOG_TOOL_DETAILS=1.
COMMAND_KINDS = {"loop": "loop", "schedule": "scheduled",
                 "scheduled": "scheduled", "team": "team"}


def prompt_kind(injected, command_name, command_source=None):
    """prompts.kind for a live prompt event."""
    if command_name:
        return COMMAND_KINDS.get(str(command_name).lower(), "command")
    if command_source:
        return "command"
    return "other" if injected else "human"


def handle_record(con, rec):
    body = (rec.get("body") or {}).get("stringValue", "")
    attrs = attr_dict(rec.get("attributes"))
    ts = attrs.get("event.timestamp")
    session_id = attrs.get("session.id")
    prompt_id = attrs.get("prompt.id")

    if body == "claude_code.user_prompt":
        if not prompt_id:
            return
        text = attrs.get("prompt", "")
        injected = 1 if text.lstrip().startswith(INJECTED_PREFIXES) else 0
        canonical = canonical_for_session(con, session_id, ts) if injected else None
        db.upsert_prompt(con, prompt_id, session_id=session_id, ts=ts,
                         text=text, source="otel", injected=injected,
                         canonical_id=canonical)
        # kind is written separately: db.upsert_prompt may not take it yet, and
        # a transcript-derived kind is never overwritten.
        fill_nulls(con, "prompts", "prompt_id", prompt_id,
                   {"kind": prompt_kind(injected, attrs.get("command_name"),
                                        attrs.get("command_source"))})
    elif body == "claude_code.api_request":
        rid = request_key(attrs)
        if not rid:
            return
        db.upsert_request(con, request_row(attrs, rid, prompt_id, session_id,
                                           ts), "otel")
    elif body == "claude_code.api_error":
        rid = request_key(attrs)
        if not rid:
            return          # nothing to key on; the count is not worth a
                            # row that can never be joined or deduplicated
        record_api_error(con, error_row(attrs, rid, prompt_id, session_id, ts))
    elif body == "claude_code.tool_result":
        tuid = attrs.get("tool_use_id")
        if not tuid:
            return
        db.insert_tool_call(con, tuid, prompt_id, session_id, ts,
                            attrs.get("tool_name", "?"),
                            attrs.get("agent.name"), "otel")
        success = attrs.get("success")
        is_error = None
        if success is not None:
            # Documented as the string "true"/"false"; tolerate a real bool.
            is_error = 0 if str(success).lower() == "true" else 1
        fill_nulls(con, "tool_calls", "tool_use_id", tuid, {
            "input_bytes": attrs.get("tool_input_size_bytes"),
            "result_bytes": attrs.get("tool_result_size_bytes"),
            "duration_ms": attrs.get("duration_ms"),
            "is_error": is_error,
            "error_type": attrs.get("error_type"),
        })
    # other event types (assistant_response, tool_decision, ...) are ignored


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if self.path.rstrip("/") == "/v1/logs":
                payload = json.loads(body)
                with _db_lock:
                    for rl in payload.get("resourceLogs", []):
                        for sl in rl.get("scopeLogs", []):
                            for rec in sl.get("logRecords", []):
                                handle_record(_con, rec)
                    _con.commit()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception:
            log.exception("failed to process %s", self.path)
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *args):
        pass


# Tables whose growth means the dashboard has something new to say. agents and
# session_events are here because a reconcile can add nothing but those - a
# session that compacted, switched model or ran a subagent - and the page has
# something to show for each. A table the schema does not have yet contributes
# nothing instead of raising.
FINGERPRINT_TABLES = ("api_requests", "prompts", "tool_calls", "edits",
                      "agents", "session_events")


def data_fingerprint(con):
    """(count, max rowid) per table - cheap proof that data arrived.

    Rebuilding costs a full collect() plus a megabyte of HTML, so it should
    only happen when there is something to show. A row *updated* in place (an
    OTel cost correcting an estimate) moves neither number and waits for the
    next insert to be published, which on a live machine is seconds away.
    """
    present = tables(con)
    return tuple(
        tuple(con.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM %s" % t).fetchone())
        if t in present else None
        for t in FINGERPRINT_TABLES)


def ingest_kwargs(db_path):
    """`db_path=` for jsonl_ingest.run() if it takes one."""
    try:
        params = inspect.signature(jsonl_ingest.run).parameters
    except (TypeError, ValueError):
        return {}
    return {"db_path": db_path} if "db_path" in params else {}


def reconcile(db_path=None):
    """One reconciliation pass over every configured source.

    db_path is threaded all the way through. Resolving it once in main() and
    then letting the reconciler fall back to its own default would put live
    rows in one database and transcript rows in another, and silently create
    and migrate the default file as a side effect of --db.

    Remote machines (sources.json) are fetched *before* the DB lock is taken:
    an SSH transfer can run for minutes, and holding the lock that long would
    stall the live telemetry the receiver exists to accept. The fetch touches
    only the remote_state bookkeeping, on its own short-lived connection.
    """
    db_path = db_path or resolve_db()
    cfg = sources.SourceConfig.load()
    if cfg.hosts():
        # Wrapped on its own: a remote that misbehaves, or a hiccup writing
        # its bookkeeping, must never cost us the local reconcile below -
        # that is the part that matters on this machine.
        try:
            con = db.connect(db_path)
            try:
                log.info("remote fetch: %s", jsonl_ingest.fetch_remotes(
                    con, cfg, respect_backoff=True))
            finally:
                con.close()
        except Exception:
            log.exception("remote fetch failed; continuing with local sources")
    # Only the ingest itself holds the lock. Everything that does not write -
    # loading sources.json, the remote fetch above, and the logging around the
    # call - now sits outside it.
    # TODO(Agent A): jsonl_ingest.run() also discovers source directories and
    # stats every transcript while holding this lock. Once it commits per
    # transcript, the lock can go entirely: run() writes on its own connection,
    # so SQLite's own writer serialisation is enough.
    with _db_lock:
        return jsonl_ingest.run(config=cfg, skip_remote_fetch=True,
                                **ingest_kwargs(db_path))


def build_kwargs(db_path=None):
    """What build() will accept: no self-probe (we are the receiver), our db."""
    try:
        params = inspect.signature(build_dashboard.build).parameters
    except (TypeError, ValueError):
        return {}
    out = {}
    if "check_receiver" in params:
        out["check_receiver"] = False
    if db_path and "db_path" in params:
        # build() gets our connection, but conversation pages and index.html
        # open their own; without this they would use the default database.
        out["db_path"] = db_path
    return out


def maintenance_loop(db_path=None):
    """Reconcile hourly; rebuild the dashboard when the data actually changed.

    The rebuild runs on its own connection, outside `_db_lock`. Reading and
    rendering takes seconds, and holding the lock across it would make live
    telemetry wait on a page nobody is looking at yet; WAL lets a reader run
    beside the writer, and the read is of committed rows either way.
    """
    db_path = db_path or resolve_db()
    kwargs = build_kwargs(db_path)
    last_reconcile = 0.0
    last_fingerprint = None
    build_con = None
    while True:
        try:
            stale = code_is_stale()
            now = time.time()
            if now - last_reconcile >= RECONCILE_EVERY:
                stats = reconcile(db_path)
                last_reconcile = now
                log.info("reconcile: %s", stats)
            if not stale:
                with _db_lock:
                    fingerprint = data_fingerprint(_con)
                if fingerprint != last_fingerprint:
                    if build_con is None:
                        build_con = db.connect(db_path)
                    build_con.rollback()   # start from the latest snapshot
                    build_dashboard.build(build_con, **kwargs)
                    last_fingerprint = fingerprint
                    log.info("dashboard rebuilt")
        except Exception:
            log.exception("maintenance loop error")
        time.sleep(REBUILD_CHECK)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Receive Claude Code OTel telemetry into metrics.db and "
                    "keep dashboard.html fresh.")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to write (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, then metrics.db)")
    ap.add_argument("--port", type=int, default=PORT, metavar="N",
                    help=f"port to listen on for OTLP/HTTP (default {PORT}); "
                         "binding it is also what keeps one receiver running")
    return ap.parse_args(argv)


def main(argv=None):
    global _con
    args = parse_args(argv)
    path = resolve_db(args.db)
    _con = db.connect(path, cross_thread=True)
    server = ThreadingHTTPServer((HOST, args.port), Handler)  # fails if already running
    threading.Thread(target=maintenance_loop, args=(path,), daemon=True).start()
    log.info("receiver listening on %s:%s (db %s)", HOST, args.port, path)
    server.serve_forever()


if __name__ == "__main__":
    main()
