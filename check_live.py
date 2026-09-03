"""Quick verification of live OTel ingestion (used during setup/testing).

Answers two questions: is telemetry arriving at all, and is it carrying the
newer attributes (effort, speed, cost basis, context size). A fill rate of 0%
on a column that exists usually means the CLI is older than the attribute, or
that OTEL_LOG_TOOL_DETAILS is off for the ones it gates.
"""
import argparse

import db

# Columns worth reporting on, per table: the ones the receiver only started
# writing with schema v8. A column missing from the schema is reported as such
# rather than skipped silently - it means this database has not been migrated.
FILL_COLUMNS = {
    "api_requests": ("effort", "speed", "cost_basis", "context_tokens",
                     "duration_ms", "cost_usd", "error"),
    "tool_calls": ("input_bytes", "result_bytes", "duration_ms", "is_error",
                   "error_type"),
    "prompts": ("kind",),
}


def resolve_db(explicit=None):
    fn = getattr(db, "resolve_path", None)
    if fn is not None:
        return fn(explicit)
    import os
    return explicit or os.environ.get("CLAUDE_LENS_DB") or db.DB_PATH


def columns(con, table):
    return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}


def fill_rates(con, table, limit):
    """(column, filled, total, present) over the newest `limit` OTel rows."""
    have = columns(con, table)
    total = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM %s WHERE source='otel' "
        "ORDER BY ts DESC LIMIT ?)" % table, (limit,)).fetchone()[0]
    out = []
    for col in FILL_COLUMNS[table]:
        if col not in have:
            out.append((col, 0, total, False))
            continue
        filled = con.execute(
            "SELECT COUNT(*) FROM (SELECT %s AS v FROM %s WHERE source='otel' "
            "ORDER BY ts DESC LIMIT ?) WHERE v IS NOT NULL" % (col, table),
            (limit,)).fetchone()[0]
        out.append((col, filled, total, True))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Show what live OTel ingestion has stored recently.")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to read (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, then metrics.db)")
    ap.add_argument("--limit", type=int, default=20, metavar="N",
                    help="rows to print per table (default 20)")
    ap.add_argument("--recent", type=int, default=200, metavar="N",
                    help="newest rows the fill rates are measured over "
                         "(default 200)")
    args = ap.parse_args(argv)

    path = resolve_db(args.db)
    con = db.connect(path)
    print("db:", path)
    for table in ("prompts", "api_requests", "tool_calls"):
        n = con.execute("SELECT COUNT(*) FROM %s WHERE source='otel'"
                        % table).fetchone()[0]
        print("otel %s: %d" % (table, n))

    print("")
    if not any(con.execute("SELECT COUNT(*) FROM %s WHERE source='otel'"
                           % t).fetchone()[0]
               for t in ("prompts", "api_requests", "tool_calls")):
        print("No live rows at all: the receiver is not running, or Claude "
              "Code is not pointed at it (see the README's live mode section).")
        print("")
    print("Fill rates over the newest %d otel rows per table:" % args.recent)
    missing = []
    for table in ("api_requests", "tool_calls", "prompts"):
        for col, filled, total, present in fill_rates(con, table, args.recent):
            if not present:
                missing.append("%s.%s" % (table, col))
                print("  %-26s not in this schema" % ("%s.%s" % (table, col)))
                continue
            pct = (100.0 * filled / total) if total else 0.0
            print("  %-26s %5d / %-5d  %5.1f%%"
                  % ("%s.%s" % (table, col), filled, total, pct))
    if missing:
        print("")
        print("NOTE: %d column(s) are absent - this database predates schema "
              "v8. Run any entry point once to migrate it." % len(missing))

    print("")
    for r in con.execute(
            """SELECT substr(prompt_id,1,8), model, input_tokens, output_tokens,
                      cache_read_tokens, round(cost_usd,5), query_source,
                      agent_name
               FROM api_requests WHERE source='otel'
               ORDER BY ts DESC LIMIT ?""", (args.limit,)):
        print("REQ:", r)
    for r in con.execute(
            """SELECT substr(prompt_id,1,8), injected, substr(canonical_id,1,8),
                      substr(text,1,60) FROM prompts WHERE source='otel'
               ORDER BY ts DESC LIMIT ?""", (args.limit,)):
        print("PROMPT:", r)
    for r in con.execute(
            """SELECT tool_use_id, tool_name, source FROM tool_calls
               WHERE source='otel' ORDER BY ts DESC LIMIT ?""", (args.limit,)):
        print("TOOL:", r)
    con.close()


if __name__ == "__main__":
    main()
