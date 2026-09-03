"""Tests for the transcript ingester and the storage rules it depends on.

Every fixture here is a hand-written JSONL transcript in a temporary
directory, shaped like the real ones: the field names, entry types and
nesting were read off transcripts under ~/.claude/projects rather than
invented, because the whole point of the ingester is that it agrees with what
Claude Code actually writes.

Run: python3 test_ingest.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import db
import jsonl_ingest as ji
import sources


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

SESSION = "11111111-2222-3333-4444-555555555555"
PROJECT = "-home-someone-repo"


def entry(**kw):
    """A transcript entry with the envelope every real one carries."""
    base = {"sessionId": SESSION, "cwd": "/home/someone/repo",
            "version": "2.1.258", "gitBranch": "main",
            "entrypoint": "claude-vscode", "userType": "external",
            "isSidechain": False}
    base.update(kw)
    return base


def human(prompt_id, text, ts, blocks=None):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return entry(type="user", promptId=prompt_id, uuid=prompt_id, ts=None,
                 timestamp=ts, origin={"kind": "human"},
                 message={"role": "user", "content": content})


def assistant(request_id, ts, model="claude-opus-5", output=100, blocks=(),
              effort="high", speed="standard", thinking=7, stop="end_turn",
              usage_extra=None, agent=None):
    usage = {"input_tokens": 12, "cache_creation_input_tokens": 300,
             "cache_read_input_tokens": 4000, "output_tokens": output,
             "cache_creation": {"ephemeral_5m_input_tokens": 300,
                                "ephemeral_1h_input_tokens": 0},
             "output_tokens_details": {"thinking_tokens": thinking},
             "server_tool_use": {"web_search_requests": 2,
                                 "web_fetch_requests": 1},
             "service_tier": "standard", "inference_geo": "not_available",
             "speed": speed}
    if usage_extra:
        usage.update(usage_extra)
    e = entry(type="assistant", requestId=request_id, timestamp=ts,
              effort=effort,
              message={"role": "assistant", "model": model,
                       "stop_reason": stop, "content": list(blocks),
                       "usage": usage})
    if agent:
        e["agentId"] = agent
        e["isSidechain"] = True
    return e


def tool_use(tool_id, name, inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def tool_result(tool_id, ts, content, is_error=False, tool_use_result=None):
    e = entry(type="user", promptId="ignored", timestamp=ts,
              message={"role": "user",
                       "content": [{"type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": content,
                                    "is_error": is_error}]})
    if tool_use_result is not None:
        e["toolUseResult"] = tool_use_result
    return e


def attachment(kind, ts, payload):
    return entry(type="attachment", timestamp=ts,
                 attachment=dict(payload, type=kind))


class TranscriptCase(unittest.TestCase):
    """A temp Claude directory with one project, plus a fresh database."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lens-ingest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.claude_dir = os.path.join(self.tmp, ".claude")
        self.projects = os.path.join(self.claude_dir, "projects")
        os.makedirs(os.path.join(self.projects, PROJECT))
        self.con = db.connect(os.path.join(self.tmp, "metrics.db"))
        self.addCleanup(self.con.close)

    def write(self, entries, session=SESSION):
        path = os.path.join(self.projects, PROJECT, session + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def write_subagent(self, agent_id, entries, session=SESSION):
        folder = os.path.join(self.projects, PROJECT, session, "subagents")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "agent-%s.jsonl" % agent_id)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def ingest(self, force=True):
        return ji.ingest_tree(self.con, self.projects, force=force)

    def rows(self, sql, *args):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, *args):
        row = self.con.execute(sql, args).fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------

