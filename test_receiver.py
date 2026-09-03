"""Tests for the OTel receiver and the SessionEnd hook.

Run with `python3 test_receiver.py`.

Synthetic OTLP/HTTP JSON payloads go through `receiver.handle_record` into a
temporary database, and the stored rows are checked. Columns that schema v8
adds are asserted only when the database actually has them, so this file is
green both before and after the schema lands; whatever was skipped is printed
at the end rather than passing in silence.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db                                             # noqa: E402
import receiver                                       # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(BASE, "hooks", "session_end_hook.py")

SKIPPED = set()          # "table.column" pairs this schema does not have yet


def tearDownModule():
    if SKIPPED:
        print("\nColumns not in this schema, assertions skipped: "
              + ", ".join(sorted(SKIPPED)))
    else:
        print("\nAll schema v8 columns present; nothing skipped.")


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
        self.assertEqual(r["agent_name"], "Explore")
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
        self.assertEqual(set(receiver.FINGERPRINT_TABLES),
                         {"api_requests", "prompts", "tool_calls", "edits"})


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
