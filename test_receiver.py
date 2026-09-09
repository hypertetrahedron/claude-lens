"""Tests for the OTel receiver and the SessionEnd hook.

Run with `python3 test_receiver.py`.

Synthetic OTLP/HTTP JSON payloads go through `receiver.handle_record` into a
temporary database, and the stored rows are checked. Columns that a newer
schema adds are asserted only when the database actually has them, so this is
green both before and after the schema lands; whatever was skipped is printed
at the end rather than passing in silence.
"""
import gzip
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                             # noqa: E402
import jsonl_ingest
import sources
import receiver                                       # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(BASE, "hooks", "session_end_hook.py")

SKIPPED = set()          # "table.column" pairs this schema does not have yet


def tearDownModule():
    if SKIPPED:
        print("\nColumns not in this schema, assertions skipped: "
              + ", ".join(sorted(SKIPPED)))
    else:
        print("\nAll schema v%d columns present; nothing skipped."
              % db.SCHEMA_VERSION)


def record(body, attrs):
    """A synthetic OTLP/HTTP JSON log record."""
    def value(v):
        if isinstance(v, bool):
            return {"boolValue": v}
        if isinstance(v, int):
            return {"intValue": str(v)}     # OTLP/JSON sends ints as strings
        if isinstance(v, float):
            return {"doubleValue": v}
        return {"stringValue": str(v)}
    return {"body": {"stringValue": body},
            "attributes": [{"key": k, "value": value(v)}
                           for k, v in attrs.items() if v is not None]}