class StreamedDuplicates(TranscriptCase):
    """One API request is written once per content block; the last one wins."""

    def transcript(self):
        return [
            human("p1", "do the thing", "2026-09-01T10:00:00.000Z"),
            # First block: the request has barely started streaming.
            assistant("req_A", "2026-09-01T10:00:01.000Z", output=4),
            # Final block: the complete usage for the same request.
            assistant("req_A", "2026-09-01T10:00:09.000Z", output=880),
        ]

    def test_last_chunk_wins(self):
        self.write(self.transcript())
        self.ingest()
        self.assertEqual(self.one("SELECT COUNT(*) FROM api_requests"), 1)
        self.assertEqual(self.one("SELECT output_tokens FROM api_requests"), 880)

    def test_reparse_corrects_a_stale_row(self):
        """A row captured mid-stream is repaired by re-parsing the file.

        This is the behaviour schema v8 exists for: before it, transcript
        inserts were insert-or-ignore and the stub row was permanent.
        """
        db.upsert_request(self.con, {
            "request_id": "req_A", "session_id": SESSION, "model": "claude-opus-5",
            "output_tokens": 4, "input_tokens": 12,
            "ts": "2026-09-01T10:00:01.000Z"}, "jsonl")
        self.con.commit()
        self.assertEqual(self.one("SELECT output_tokens FROM api_requests"), 4)
        self.write(self.transcript())
        self.ingest()
        self.assertEqual(self.one("SELECT output_tokens FROM api_requests"), 880)

    def test_a_smaller_row_never_wins(self):
        db.upsert_request(self.con, {
            "request_id": "req_A", "session_id": SESSION,
            "output_tokens": 5000}, "jsonl")
        self.write(self.transcript())
        self.ingest()
        self.assertEqual(self.one("SELECT output_tokens FROM api_requests"), 5000)

    def test_otel_rows_are_never_touched(self):
        db.upsert_request(self.con, {
            "request_id": "req_A", "session_id": SESSION, "output_tokens": 7,
            "cost_usd": 0.5, "cost_basis": "contracted"}, "otel")
        self.write(self.transcript())
        self.ingest()
        row = self.rows("SELECT output_tokens, cost_usd, source, cost_basis "
                        "FROM api_requests")[0]
        self.assertEqual(row, (7, 0.5, "otel", "contracted"))

    def test_new_columns_are_populated(self):
        self.write(self.transcript())
        self.ingest()
        row = self.rows(
            """SELECT effort, speed, thinking_tokens, stop_reason,
                      server_tool_requests, service_tier, inference_geo,
                      context_tokens FROM api_requests""")[0]
        self.assertEqual(row[:4], ("high", "standard", 7, "end_turn"))
        self.assertEqual(row[4], 3)          # 2 web_search + 1 web_fetch
        self.assertEqual(row[5:7], ("standard", "not_available"))
        self.assertEqual(row[7], 12 + 4000 + 300)


class PromptText(TranscriptCase):
    """The IDE prepends its own text block in front of what a person typed."""

    WRAPPER = ("<ide_opened_file>The user opened the file /home/someone/repo/"
               "src/a/very/long/path/to/some/module.py in the IDE. This may or "
               "may not be related to the current task.</ide_opened_file>")

    def test_wrapper_is_stripped_and_the_prompt_is_searchable(self):
        self.write([
            human("p1", None, "2026-09-01T10:00:00.000Z", blocks=[
                {"type": "text", "text": self.WRAPPER},
                {"type": "text", "text": "rename the widget factory"}]),
            assistant("req_A", "2026-09-01T10:00:01.000Z"),
        ])
        self.ingest()
        text = self.one("SELECT text FROM prompts WHERE prompt_id='p1'")
        self.assertEqual(text, "rename the widget factory")
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM prompts WHERE text LIKE '%widget%'"), 1)

    def test_a_wrapper_only_turn_still_gets_text(self):
        """Never store a blank row: fall back to the envelope's contents."""
        msg = {"content": [{"type": "text", "text": self.WRAPPER}]}
        self.assertIn("The user opened the file", ji.prompt_text(msg))

    def test_wrapper_alone_is_not_a_prompt(self):
        self.assertTrue(ji.is_injected_text(
            {"content": [{"type": "text", "text": self.WRAPPER}]}))
        self.assertFalse(ji.is_injected_text(
            {"content": [{"type": "text", "text": self.WRAPPER},
                         {"type": "text", "text": "hello"}]}))

    def test_ide_wrappers_are_not_injection_markers(self):
        # Removing it from INJECTED_PREFIXES is the fix; keep it removed.
        self.assertNotIn("<ide_opened_file>", ji.INJECTED_PREFIXES)


