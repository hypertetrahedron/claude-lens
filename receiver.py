"""OTLP HTTP receiver for Claude Code telemetry -> metrics.db.

Listens on 127.0.0.1:4318 (OTLP/HTTP, JSON encoding). Claude Code is pointed
here via OTEL_EXPORTER_OTLP_ENDPOINT with OTEL_EXPORTER_OTLP_PROTOCOL=http/json.

Also acts as the system's scheduler:
- On startup and every RECONCILE_EVERY seconds: runs the JSONL reconciler
  (heals any gaps from receiver downtime; supplies session->project mapping).
- Every REBUILD_CHECK seconds: regenerates dashboard.html if new data arrived.

Single-instance safety: binding port 4318 fails if a receiver already runs.
"""
import gzip
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

HOST, PORT = "127.0.0.1", 4318
REBUILD_CHECK = 60          # seconds between dirty-checks for dashboard rebuild
RECONCILE_EVERY = 3600  # seconds between JSONL reconciliation passes
                        # (hourly: file-edit stats only arrive via JSONL)

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "receiver.log")

log = logging.getLogger("receiver")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)

# Prompts injected by the harness (not typed by the user) get their own
# prompt.id in telemetry; fold them into the preceding human prompt.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<teammate-message",
    "<system-reminder>",
    "<command-name>",
    "Caveat: The messages below",
)

_dirty = threading.Event()
_db_lock = threading.Lock()
_con = db.connect(cross_thread=True)


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


def handle_record(con, rec):
    body = (rec.get("body") or {}).get("stringValue", "")
    attrs = attr_dict(rec.get("attributes"))
    ts = attrs.get("event.timestamp")
    session_id = attrs.get("session.id")
    prompt_id = attrs.get("prompt.id")

    if body == "claude_code.user_prompt":
        text = attrs.get("prompt", "")
        injected = 1 if text.lstrip().startswith(INJECTED_PREFIXES) else 0
        canonical = canonical_for_session(con, session_id, ts) if injected else None
        db.upsert_prompt(con, prompt_id, session_id=session_id, ts=ts,
                         text=text, source="otel", injected=injected,
                         canonical_id=canonical)
    elif body == "claude_code.api_request":
        agent = attrs.get("agent.name")
        db.upsert_request(con, {
            "request_id": attrs.get("request_id"),
            "prompt_id": prompt_id,
            "session_id": session_id,
            "ts": ts,
            "model": attrs.get("model", "?"),
            "input_tokens": attrs.get("input_tokens", 0),
            "output_tokens": attrs.get("output_tokens", 0),
            "cache_read_tokens": attrs.get("cache_read_tokens", 0),
            "cache_create_tokens": attrs.get("cache_creation_tokens", 0),
            "cache_5m_tokens": 0,
            "cache_1h_tokens": 0,
            "cost_usd": attrs.get("cost_usd"),
            "duration_ms": attrs.get("duration_ms"),
            "query_source": attrs.get("query_source"),
            "agent_name": agent,
        }, "otel")
    elif body == "claude_code.tool_result":
        tuid = attrs.get("tool_use_id")
        if tuid:
            db.insert_tool_call(con, tuid, prompt_id, session_id, ts,
                                attrs.get("tool_name", "?"),
                                attrs.get("agent.name"), "otel")
    # other event types (assistant_response, tool_decision, ...) are ignored


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if self.path.rstrip("/") == "/v1/logs":
                payload = json.loads(body)
                n = 0
                with _db_lock:
                    for rl in payload.get("resourceLogs", []):
                        for sl in rl.get("scopeLogs", []):
                            for rec in sl.get("logRecords", []):
                                handle_record(_con, rec)
                                n += 1
                    _con.commit()
                if n:
                    _dirty.set()
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


def maintenance_loop():
    last_reconcile = 0.0
    while True:
        try:
            now = time.time()
            if now - last_reconcile >= RECONCILE_EVERY:
                with _db_lock:
                    stats = jsonl_ingest.run()
                last_reconcile = now
                _dirty.set()
                log.info("reconcile: %s", stats)
            if _dirty.is_set():
                _dirty.clear()
                with _db_lock:
                    build_dashboard.build(_con)
                log.info("dashboard rebuilt")
        except Exception:
            log.exception("maintenance loop error")
        time.sleep(REBUILD_CHECK)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)  # fails if already running
    threading.Thread(target=maintenance_loop, daemon=True).start()
    log.info("receiver listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
