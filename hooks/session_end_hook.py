"""SessionEnd hook: ingest the transcript that just ended, refresh the page.

Claude Code runs this with the hook payload on stdin
(https://code.claude.com/docs/en/hooks): session_id, transcript_path, cwd,
hook_event_name, reason, permission_mode. Only transcript_path is needed here.

Why a hook rather than a watcher: Anthropic documents the transcript JSONL
layout as internal and subject to change, and points at hooks as the supported
way to react to a session. This keeps the *timing* supported even though the
parsing is still our own.

Rules this file lives by:
- **Never fails the session.** Every path exits 0; nothing is raised out of
  main(). A usage dashboard is not worth an error at the end of someone's work.
- **Nothing on stdout.** Hook stdout is interpreted (JSON control fields), so
  anything the ingester or builder prints is captured and logged instead.
- **Fast.** SessionEnd hooks share a 1.5 second budget unless the settings give
  a longer per-hook `timeout`; see the README. `--no-build` keeps it to the
  ingest alone.
"""
import argparse
import contextlib
import io
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HOOK_DIR)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

LOG_PATH = os.path.join(HOOK_DIR, "hook.log")
LOG_BYTES = 1_000_000       # one megabyte, then rotate


def get_log():
    log = logging.getLogger("claude_lens.session_end")
    if not log.handlers:
        log.setLevel(logging.INFO)
        try:
            handler = RotatingFileHandler(LOG_PATH, maxBytes=LOG_BYTES,
                                          backupCount=1, encoding="utf-8")
        except OSError:
            handler = logging.NullHandler()
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.propagate = False       # never reach a root handler on stdout
    return log


def read_payload(stream=None):
    """The hook JSON from stdin. Anything unreadable is an empty payload."""
    stream = sys.stdin if stream is None else stream
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_db(explicit=None):
    import db
    fn = getattr(db, "resolve_path", None)
    if fn is not None:
        return fn(explicit)
    return explicit or os.environ.get("CLAUDE_LENS_DB") or db.DB_PATH


def ingest(con, path):
    """Ingest one transcript. Returns the entry point that was used.

    Prefers the single-file entry point; falls back to the whole-tree run only
    if this project has neither (in which case the hook is doing far more work
    than it should, and says so in the log).
    """
    import jsonl_ingest
    fn = getattr(jsonl_ingest, "ingest_file", None)
    if fn is not None:
        fn(con, path)
        return "ingest_file"
    fn = getattr(jsonl_ingest, "ingest_main_file", None)
    if fn is not None:
        fn(con, path)
        mark = getattr(jsonl_ingest, "mark_ingested", None)
        if mark is not None:
            # So the next full pass skips a file nothing will append to.
            try:
                mark(con, path)
            except Exception:
                pass
        return "ingest_main_file"
    jsonl_ingest.run()
    return "run"


def rebuild(db_path=None):
    """Rebuild dashboard.html unless a receiver owns it. Returns what it did.

    db_path is passed through when the builder accepts it: with --db the
    transcript went into that database, so the page must be rendered from
    the same one rather than from the default metrics.db.
    """
    import build_dashboard
    if build_dashboard.receiver_running():
        return "skipped (receiver listening on 127.0.0.1:4318)"
    kwargs = {}
    try:
        import inspect
        params = inspect.signature(build_dashboard.build).parameters
        if "check_receiver" in params:
            kwargs["check_receiver"] = False
        if db_path and "db_path" in params:
            kwargs["db_path"] = db_path
    except (TypeError, ValueError):
        pass
    build_dashboard.build(**kwargs)
    return "rebuilt"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Claude Code SessionEnd hook: ingest the finished "
                    "transcript and refresh the dashboard.")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to write (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, then metrics.db)")
    ap.add_argument("--transcript", metavar="PATH", default=None,
                    help="transcript to ingest instead of the one named in the "
                         "hook payload (for testing)")
    ap.add_argument("--no-build", action="store_true",
                    help="ingest only; leave dashboard.html to the next build")
    return ap.parse_args(argv)


def main(argv=None, stream=None):
    log = get_log()
    started = time.time()
    try:
        args = parse_args(argv)
        payload = read_payload(stream)
        path = args.transcript or payload.get("transcript_path")
        session = payload.get("session_id") or "?"
        if not path:
            log.info("session=%s no transcript_path in payload; nothing to do",
                     session)
            return 0
        if not os.path.exists(path):
            log.info("session=%s transcript is gone (%s)", session, path)
            return 0
        # db.connect() prints when it migrates, and build() prints notices.
        # Hook stdout is interpreted by Claude Code, so it goes to the log.
        noise = io.StringIO()
        with contextlib.redirect_stdout(noise):
            import db
            con = db.connect(resolve_db(args.db))
            try:
                used = ingest(con, path)
                con.commit()
            finally:
                con.close()
            built = ("not requested" if args.no_build
                     else rebuild(resolve_db(args.db)))
        text = noise.getvalue().strip()
        log.info("session=%s ingested %s via %s; dashboard %s; %.2fs%s",
                 session, os.path.basename(path), used, built,
                 time.time() - started, ("; " + text.replace("\n", " ")
                                         if text else ""))
    except Exception:
        # Never let a reporting tool break the end of a session.
        log.exception("session end hook failed after %.2fs",
                      time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