class TaskNotificationFolding(TranscriptCase):
    """A background agent reports back through a prompt nobody typed."""

    def build(self):
        self.write([
            human("p-human", "run three agents", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", blocks=[
                tool_use("toolu_1", "Agent",
                         {"description": "Survey the tools",
                          "subagent_type": "general-purpose",
                          "model": "opus", "prompt": "go"})]),
            tool_result("toolu_1", "2026-09-01T10:00:02.000Z", "launched",
                        tool_use_result={"isAsync": True,
                                         "status": "async_launched",
                                         "agentId": "aa11bb22",
                                         "description": "Survey the tools",
                                         "resolvedModel": "claude-opus-5[1m]"}),
            entry(type="user", promptId="p-notify",
                  timestamp="2026-09-01T10:05:00.000Z",
                  origin={"kind": "task-notification"},
                  message={"role": "user",
                           "content": "<task-notification>\n<task-id>x</task-id>"
                                      "\n</task-notification>"}),
        ])
        self.write_subagent("aa11bb22", [
            entry(type="user", promptId="p-notify", agentId="aa11bb22",
                  isSidechain=True, timestamp="2026-09-01T10:01:00.000Z",
                  message={"role": "user", "content": "go"}),
            assistant("req_sub", "2026-09-01T10:02:00.000Z", output=555,
                      agent="aa11bb22", model="claude-opus-5"),
        ])
        self.ingest()

    def test_notification_prompt_folds_to_the_human_prompt(self):
        self.build()
        row = self.rows("SELECT injected, canonical_id, kind FROM prompts "
                        "WHERE prompt_id='p-notify'")[0]
        self.assertEqual(row, (1, "p-human", "task-notification"))

    def test_human_prompt_keeps_injected_zero(self):
        self.build()
        self.assertEqual(
            self.rows("SELECT injected, kind FROM prompts WHERE prompt_id='p-human'")[0],
            (0, "human"))

    def test_subagent_usage_hangs_off_the_notification(self):
        self.build()
        self.assertEqual(
            self.rows("SELECT prompt_id, agent_name, query_source, output_tokens "
                      "FROM api_requests WHERE request_id='req_sub'")[0],
            ("p-notify", "aa11bb22", "subagent", 555))

    def test_agents_table_is_populated(self):
        self.build()
        row = self.rows(
            """SELECT session_id, prompt_id, subagent_type, requested_model,
                      resolved_model, description, tool_use_id
               FROM agents WHERE agent_id='aa11bb22'""")[0]
        self.assertEqual(row, (SESSION, "p-human", "general-purpose", "opus",
                               "claude-opus-5[1m]", "Survey the tools",
                               "toolu_1"))

    def test_agent_id_joins_the_subagent_transcript(self):
        self.build()
        self.assertEqual(
            self.one("""SELECT COUNT(*) FROM api_requests r
                        JOIN agents a ON a.agent_id = r.agent_name"""), 1)

    def test_prompt_kind_from_text_when_no_origin_marker(self):
        self.assertEqual(
            ji.prompt_kind({}, "<task-notification>\nx"), "task-notification")
        self.assertEqual(ji.prompt_kind({"origin": {"kind": "coordinator"}}),
                         "coordinator")
        self.assertEqual(ji.prompt_kind({"origin": {"kind": "teammate"}}), "team")
        self.assertEqual(ji.prompt_kind({"origin": {"kind": "command"}}),
                         "command")
        self.assertEqual(ji.prompt_kind({"origin": {"kind": "wat"}}), "other")
        self.assertEqual(ji.prompt_kind({}, "<command-name>/loop"), "command")
        self.assertIsNone(ji.prompt_kind({}, "just a sentence"))

    def test_unknown_kinds_are_recorded_as_other(self):
        """A marker a later CLI invents must not vanish from the table."""
        for raw in ("something-new", "sdk-driver", ""):
            kind = ji.prompt_kind({"origin": {"kind": raw}})
            self.assertTrue(kind is None or kind in ji.KNOWN_KINDS, raw)
        self.assertEqual(ji.prompt_kind({"origin": {"kind": "something-new"}}),
                         "other")