class ReceiverCase(unittest.TestCase):
    """A temp database plus the helpers for schema-tolerant assertions."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lens-receiver-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        receiver.reset_column_cache()
        self.addCleanup(receiver.reset_column_cache)
        self.con = db.connect(os.path.join(self.dir, "metrics.db"))
        self.addCleanup(self.con.close)

    def feed(self, body, attrs):
        receiver.handle_record(self.con, record(body, attrs))
        self.con.commit()

    def row(self, table, key_col, key):
        cols = sorted(receiver.columns(self.con, table))
        r = self.con.execute(
            "SELECT %s FROM %s WHERE %s=?" % (",".join(cols), table, key_col),
            (key,)).fetchone()
        return dict(zip(cols, r)) if r else None

    def check(self, table, row, expected):
        """Assert every expected column, skipping ones the schema lacks."""
        have = receiver.columns(self.con, table)
        for col, want in expected.items():
            if col not in have:
                SKIPPED.add("%s.%s" % (table, col))
                continue
            self.assertEqual(row[col], want, "%s.%s" % (table, col))


class ApiRequest(ReceiverCase):
    ATTRS = {
        "event.timestamp": "2026-09-01T10:00:00.000Z",
        "session.id": "sess-1",
        "prompt.id": "prompt-1",
        "model": "claude-sonnet-4-5-20250929",
        "cost_usd": 0.0123,
        "duration_ms": 4200,
        "input_tokens": 100,
        "output_tokens": 250,
        "cache_read_tokens": 9000,
        "cache_creation_tokens": 400,
        "request_id": "req_abc",
        "speed": "fast",
        "effort": "high",
        "query_source": "repl_main_thread",
        "agent.name": "Explore",
    }

    def test_core_columns(self):
        self.feed("claude_code.api_request", self.ATTRS)
        r = self.row("api_requests", "request_id", "req_abc")
        self.assertIsNotNone(r)
        self.assertEqual(r["prompt_id"], "prompt-1")
        self.assertEqual(r["session_id"], "sess-1")
        self.assertEqual(r["input_tokens"], 100)
        self.assertEqual(r["output_tokens"], 250)
        self.assertEqual(r["cache_read_tokens"], 9000)
        self.assertEqual(r["cache_create_tokens"], 400)
        self.assertAlmostEqual(r["cost_usd"], 0.0123)
        self.assertEqual(r["duration_ms"], 4200)
        # agent.name is the subagent *type*; agent_name holds the CLI's
        # agentId, which only a transcript knows. Storing the type here
        # replaced the id and broke the join to the agents table.
        self.assertIsNone(r["agent_name"])
        self.assertEqual(r["source"], "otel")
        self.assertEqual(r["model_raw"], "claude-sonnet-4-5-20250929")

    def test_new_columns(self):
        self.feed("claude_code.api_request", self.ATTRS)
        r = self.row("api_requests", "request_id", "req_abc")
        self.check("api_requests", r, {
            "effort": "high",
            "speed": "fast",
            # input + cache_read + cache_create
            "context_tokens": 9500,
            "cost_basis": receiver.cost_basis(),
            "error": None,
        })

    def test_cost_from_micros(self):
        attrs = dict(self.ATTRS)
        del attrs["cost_usd"]
        attrs["cost_usd_micros"] = 12300
        self.feed("claude_code.api_request", attrs)
        r = self.row("api_requests", "request_id", "req_abc")
        self.assertAlmostEqual(r["cost_usd"], 0.0123)

    def test_client_request_id_is_the_fallback_key(self):
        attrs = dict(self.ATTRS)
        del attrs["request_id"]
        attrs["client_request_id"] = "cli-uuid-1"
        self.feed("claude_code.api_request", attrs)
        self.assertIsNotNone(self.row("api_requests", "request_id",
                                      "cli-uuid-1"))

    def test_no_id_stores_nothing(self):
        attrs = dict(self.ATTRS)
        del attrs["request_id"]
        self.feed("claude_code.api_request", attrs)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM api_requests")
            .fetchone()[0], 0)

    def test_agent_name_stays_null_but_the_type_survives_in_query_source(self):
        """The type used to be written into agent_name, which is the CLI's
        agentId column and the only thing that joins the agents table (see
        CLAUDE.md's schema v9 note - it turned 36 distinct ids into 3 type
        names and broke every join). request_row() must never write it there
        again, and losing the type outright would be just as bad, so it has
        to still be readable off query_source.
        """
        attrs = dict(self.ATTRS, request_id="req_sub",
                    query_source="agent:builtin:general-purpose")
        self.feed("claude_code.api_request", attrs)
        r = self.row("api_requests", "request_id", "req_sub")
        self.assertIsNone(r["agent_name"])
        self.assertEqual(r["query_source"], "agent:builtin:general-purpose")
        self.assertTrue(db.is_subagent_qs(r["query_source"]))

    def test_speed_normal_is_stored_as_standard(self):
        """OTel's word for "not fast" is "normal"; the transcript's is
        "standard", and build_dashboard's switch detector and the fast-mode
        price multiplier both compare against the transcript's spelling.
        Storing OTel's own word here would fork live data into two spellings
        of the same thing that nothing joins - test_core_columns never
        exercises this because its fixture already uses "fast".
        """
        attrs = dict(self.ATTRS, request_id="req_speed", speed="normal")
        self.feed("claude_code.api_request", attrs)
        r = self.row("api_requests", "request_id", "req_speed")
        self.check("api_requests", r, {"speed": "standard"})


class ApiError(ReceiverCase):
    ATTRS = {
        "event.timestamp": "2026-09-01T10:05:00.000Z",
        "session.id": "sess-1",
        "prompt.id": "prompt-1",
        "model": "claude-sonnet-4-5-20250929",
        "error": "Overloaded",
        "status_code": 529,
        "duration_ms": 800,
        "attempt": 2,
        "request_id": "req_err",
    }

    def test_row_with_error_and_zero_tokens(self):
        self.feed("claude_code.api_error", self.ATTRS)
        r = self.row("api_requests", "request_id", "req_err")
        self.assertIsNotNone(r)
        self.assertEqual(r["input_tokens"], 0)
        self.assertEqual(r["output_tokens"], 0)
        self.assertEqual(r["cost_usd"], 0.0)
        self.assertEqual(r["duration_ms"], 800)
        self.check("api_requests", r, {"error": "529: Overloaded",
                                       "context_tokens": 0})

    def test_no_request_id_is_dropped(self):
        attrs = dict(self.ATTRS)
        del attrs["request_id"]
        self.feed("claude_code.api_error", attrs)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM api_requests")
            .fetchone()[0], 0)

    def test_error_never_zeroes_a_successful_row(self):
        self.feed("claude_code.api_request", dict(ApiRequest.ATTRS,
                                                  **{"request_id": "req_err"}))
        self.feed("claude_code.api_error", self.ATTRS)
        r = self.row("api_requests", "request_id", "req_err")
        self.assertEqual(r["output_tokens"], 250)
        self.check("api_requests", r, {"error": "529: Overloaded"})


class ToolResult(ReceiverCase):
    ATTRS = {
        "event.timestamp": "2026-09-01T10:06:00.000Z",
        "session.id": "sess-1",
        "prompt.id": "prompt-1",
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "success": "false",
        "duration_ms": 1500,
        "error_type": "Error:ENOENT",
        "tool_input_size_bytes": 220,
        "tool_result_size_bytes": 40960,
    }

    def test_stores_sizes_and_failure(self):
        self.feed("claude_code.tool_result", self.ATTRS)
        r = self.row("tool_calls", "tool_use_id", "toolu_1")
        self.assertIsNotNone(r)
        self.assertEqual(r["tool_name"], "Bash")
        self.assertEqual(r["source"], "otel")
        self.check("tool_calls", r, {
            "input_bytes": 220,
            "result_bytes": 40960,
            "duration_ms": 1500,
            "is_error": 1,
            "error_type": "Error:ENOENT",
        })

    def test_success_is_not_an_error(self):
        self.feed("claude_code.tool_result",
                  dict(self.ATTRS, success="true", error_type=None))
        r = self.row("tool_calls", "tool_use_id", "toolu_1")
        self.check("tool_calls", r, {"is_error": 0})

    def test_no_tool_use_id_is_dropped(self):
        attrs = dict(self.ATTRS)
        del attrs["tool_use_id"]
        self.feed("claude_code.tool_result", attrs)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM tool_calls")
            .fetchone()[0], 0)


class UserPrompt(ReceiverCase):
    BASE_ATTRS = {
        "event.timestamp": "2026-09-01T09:59:00.000Z",
        "session.id": "sess-1",
        "prompt.id": "prompt-1",
        "prompt": "add a test for the receiver",
        "prompt_length": 27,
    }

    def test_human_prompt(self):
        self.feed("claude_code.user_prompt", self.BASE_ATTRS)
        r = self.row("prompts", "prompt_id", "prompt-1")
        self.assertEqual(r["text"], "add a test for the receiver")
        self.assertEqual(r["injected"], 0)
        self.check("prompts", r, {"kind": "human"})

    def test_command_prompt(self):
        self.feed("claude_code.user_prompt",
                  dict(self.BASE_ATTRS, command_name="compact",
                       command_source="builtin"))
        r = self.row("prompts", "prompt_id", "prompt-1")
        self.check("prompts", r, {"kind": "command"})

    def test_known_command_kinds(self):
        self.assertEqual(receiver.prompt_kind(0, "loop", "builtin"), "loop")
        self.assertEqual(receiver.prompt_kind(0, "schedule", "builtin"),
                         "scheduled")
        self.assertEqual(receiver.prompt_kind(1, None, None), "other")
        self.assertEqual(receiver.prompt_kind(0, None, None), "human")

    def test_injected_prompt_folds_into_the_human_one(self):
        self.feed("claude_code.user_prompt", self.BASE_ATTRS)
        injected = receiver.INJECTED_PREFIXES[0] + " something"
        self.feed("claude_code.user_prompt",
                  dict(self.BASE_ATTRS, **{"prompt.id": "prompt-2",
                                           "prompt": injected,
                                           "event.timestamp":
                                               "2026-09-01T10:01:00.000Z"}))
        r = self.row("prompts", "prompt_id", "prompt-2")
        self.assertEqual(r["injected"], 1)
        self.assertEqual(r["canonical_id"], "prompt-1")


class CostBasis(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lens-settings-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.dir
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._env
        receiver._basis["at"] = 0.0

    def write(self, data):
        with open(os.path.join(self.dir, "settings.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f)
        receiver._basis["at"] = 0.0     # force a re-read

    def test_list_by_default(self):
        self.write({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}})
        self.assertEqual(receiver.cost_basis(), "list")

    def test_contracted_with_model_pricing(self):
        self.write({"modelPricing": {"multiplier": 0.85}})
        self.assertEqual(receiver.cost_basis(), "contracted")

    def test_missing_settings_file_is_list(self):
        receiver._basis["at"] = 0.0
        self.assertEqual(receiver.cost_basis(), "list")

    def test_settings_paths_cover_managed_locations(self):
        paths = receiver.settings_paths()
        self.assertIn(os.path.join(self.dir, "settings.json"), paths)
        for d in receiver.MANAGED_DIRS:
            self.assertIn(os.path.join(d, "managed-settings.json"), paths)

    def test_result_is_cached(self):
        self.write({"modelPricing": {"multiplier": 0.85}})
        self.assertEqual(receiver.cost_basis(), "contracted")
        os.remove(os.path.join(self.dir, "settings.json"))
        # No re-read inside the TTL, so the answer is unchanged.
        self.assertEqual(receiver.cost_basis(), "contracted")


class ReconcileLocking(ReceiverCase):
    """Where _db_lock is held, and where it deliberately is not.

    The lock exists for `_con`, which every ThreadingHTTPServer handler thread
    shares. It was also being held across the whole reconcile, so directory
    discovery and an os.stat per transcript blocked incoming telemetry for the
    length of a full ingest. The ingest writes on its own connection, so
    SQLite's one-writer rule already serialises it.
    """

    def setUp(self):
        super().setUp()
        self._real_run = jsonl_ingest.run
        self._real_load = sources.SourceConfig.load
        self._real_con = receiver._con
        receiver._con = self.con
        def restore():
            jsonl_ingest.run = self._real_run
            sources.SourceConfig.load = self._real_load
            receiver._con = self._real_con
        self.addCleanup(restore)
        sources.SourceConfig.load = staticmethod(lambda *a, **k: _NoHosts())

    def test_the_ingest_does_not_run_under_the_lock(self):
        """A held lock here stalls every POST for the length of the pass."""
        seen = {}

        def fake_run(*a, **kw):
            seen["locked"] = receiver._db_lock.locked()
            return {"scanned": 0, "ingested": 0}

        jsonl_ingest.run = fake_run
        receiver.reconcile(os.path.join(self.dir, "metrics.db"))
        self.assertIs(seen["locked"], False)

    def test_the_lock_is_free_again_afterwards(self):
        """_note_schema_version takes it after the ingest; it must give it back."""
        jsonl_ingest.run = lambda *a, **kw: {"scanned": 0, "ingested": 0}
        receiver.reconcile(os.path.join(self.dir, "metrics.db"))
        self.assertFalse(receiver._db_lock.locked())

    def test_a_failing_ingest_does_not_strand_the_lock(self):
        """An exception out of run() must not leave every POST blocked."""
        def boom(*a, **kw):
            raise RuntimeError("ingest exploded")

        jsonl_ingest.run = boom
        with self.assertRaises(RuntimeError):
            receiver.reconcile(os.path.join(self.dir, "metrics.db"))
        self.assertFalse(receiver._db_lock.locked())


class _NoHosts:
    """Stand-in SourceConfig: no remotes, so reconcile skips the SSH branch."""

    def hosts(self):
        return []


class Fingerprint(ReceiverCase):
    def test_changes_only_when_rows_arrive(self):
        first = receiver.data_fingerprint(self.con)
        self.assertEqual(first, receiver.data_fingerprint(self.con))
        receiver.handle_record(self.con, record("claude_code.api_request",
                                                ApiRequest.ATTRS))
        self.con.commit()
        second = receiver.data_fingerprint(self.con)
        self.assertNotEqual(first, second)
        # The same request again is an update, not an insert: neither the count
        # nor the max rowid moves, and no rebuild is triggered.
        receiver.handle_record(self.con, record("claude_code.api_request",
                                                ApiRequest.ATTRS))
        self.con.commit()
        self.assertEqual(second, receiver.data_fingerprint(self.con))

    def test_covers_every_table_the_dashboard_reads(self):
        # agents and session_events are included because a reconcile can add
        # nothing but those (a subagent launch, a compaction) and the page has
        # something new to show for each.
        self.assertEqual(set(receiver.FINGERPRINT_TABLES),
                         {"api_requests", "prompts", "tool_calls", "edits",
                          "agents", "session_events"})

    def test_missing_fingerprint_table_contributes_none(self):
        self.con.execute("DROP TABLE session_events")
        self.con.commit()
        receiver.reset_column_cache()
        fp = receiver.data_fingerprint(self.con)
        self.assertEqual(len(fp), len(receiver.FINGERPRINT_TABLES))
        self.assertIsNone(fp[receiver.FINGERPRINT_TABLES.index("session_events")])


TRANSCRIPT = [
    {"type": "user", "sessionId": "hook-sess", "cwd": "/tmp/proj",
     "promptId": "hook-prompt", "uuid": "u1",
     "timestamp": "2026-09-01T12:00:00.000Z",
     "origin": {"kind": "human"},
     "message": {"role": "user", "content": "hello from a hook test"}},
    {"type": "assistant", "sessionId": "hook-sess", "promptId": "hook-prompt",
     "requestId": "req_hook", "timestamp": "2026-09-01T12:00:05.000Z",
     "message": {"model": "claude-sonnet-4-5-20250929",
                 "usage": {"input_tokens": 12, "output_tokens": 34,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}},
]


class SessionEndHook(unittest.TestCase):
    """The hook is run as a real subprocess: stdin handling is the point."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lens-hook-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = os.path.join(self.dir, "metrics.db")
        proj = os.path.join(self.dir, "projects", "-tmp-proj")
        os.makedirs(proj)
        self.transcript = os.path.join(proj, "hook-sess.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as f:
            for entry in TRANSCRIPT:
                f.write(json.dumps(entry) + "\n")

    def run_hook(self, payload):
        return subprocess.run(
            [sys.executable, HOOK, "--db", self.db, "--no-build"],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=120)

    def test_ingests_the_transcript_named_on_stdin(self):
        proc = self.run_hook({"session_id": "hook-sess",
                              "transcript_path": self.transcript,
                              "cwd": "/tmp/proj",
                              "hook_event_name": "SessionEnd",
                              "reason": "clear"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "hooks must not write to stdout")
        con = db.connect(self.db)
        self.addCleanup(con.close)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM prompts WHERE prompt_id=?",
                        ("hook-prompt",)).fetchone()[0], 1)
        r = con.execute("SELECT output_tokens FROM api_requests "
                        "WHERE request_id=?", ("req_hook",)).fetchone()
        self.assertIsNotNone(r, "the assistant turn should have been ingested")
        self.assertEqual(r[0], 34)

    def test_missing_transcript_still_exits_zero(self):
        proc = self.run_hook({"session_id": "gone",
                              "transcript_path": os.path.join(self.dir,
                                                              "nope.jsonl")})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_garbage_stdin_still_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, HOOK, "--db", self.db, "--no-build"],
            input="not json at all", capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_empty_stdin_still_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, HOOK, "--db", self.db, "--no-build"],
            input="", capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_payload_parsing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("session_end_hook", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import io as _io
        self.assertEqual(mod.read_payload(_io.StringIO('{"a": 1}')), {"a": 1})
        self.assertEqual(mod.read_payload(_io.StringIO("[]")), {})
        self.assertEqual(mod.read_payload(_io.StringIO("  ")), {})
        self.assertEqual(mod.read_payload(_io.StringIO("{oops")), {})


class HttpSurface(unittest.TestCase):
    """The real request path, not handle_record() called directly.

    Every other test in this file drives receiver.handle_record straight, so
    none of them would notice a wrong path, a missing gzip decode, or one of
    the hardening checks (Origin, content type, size cap) silently dropped -
    all of which return 200 while quietly ingesting nothing, which is
    indistinguishable from telemetry that simply stopped arriving.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lens-receiver-http-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        receiver.reset_column_cache()
        self.addCleanup(receiver.reset_column_cache)
        # These tests deliberately send the requests the receiver is meant to
        # refuse, and every refusal is logged. receiver.log is the *live*
        # service's log, so without this a test run leaves warnings there that
        # read exactly like a real page attacking localhost:4318 - the one
        # place someone would look to find out whether that had happened.
        receiver.log.propagate = False
        self._old_level = receiver.log.level
        receiver.log.setLevel(logging.CRITICAL)
        self.addCleanup(receiver.log.setLevel, self._old_level)
        # do_POST reads these two module globals directly (see receiver.py's
        # Handler), so the real HTTP path can only be tested by pointing them
        # at a temp database the way main() would, then restoring them.
        self._old_con = receiver._con
        self._old_lock = receiver._db_lock
        receiver._con = db.connect(os.path.join(self.dir, "metrics.db"),
                                   cross_thread=True)
        receiver._db_lock = threading.Lock()
        self.server = HTTPServer(("127.0.0.1", 0), receiver.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        # shutdown()+server_close() release the ephemeral port even if a test
        # fails partway through; join() with a timeout keeps a misbehaving
        # handler from hanging the whole suite instead of just this test.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        receiver._con.close()
        receiver._con = self._old_con
        receiver._db_lock = self._old_lock

    def payload(self):
        rec = record("claude_code.api_request",
                     dict(ApiRequest.ATTRS, request_id="req_http"))
        return json.dumps(
            {"resourceLogs": [{"scopeLogs": [{"logRecords": [rec]}]}]}
        ).encode("utf-8")

    def post(self, path, body, headers=None):
        """POST `body` and return (status, response bytes).

        A short client-side timeout so a hang in do_POST fails this test
        instead of the whole suite.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            hdrs = {"Content-Type": "application/json"}
            if headers:
                hdrs.update(headers)
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def count(self):
        return receiver._con.execute(
            "SELECT COUNT(*) FROM api_requests").fetchone()[0]

    def test_a_plain_post_is_ingested(self):
        status, _ = self.post("/v1/logs", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(self.count(), 1)

    def test_gzip_encoded_body_is_decoded_and_ingested(self):
        status, _ = self.post("/v1/logs", gzip.compress(self.payload()),
                              headers={"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(self.count(), 1)

    def test_a_wrong_path_is_ignored(self):
        """200 (it might be some other OTLP signal this receiver does not
        collect), but nothing must land in the database - the only half of
        this that a drifted path check would get silently wrong.
        """
        status, _ = self.post("/v1/traces", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(self.count(), 0)

    def test_an_origin_header_is_rejected(self):
        """A page the user has open can fetch() to 127.0.0.1; a real
        OTLP/HTTP exporter never sends this header at all."""
        status, _ = self.post("/v1/logs", self.payload(),
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.count(), 0)

    def test_a_non_json_content_type_is_rejected(self):
        status, _ = self.post("/v1/logs", self.payload(),
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.count(), 0)

    def test_an_over_cap_content_length_is_rejected(self):
        """Rejected off the declared header alone, before a byte of the body
        is read - so this does not actually send MAX_BODY_BYTES of data."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", "/v1/logs", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length",
                          str(receiver.MAX_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            status = resp.status
            resp.read()
        finally:
            conn.close()
        self.assertEqual(status, 413)
        self.assertEqual(self.count(), 0)


class CliFlags(unittest.TestCase):
    def test_receiver_help_does_not_hang(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(BASE, "receiver.py"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--db", proc.stdout)
        self.assertIn("--port", proc.stdout)

    def test_receiver_parses_db_and_port(self):
        args = receiver.parse_args(["--db", "/tmp/x.db", "--port", "4319"])
        self.assertEqual(args.db, "/tmp/x.db")
        self.assertEqual(args.port, 4319)

    def test_check_live_help(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(BASE, "check_live.py"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--db", proc.stdout)

    def test_importing_the_receiver_opens_no_database(self):
        # A module-level db.connect() would create (and migrate) metrics.db
        # just by importing, which the tests above must not do.
        self.assertIsNone(receiver._con)


if __name__ == "__main__":
    unittest.main(verbosity=2)