class SessionEvents(TranscriptCase):

    def test_context_reading_from_the_attachment(self):
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            attachment("total_tokens_reminder", "2026-09-01T10:00:05.000Z",
                       {"text": "<total_tokens>14967339 tokens left</total_tokens>"}),
            assistant("req_A", "2026-09-01T10:00:06.000Z"),
        ])
        self.ingest()
        self.assertEqual(
            self.rows("SELECT prompt_id, value FROM session_events "
                      "WHERE kind='context'"),
            [("p1", 14967339)])

    def test_compaction_marker(self):
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            entry(type="user", promptId="p2", isCompactSummary=True,
                  timestamp="2026-09-01T11:00:00.000Z",
                  message={"role": "user",
                           "content": "This session is being continued from a "
                                      "previous conversation."}),
            assistant("req_A", "2026-09-01T11:00:01.000Z"),
        ])
        self.ingest()
        self.assertEqual(
            self.one("SELECT detail FROM session_events WHERE kind='compact'"),
            "isCompactSummary")

    def test_model_effort_and_speed_switches(self):
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z",
                      model="claude-opus-5", effort="high", speed="standard"),
            assistant("req_B", "2026-09-01T10:05:00.000Z",
                      model="claude-sonnet-5", effort="low", speed="fast"),
        ])
        self.ingest()
        got = dict(self.rows("SELECT kind, detail FROM session_events "
                             "WHERE kind LIKE '%switch'"))
        self.assertEqual(got, {
            "model_switch": "claude-opus-5->claude-sonnet-5",
            "effort_switch": "high->low",
            "speed_switch": "standard->fast"})

    def test_subagent_requests_do_not_count_as_switches(self):
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", model="claude-opus-5"),
        ])
        self.write_subagent("bb99", [
            entry(type="user", promptId="p1", agentId="bb99", isSidechain=True,
                  timestamp="2026-09-01T10:00:02.000Z",
                  message={"role": "user", "content": "go"}),
            assistant("req_S", "2026-09-01T10:00:03.000Z", agent="bb99",
                      model="claude-haiku-4-5"),
        ])
        self.ingest()
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM session_events WHERE kind='model_switch'"),
            0)

    def test_events_do_not_multiply_on_re_ingest(self):
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            attachment("total_tokens_reminder", "2026-09-01T10:00:05.000Z",
                       {"text": "<total_tokens>500 tokens left</total_tokens>"}),
            assistant("req_A", "2026-09-01T10:00:06.000Z"),
        ])
        self.ingest()
        self.ingest()
        self.ingest()
        self.assertEqual(self.one("SELECT COUNT(*) FROM session_events"), 1)

    def test_switch_is_recomputed_from_the_db_on_an_append(self):
        """An incremental re-ingest starts mid-session; the previous request
        is in the database, not in the part of the file being read."""
        first = [human("p1", "hello", "2026-09-01T10:00:00.000Z"),
                 assistant("req_A", "2026-09-01T10:00:01.000Z",
                           model="claude-opus-5")]
        self.write(first)
        self.ingest(force=False)
        self.write(first + [assistant("req_B", "2026-09-01T10:30:00.000Z",
                                      model="claude-sonnet-5")])
        self.ingest(force=False)
        self.assertEqual(
            self.one("SELECT detail FROM session_events WHERE kind='model_switch'"),
            "claude-opus-5->claude-sonnet-5")

    def test_a_full_reparse_does_not_invent_a_switch(self):
        """Seeding must be bounded by the timestamp, or a forced re-parse
        compares a session's first request against its own last one."""
        self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", model="claude-opus-5"),
            assistant("req_B", "2026-09-01T10:05:00.000Z", model="claude-sonnet-5"),
        ])
        self.ingest()
        self.ingest()
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM session_events WHERE kind='model_switch'"),
            1)


class SessionDescriptors(TranscriptCase):

    def test_branch_version_entrypoint_and_span(self):
        path = self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            entry(type="user", promptId="p2", permissionMode="bypassPermissions",
                  timestamp="2026-09-01T10:00:02.000Z",
                  origin={"kind": "coordinator"},
                  message={"role": "user", "content": "<coordinator>x"}),
            assistant("req_A", "2026-09-01T12:00:00.000Z"),
        ])
        self.ingest()
        row = self.rows(
            """SELECT git_branch, cli_version, entrypoint, permission_mode,
                      transcript_path, first_ts, last_ts FROM sessions""")[0]
        self.assertEqual(row[:4], ("main", "2.1.258", "claude-vscode",
                                   "bypassPermissions"))
        self.assertEqual(row[4], path)
        self.assertEqual(row[5], "2026-09-01T10:00:00.000Z")
        self.assertEqual(row[6], "2026-09-01T12:00:00.000Z")


class ToolCallSizes(TranscriptCase):

    def build(self):
        big = "x" * 5000
        self.write([
            human("p1", "read some files", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", blocks=[
                tool_use("toolu_ok", "Read", {"file_path": "/tmp/a.txt"}),
                tool_use("toolu_bad", "Bash", {"command": "false"}),
                tool_use("toolu_bash", "Bash", {"command": "echo hi"}),
                tool_use("toolu_skill", "Skill", {"skill": "dataviz"})]),
            tool_result("toolu_ok", "2026-09-01T10:00:02.000Z", big),
            tool_result("toolu_bad", "2026-09-01T10:00:03.000Z",
                        "Error: command failed", is_error=True),
            # A Bash result's real weight is in toolUseResult, which the
            # visible block only summarises.
            tool_result("toolu_bash", "2026-09-01T10:00:04.000Z", "hi",
                        tool_use_result={"stdout": "hi" * 100, "stderr": "",
                                         "interrupted": False}),
        ])
        self.ingest()

    def test_result_bytes(self):
        self.build()
        self.assertEqual(self.one("SELECT result_bytes FROM tool_calls "
                                  "WHERE tool_use_id='toolu_ok'"), 5000)
        self.assertEqual(self.one("SELECT result_bytes FROM tool_calls "
                                  "WHERE tool_use_id='toolu_bash'"), 200)

    def test_input_bytes_and_detail(self):
        self.build()
        row = self.rows("SELECT input_bytes, detail, tool_name FROM tool_calls "
                        "WHERE tool_use_id='toolu_skill'")[0]
        self.assertEqual(row[1:], ("dataviz", "Skill"))
        # Compact separators: the size is a property of the input, not of
        # the serialiser's spacing (and one encoder is reused per run).
        self.assertEqual(row[0], len(json.dumps({"skill": "dataviz"},
                                                separators=(",", ":"))))

    def test_is_error(self):
        self.build()
        self.assertEqual(dict(self.rows(
            "SELECT tool_use_id, is_error FROM tool_calls "
            "WHERE tool_use_id IN ('toolu_ok','toolu_bad')")),
            {"toolu_ok": 0, "toolu_bad": 1})

    def test_a_result_without_a_call_creates_no_row(self):
        self.build()
        self.assertIsNone(self.one("SELECT tool_name FROM tool_calls "
                                   "WHERE tool_use_id='toolu_missing'"))

    def test_fill_null_never_overwrites(self):
        self.build()
        db.insert_tool_call(self.con, "toolu_ok", None, None, None, "mcp_tool",
                            None, "otel", duration_ms=1234)
        self.assertEqual(self.rows(
            "SELECT tool_name, result_bytes, duration_ms FROM tool_calls "
            "WHERE tool_use_id='toolu_ok'")[0], ("Read", 5000, 1234))

    def test_content_len_handles_block_lists(self):
        self.assertEqual(ji.content_len("abc"), 3)
        self.assertEqual(ji.content_len([{"type": "text", "text": "abcd"}]), 4)
        self.assertEqual(ji.content_len(None), 0)


class FileHistoryRecovery(TranscriptCase):
    """Subagent Edit/Write calls leave no toolUseResult; the undo history does.

    Layout verified on disk: ~/.claude/file-history/<session>/<hash>@v<N>,
    where <hash> is sha256(absolute path)[:16] and each file is a full copy of
    that version.
    """

    TARGET = "/home/someone/repo/module.py"

    def build(self, versions):
        folder = os.path.join(self.claude_dir, "file-history", SESSION)
        os.makedirs(folder, exist_ok=True)
        digest = ji.backup_hash(self.TARGET)
        for number, body in versions.items():
            with open(os.path.join(folder, "%s@v%d" % (digest, number)),
                      "w", encoding="utf-8") as f:
                f.write(body)
        self.write([
            human("p1", "refactor it", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", blocks=[
                tool_use("toolu_agent", "Agent", {"subagent_type": "general-purpose",
                                                  "description": "edit"})]),
            tool_result("toolu_agent", "2026-09-01T10:00:02.000Z", "done",
                        tool_use_result={"agentId": "cc33", "status": "done",
                                         "resolvedModel": "claude-opus-5"}),
            {"type": "file-history-delta", "trackingPath": "module.py",
             "backup": {"backupFileName": None, "version": 1,
                        "backupTime": "2026-09-01T10:00:03.000Z",
                        "realParentDir": "/home/someone/repo"}},
            {"type": "file-history-snapshot", "messageId": "m1",
             "snapshot": {"messageId": "m1",
                          "timestamp": "2026-09-01T10:10:00.000Z",
                          "trackedFileBackups": {
                              "module.py": {
                                  "backupFileName": "%s@v2" % digest,
                                  "version": 2,
                                  "backupTime": "2026-09-01T10:10:00.000Z",
                                  "realParentDir": "/home/someone/repo"}}}},
        ])
        # The subagent edits the file; its user turns carry no toolUseResult,
        # so nothing about the change reaches `edits` the usual way.
        self.write_subagent("cc33", [
            entry(type="user", promptId="p1", agentId="cc33", isSidechain=True,
                  timestamp="2026-09-01T10:05:00.000Z",
                  message={"role": "user", "content": "go"}),
            assistant("req_S", "2026-09-01T10:05:01.000Z", agent="cc33", blocks=[
                tool_use("toolu_edit", "Edit", {"file_path": self.TARGET,
                                                "old_string": "a",
                                                "new_string": "b"})]),
            entry(type="user", promptId="p1", agentId="cc33", isSidechain=True,
                  timestamp="2026-09-01T10:05:02.000Z",
                  message={"role": "user",
                           "content": [{"type": "tool_result",
                                        "tool_use_id": "toolu_edit",
                                        "content": "ok"}]}),
        ])
        self.ingest()

    def test_recovers_the_diff(self):
        self.build({1: "one\ntwo\nthree\n", 2: "one\nTWO\nthree\nfour\n"})
        row = self.rows(
            """SELECT prompt_id, session_id, file_path, kind, lines_added,
                      lines_removed, source FROM edits WHERE source='file-history'""")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][:4], ("p1", SESSION, self.TARGET, "update"))
        self.assertEqual(row[0][4:], (2, 1, "file-history"))

    def test_one_version_cannot_be_diffed(self):
        self.build({2: "one\nTWO\n"})
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM edits WHERE source='file-history'"), 0)

    def test_never_double_counts_a_measured_edit(self):
        db.insert_edit(self.con, "toolu_other", "p1", SESSION,
                       "2026-09-01T10:04:00.000Z", self.TARGET, "update",
                       9, 9, 9, None, "jsonl")
        self.build({1: "one\ntwo\n", 2: "one\nTWO\nthree\n"})
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM edits WHERE source='file-history'"), 0)

    def test_backup_hash_matches_the_cli(self):
        # Checked against a real ~/.claude/file-history entry.
        self.assertEqual(len(ji.backup_hash("/tmp/x")), 16)
        self.assertNotEqual(ji.backup_hash("/tmp/x"), ji.backup_hash("/tmp/y"))

    def test_recovery_is_idempotent(self):
        self.build({1: "one\ntwo\nthree\n", 2: "one\nTWO\nthree\nfour\n"})
        self.ingest()
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM edits WHERE source='file-history'"), 1)


class SingleFileEntryPoint(TranscriptCase):
    """What the SessionEnd hook calls."""

    def test_ingests_a_main_transcript(self):
        path = self.write([
            human("p1", "hello", "2026-09-01T10:00:00.000Z"),
            assistant("req_A", "2026-09-01T10:00:01.000Z", output=42),
        ])
        self.assertTrue(ji.ingest_file(self.con, path))
        self.assertEqual(self.one("SELECT output_tokens FROM api_requests"), 42)
        self.assertEqual(self.one("SELECT COUNT(*) FROM ingest_state"), 1)

    def test_ingests_a_subagent_transcript(self):
        path = self.write_subagent("dd44", [
            entry(type="user", promptId="p1", agentId="dd44", isSidechain=True,
                  timestamp="2026-09-01T10:00:00.000Z",
                  message={"role": "user", "content": "go"}),
            assistant("req_S", "2026-09-01T10:00:01.000Z", agent="dd44",
                      output=13),
        ])
        ji.ingest_file(self.con, path)
        self.assertEqual(
            self.rows("SELECT agent_name, query_source, output_tokens "
                      "FROM api_requests")[0], ("dd44", "subagent", 13))

    def test_shape_detection(self):
        main = self.write([human("p1", "hi", "2026-09-01T10:00:00.000Z")])
        sub = self.write_subagent("ee55", [
            entry(type="user", promptId="p1", agentId="ee55", isSidechain=True,
                  timestamp="2026-09-01T10:00:00.000Z",
                  message={"role": "user", "content": "go"})])
        self.assertFalse(ji.is_subagent_transcript(main))
        self.assertTrue(ji.is_subagent_transcript(sub))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# The v7 CREATE TABLE text, captured verbatim so the migration is exercised
# against what a v7 database really looks like rather than against whatever
# db.SCHEMA says today.
V7_SCHEMA = """
CREATE TABLE prompts (
    prompt_id TEXT PRIMARY KEY, session_id TEXT, project TEXT, ts TEXT,
    text TEXT DEFAULT '', source TEXT, injected INTEGER DEFAULT 0,
    canonical_id TEXT);
CREATE TABLE api_requests (
    request_id TEXT PRIMARY KEY, prompt_id TEXT, session_id TEXT, ts TEXT,
    model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_create_tokens INTEGER DEFAULT 0,
    cache_5m_tokens INTEGER DEFAULT 0, cache_1h_tokens INTEGER DEFAULT 0,
    cost_usd REAL, duration_ms INTEGER, query_source TEXT, agent_name TEXT,
    source TEXT, model_raw TEXT, provider TEXT);
CREATE TABLE tool_calls (
    tool_use_id TEXT PRIMARY KEY, prompt_id TEXT, session_id TEXT, ts TEXT,
    tool_name TEXT, agent_name TEXT, source TEXT, detail TEXT);
CREATE TABLE edits (
    tool_use_id TEXT PRIMARY KEY, prompt_id TEXT, session_id TEXT, ts TEXT,
    file_path TEXT, kind TEXT, lines_added INTEGER DEFAULT 0,
    lines_removed INTEGER DEFAULT 0, chars_added INTEGER DEFAULT 0,
    agent_name TEXT, source TEXT);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT,
    source_label TEXT DEFAULT '', title TEXT);
CREATE TABLE run_cost (
    session_id TEXT PRIMARY KEY, cost_usd REAL, runs INTEGER, source TEXT);
CREATE TABLE remote_state (
    host TEXT PRIMARY KEY, last_fetch REAL, last_error TEXT,
    fail_count INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0);
CREATE TABLE ingest_state (path TEXT PRIMARY KEY, size INTEGER, mtime REAL);
"""


class MigrationToV8(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lens-migrate-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "v7.db")
        raw = sqlite3.connect(self.path)
        raw.executescript(V7_SCHEMA)
        raw.execute(
            """INSERT INTO api_requests
               (request_id, session_id, model, input_tokens, output_tokens,
                cache_read_tokens, cache_create_tokens, agent_name, source)
               VALUES ('req_old','s1','claude-opus-5',10,20,300,40,
                       'agent-abc123','jsonl')""")
        raw.execute("INSERT INTO tool_calls (tool_use_id, agent_name, source) "
                    "VALUES ('toolu_old','agent-abc123','jsonl')")
        raw.execute("INSERT INTO edits (tool_use_id, agent_name, source) "
                    "VALUES ('toolu_e','agent-abc123','jsonl')")
        raw.execute("INSERT INTO ingest_state VALUES ('/some/file.jsonl', 1, 2.0)")
        raw.execute("INSERT INTO prompts (prompt_id, text) VALUES ('p1','hi')")
        raw.execute("PRAGMA user_version = 7")
        raw.commit()
        raw.close()

    def migrate(self):
        con = db.connect(self.path)
        self.addCleanup(con.close)
        return con

    def test_version_is_stamped(self):
        self.assertEqual(self.migrate().execute(
            "PRAGMA user_version").fetchone()[0], 8)

    def test_new_columns_exist(self):
        con = self.migrate()
        for table, columns in db._V8_COLUMNS.items():
            have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
            for name, _ in columns:
                self.assertIn(name, have, "%s.%s" % (table, name))

    def test_new_tables_and_indexes_exist(self):
        con = self.migrate()
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for name in ("agents", "session_events", "idx_events_session",
                     "idx_events_uniq", "idx_req_ts"):
            self.assertIn(name, names)

    def test_existing_history_gets_context_tokens(self):
        self.assertEqual(self.migrate().execute(
            "SELECT context_tokens FROM api_requests").fetchone()[0],
            10 + 300 + 40)

    def test_agent_name_prefix_is_stripped(self):
        con = self.migrate()
        for table in ("api_requests", "tool_calls", "edits"):
            self.assertEqual(
                con.execute("SELECT agent_name FROM %s" % table).fetchone()[0],
                "abc123")

    def test_ingest_state_is_cleared_to_force_a_reparse(self):
        self.assertEqual(self.migrate().execute(
            "SELECT COUNT(*) FROM ingest_state").fetchone()[0], 0)

    def test_existing_rows_survive(self):
        con = self.migrate()
        self.assertEqual(con.execute(
            "SELECT output_tokens FROM api_requests").fetchone()[0], 20)
        self.assertEqual(con.execute(
            "SELECT text FROM prompts").fetchone()[0], "hi")

    def test_a_second_open_is_a_no_op(self):
        self.migrate()
        con = db.connect(self.path)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_a_newer_database_refuses_to_open(self):
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA user_version = 99")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError):
            db.connect(self.path)


class BackwardCompatibleWrites(unittest.TestCase):
    """A caller built against the v7 column list must keep working."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lens-compat-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.con = db.connect(os.path.join(self.tmp, "m.db"))
        self.addCleanup(self.con.close)

    def test_new_columns_are_appended_to_request_cols(self):
        self.assertEqual(
            db.REQUEST_COLS[:17],
            ("request_id", "prompt_id", "session_id", "ts", "model",
             "input_tokens", "output_tokens", "cache_read_tokens",
             "cache_create_tokens", "cache_5m_tokens", "cache_1h_tokens",
             "cost_usd", "duration_ms", "query_source", "agent_name",
             "model_raw", "provider"))

    def test_a_short_request_tuple_is_padded(self):
        db.insert_requests_jsonl(self.con, [
            ("req_1", "p1", "s1", "2026-09-01T00:00:00Z", "claude-opus-5",
             1, 2, 3, 4, 0, 0, None, None, "main", None, None, None)])
        self.assertEqual(self.con.execute(
            "SELECT output_tokens, context_tokens, effort FROM api_requests"
        ).fetchone(), (2, 8, None))

    def test_a_dict_row_fills_missing_keys_with_null(self):
        db.upsert_request(self.con, {"request_id": "req_2", "output_tokens": 5},
                          "jsonl")
        self.assertEqual(self.con.execute(
            "SELECT stop_reason, cost_basis FROM api_requests "
            "WHERE request_id='req_2'").fetchone(), (None, None))

    def test_short_prompt_and_tool_tuples_are_padded(self):
        db.upsert_prompts(self.con, [("p1", "s1", "proj", None, "hi", "jsonl",
                                      0, None)])
        db.insert_tool_calls(self.con, [("toolu_1", "p1", "s1", None, "Read",
                                         None, "jsonl", None)])
        self.assertIsNone(self.con.execute(
            "SELECT kind FROM prompts").fetchone()[0])
        self.assertIsNone(self.con.execute(
            "SELECT result_bytes FROM tool_calls").fetchone()[0])

    def test_an_over_long_tuple_is_rejected(self):
        with self.assertRaises(ValueError):
            db.upsert_prompts(self.con, [tuple(range(20))])


class ResolvePath(unittest.TestCase):
    """--db, then $CLAUDE_LENS_DB, then sources.json, then the default."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lens-path-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_env = os.environ.pop(db.DB_ENV_VAR, None)
        self.addCleanup(self._restore_env)
        self.old_cfg = sources.config_db_path
        self.addCleanup(setattr, sources, "config_db_path", self.old_cfg)
        sources.config_db_path = lambda *a, **k: None

    def _restore_env(self):
        os.environ.pop(db.DB_ENV_VAR, None)
        if self.old_env is not None:
            os.environ[db.DB_ENV_VAR] = self.old_env

    def test_default(self):
        self.assertEqual(db.resolve_path(), db.DB_PATH)

    def test_config_beats_default(self):
        wanted = os.path.join(self.tmp, "from-config.db")
        sources.config_db_path = lambda *a, **k: wanted
        self.assertEqual(db.resolve_path(), wanted)

    def test_env_beats_config(self):
        sources.config_db_path = lambda *a, **k: os.path.join(self.tmp, "cfg.db")
        wanted = os.path.join(self.tmp, "from-env.db")
        os.environ[db.DB_ENV_VAR] = wanted
        self.assertEqual(db.resolve_path(), wanted)

    def test_explicit_beats_everything(self):
        sources.config_db_path = lambda *a, **k: os.path.join(self.tmp, "cfg.db")
        os.environ[db.DB_ENV_VAR] = os.path.join(self.tmp, "env.db")
        wanted = os.path.join(self.tmp, "explicit.db")
        self.assertEqual(db.resolve_path(wanted), wanted)

    def test_user_home_is_expanded(self):
        os.environ[db.DB_ENV_VAR] = os.path.join("~", "lens.db")
        self.assertEqual(db.resolve_path(),
                         os.path.join(os.path.expanduser("~"), "lens.db"))

    def test_a_broken_sources_file_is_not_fatal(self):
        path = os.path.join(self.tmp, "sources.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        sources.config_db_path = self.old_cfg
        self.assertIsNone(sources.config_db_path(path))

    def test_the_db_key_is_read_from_sources_json(self):
        path = os.path.join(self.tmp, "sources.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"db": "D:/lens/metrics.db", "remotes": []}, f)
        sources.config_db_path = self.old_cfg
        self.assertEqual(sources.config_db_path(path), "D:/lens/metrics.db")

    def test_ingest_accepts_the_db_flag(self):
        self.assertEqual(ji.parse_args(["--db", "x.db"]).db, "x.db")
        self.assertIsNone(ji.parse_args([]).db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
