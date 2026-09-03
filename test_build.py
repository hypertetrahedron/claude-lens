"""Tests for build_dashboard.py, conversations.py and digest.py.

Everything here works against a temporary metrics.db built through db.py's own
insert functions, so the fixtures exercise the real schema (and would fail on
a column that moved) without needing a transcript for every case. The one
place a transcript is genuinely required - the conversation pages, whose whole
job is to render one - gets a hand-written JSONL file on disk.

Run with:  python test_build.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_dashboard as bd            # noqa: E402
import conversations                    # noqa: E402
import db                               # noqa: E402
import digest                           # noqa: E402
import pricing                          # noqa: E402
import report_index                     # noqa: E402

UTC = timezone.utc
# A fixed point in the past: nothing here should depend on the wall clock, and
# a test that only passes before midnight is worse than no test.
T0 = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

SCRIPT_TEXT = "<script>alert('xss')</script> please refactor the parser"


def ts(minutes=0, days=0):
    return (T0 + timedelta(minutes=minutes, days=days)).isoformat().replace(
        "+00:00", "Z")


def request(rid, pid, sid, when, model, **kw):
    """A REQUEST_COLS dict with sensible zeroes for everything unsaid."""
    row = {"request_id": rid, "prompt_id": pid, "session_id": sid, "ts": when,
           "model": model, "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_create_tokens": 0,
           "cache_5m_tokens": 0, "cache_1h_tokens": 0, "cost_usd": None,
           "query_source": "main", "provider": pricing.ANTHROPIC,
           "effort": "high", "speed": "standard", "service_tier": "standard"}
    row.update(kw)
    if row.get("context_tokens") is None:
        row["context_tokens"] = (row["input_tokens"]
                                 + row["cache_read_tokens"]
                                 + row["cache_create_tokens"])
    return row


def decode_ctx(table, strings):
    """The template's rehydrateCtx(), in Python, for the round-trip test."""
    n, cols = table["n"], table["cols"]
    acc = table["t0"]
    out = []
    for i in range(n):
        acc += cols["t"][i]
        out.append({"session": None, "t": acc, "ctx": cols["ctx"][i],
                    "cr": cols["cr"][i], "cw": cols["cw"][i], "model": None,
                    "miss": 0, "cause": None, "event": None})
    for key in ("session", "model"):
        runs = table["runs"][key]
        for k, (start, idx) in enumerate(runs):
            stop = runs[k + 1][0] if k + 1 < len(runs) else n
            value = None if idx is None else strings[idx]
            for i in range(start, stop):
                out[i][key] = value
    for i in table["sparse"]["miss"]:
        out[i]["miss"] = 1
    for i, idx in table["sparse"]["cause"]:
        out[i]["cause"] = strings[idx]
    for i, idx in table["sparse"]["event"]:
        out[i]["event"] = strings[idx]
    return out


class Fixture(unittest.TestCase):
    """Two sessions, one folded task-notification, one subagent, one compaction.

    Session A: a model switch mid-turn, then a 72-minute idle gap, then a
    fast-mode request. Session B: a compaction between two requests. Between
    them every cache-miss cause this code can name is represented except
    "unknown", which is what is left when none of the others fit.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-build-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        self.transcript = os.path.join(self.projects, "sess-a.jsonl")
        con = self.con = db.connect(self.dbpath)

        db.upsert_session(con, "sess-a", project="proj", cwd="/work/proj",
                          transcript_path=self.transcript,
                          first_ts=ts(0), last_ts=ts(80))
        db.upsert_session(con, "sess-b", project="proj2", cwd="/work/proj2",
                          first_ts=ts(10), last_ts=ts(13))
        db.upsert_session(con, "sess-c", project="proj3", cwd="/work/proj3",
                          first_ts=ts(0, days=-9), last_ts=ts(0, days=-9))

        db.upsert_prompt(con, "p1", "sess-a", "proj", ts(0), SCRIPT_TEXT,
                         "jsonl", 0, None, "human")
        # A background agent reporting back: its own prompt, folded onto p1.
        db.upsert_prompt(con, "p2", "sess-a", "proj", ts(1),
                         "<task-notification>done</task-notification>",
                         "jsonl", 1, "p1", "task-notification")
        db.upsert_prompt(con, "p3", "sess-b", "proj2", ts(10),
                         "second session", "jsonl", 0, None, "human")
        # Last week, so the digest has something to compare against.
        db.upsert_prompt(con, "p0", "sess-c", "proj3", ts(0, days=-9),
                         "last week", "jsonl", 0, None, "human")

        rows = [
            # One API request written to the transcript twice: the first chunk
            # carries a fraction of the output, the last one carries all of it.
            request("r1", "p1", "sess-a", ts(0), OPUS, input_tokens=100,
                    output_tokens=1, cache_create_tokens=10000,
                    cache_1h_tokens=10000),
            request("r1", "p1", "sess-a", ts(0), OPUS, input_tokens=100,
                    output_tokens=200, cache_create_tokens=10000,
                    cache_1h_tokens=10000),
            # A cache hit.
            request("r2", "p1", "sess-a", ts(1), OPUS, input_tokens=50,
                    output_tokens=300, cache_read_tokens=10000,
                    cache_create_tokens=500, cache_5m_tokens=500),
            # Model changed under the session: a miss, and the cause is known.
            request("r3", "p1", "sess-a", ts(2), SONNET, input_tokens=50,
                    output_tokens=400, cache_create_tokens=11000,
                    cache_1h_tokens=11000, stop_reason="max_tokens"),
            request("r4", "p1", "sess-a", ts(3), SONNET, input_tokens=20,
                    output_tokens=100, cache_read_tokens=11000,
                    cache_create_tokens=200, cache_5m_tokens=200,
                    server_tool_requests=2, thinking_tokens=90),
            # 72 minutes later: past the 60-minute TTL, so the cache is cold.
            request("r5", "p1", "sess-a", ts(75), SONNET, input_tokens=30,
                    output_tokens=150, cache_create_tokens=12000,
                    cache_1h_tokens=12000),
            # Fast mode, billed at twice the list rate on Opus.
            request("r6", "p1", "sess-a", ts(76), OPUS, input_tokens=10,
                    output_tokens=500, cache_read_tokens=12000,
                    cache_create_tokens=100, cache_5m_tokens=100,
                    speed="fast"),
            # A subagent's work, recorded against the folded prompt.
            request("r7", "p2", "sess-a", ts(4), HAIKU, input_tokens=10,
                    output_tokens=250, cache_create_tokens=3000,
                    cache_5m_tokens=3000, query_source="subagent",
                    agent_name="ag1"),
            # Session B: a compaction lands between two requests.
            request("b1", "p3", "sess-b", ts(10), OPUS, input_tokens=40,
                    output_tokens=100, cache_create_tokens=8000,
                    cache_1h_tokens=8000),
            request("b2", "p3", "sess-b", ts(11), OPUS, input_tokens=10,
                    output_tokens=120, cache_read_tokens=8000,
                    cache_create_tokens=200, cache_5m_tokens=200),
            request("b3", "p3", "sess-b", ts(13), OPUS, input_tokens=10,
                    output_tokens=130, cache_create_tokens=9000,
                    cache_1h_tokens=9000),
            request("z1", "p0", "sess-c", ts(0, days=-9), OPUS,
                    input_tokens=1000, output_tokens=1000,
                    cache_create_tokens=1000, cache_1h_tokens=1000),
        ]
        db.insert_requests_jsonl(con, rows)

        db.insert_tool_call(con, "toolu_1", "p1", "sess-a", ts(0), "Bash",
                            None, "jsonl", None, 40, 1000, 0)
        db.insert_tool_call(con, "toolu_2", "p1", "sess-a", ts(1), "Bash",
                            None, "jsonl", None, 30, 3000, 1)
        db.insert_tool_call(con, "toolu_3", "p2", "sess-a", ts(2), "Read",
                            "ag1", "jsonl", None, 20, 6000, 0)
        db.insert_tool_call(con, "toolu_agent1", "p1", "sess-a", ts(1),
                            "Agent", None, "jsonl", None, 60, 900, 0)

        db.upsert_agent(con, "ag1", "sess-a", "p1", ts(1),
                        subagent_type="general-purpose", requested_model="haiku",
                        resolved_model=HAIKU, description="check the parser",
                        tool_use_id="toolu_agent1", source="jsonl")

        db.insert_event(con, "sess-a", ts(2), "model_switch", "p1",
                        detail=f"{OPUS}->{SONNET}")
        db.insert_event(con, "sess-b", ts(12), "compact", "p3")
        db.insert_event(con, "sess-a", ts(0), "context", "p1", value=940000)
        con.commit()

        self._write_transcript()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_transcript(self):
        entries = [
            {"type": "user", "promptId": "p1", "timestamp": ts(0),
             "sessionId": "sess-a",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": SCRIPT_TEXT}]}},
            {"type": "assistant", "promptId": "p1", "requestId": "r1",
             "timestamp": ts(0), "sessionId": "sess-a",
             "message": {"model": OPUS, "content": [
                 {"type": "thinking", "thinking": "considering options"},
                 {"type": "text", "text": "Reading the parser."},
                 {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                  "input": {"command": "ls -la"}},
                 {"type": "tool_use", "id": "toolu_agent1", "name": "Agent",
                  "input": {"subagent_type": "general-purpose",
                            "description": "check the parser"}}]}},
            {"type": "user", "promptId": "p1", "timestamp": ts(1),
             "sessionId": "sess-a",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_1",
                  "content": "total 8\n", "is_error": False},
                 {"type": "tool_result", "tool_use_id": "toolu_agent1",
                  "content": "agent said <b>hello</b>", "is_error": False}]}},
            {"type": "user", "promptId": "other", "timestamp": ts(5),
             "sessionId": "sess-a",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "a different prompt entirely"}]}},
        ]
        with open(self.transcript, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        sub_dir = os.path.join(self.projects, "sess-a", "subagents")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "agent-ag1.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps(
                {"type": "assistant", "requestId": "r7", "timestamp": ts(4),
                 "message": {"model": HAIKU, "content": [
                     {"type": "text", "text": "the parser looks fine"}]}}) + "\n")

    # -- helpers ----------------------------------------------------------
    def collect(self, **kw):
        rows, window = bd.collect(self.con, **kw)
        return {r["id"]: r for r in rows}, rows, window


class Rows(Fixture):
    def test_streamed_duplicate_keeps_the_complete_row(self):
        out = self.con.execute(
            "SELECT output_tokens FROM api_requests WHERE request_id='r1'"
        ).fetchone()[0]
        self.assertEqual(out, 200)

    def test_task_notification_folds_into_the_human_prompt(self):
        by_id, rows, _ = self.collect()
        self.assertIn("p1", by_id)
        self.assertNotIn("p2", by_id, "a folded prompt must not get its own row")
        r = by_id["p1"]
        # the subagent's request and its tool call landed on the parent
        self.assertEqual(r["api_calls"], 7)
        self.assertEqual(dict(r["tools"])["Read"], 1)
        self.assertEqual(r["agents"], ["ag1"])
        self.assertEqual(r["agent_info"],
                         [["ag1", "general-purpose", HAIKU]])

    def test_new_per_row_fields(self):
        r = self.collect()[0]["p1"]
        self.assertEqual(r["effort"], "high")
        self.assertEqual(r["thinking"], 90)
        self.assertEqual(r["fast_calls"], 1)
        self.assertEqual(r["errors"], 1)
        self.assertEqual(r["max_tokens_stops"], 1)
        self.assertEqual(r["web_searches"], 2)
        self.assertEqual(r["peak_ctx"], 12110)
        self.assertEqual(r["session"], "sess-a")

    def test_fast_mode_costs_more_than_standard(self):
        before = self.collect()[0]["p1"]["cost"]
        self.con.execute(
            "UPDATE api_requests SET speed='standard' WHERE request_id='r6'")
        after = self.collect()[0]["p1"]["cost"]
        self.assertGreater(before, after,
                           "a fast-mode request must not bill at list price")

    def test_tool_attribution_sums_to_the_input_side_cost(self):
        r = self.collect()[0]["p1"]
        by_name = {t[0]: t for t in r["tool_attrib"]}
        self.assertEqual(by_name["Bash"][1], 2)
        self.assertEqual(by_name["Bash"][2], 4000)
        self.assertEqual(by_name["Read"][2], 6000)
        input_cost = r["comp"][0] + r["comp"][1] + r["comp"][3]
        self.assertAlmostEqual(sum(t[3] for t in r["tool_attrib"]),
                               input_cost, places=4)
        # shared out by result bytes: 6000 of 10900 went to Read
        total = sum(t[2] for t in r["tool_attrib"])
        self.assertAlmostEqual(by_name["Read"][3],
                               input_cost * 6000 / total, places=4)


class CacheMisses(Fixture):
    def test_causes_are_named(self):
        self.collect()
        points = bd.EXTRAS["ctx"]["sess-a"]
        causes = [p[6] for p in points if p[5]]
        self.assertEqual(causes, ["model_switch", "idle_gap"])
        b = [p[6] for p in bd.EXTRAS["ctx"]["sess-b"] if p[5]]
        self.assertEqual(b, ["compact"])

    def test_a_hit_is_not_a_miss(self):
        self.collect()
        misses = [p[5] for p in bd.EXTRAS["ctx"]["sess-a"]]
        # r1 (no previous request), r2, r4, r6 are not misses
        self.assertEqual(misses, [0, 0, 1, 0, 1, 0])

    def test_miss_cost_is_the_cache_write_it_redid(self):
        by_id, _, _ = self.collect()
        rate = pricing.resolve(SONNET)
        expect = (11000 + 12000) * pricing.CACHE_WRITE_1H_MULT * rate.inp / 1e6
        self.assertAlmostEqual(by_id["p1"]["cache_miss_cost"], expect, places=6)
        self.assertEqual(by_id["p1"]["misses"], 2)

    def test_events_are_attached_to_the_series(self):
        self.collect()
        events = [p[7] for p in bd.EXTRAS["ctx"]["sess-b"]]
        self.assertEqual(events, [None, None, "compact"])

    def test_subagent_requests_stay_out_of_the_series(self):
        self.collect()
        self.assertEqual(len(bd.EXTRAS["ctx"]["sess-a"]), 6)


class Sessions(Fixture):
    def test_session_row_values(self):
        _, rows, _ = self.collect()
        by_id = {s["id"]: s for s in bd.session_rows(rows)}
        a = by_id["sess-a"]
        self.assertEqual(a["prompts"], 1)
        self.assertEqual(a["calls"], 7)
        self.assertEqual(a["project"], "proj")
        self.assertEqual(a["switches"], 1)
        self.assertEqual(a["compactions"], 0)
        self.assertEqual(a["misses"], 2)
        self.assertEqual(a["effort"], "high")
        self.assertEqual(a["subagent_ttl"], "5m")
        self.assertEqual(a["peak_ctx"], 12110)
        self.assertEqual(a["models"], sorted([OPUS, SONNET, HAIKU]))
        self.assertTrue(0 < a["hit"] < 1)
        self.assertTrue(a["first_prompt_text"].startswith("<script>"),
                        "the session row carries the text unescaped; "
                        "escaping is the renderer's job")
        b = by_id["sess-b"]
        self.assertEqual(b["compactions"], 1)
        self.assertEqual(b["switches"], 0)

    def test_redaction_blanks_the_session_preview(self):
        _, rows, _ = self.collect()
        by_id = {s["id"]: s for s in bd.session_rows(rows, redact=True)}
        self.assertEqual(by_id["sess-a"]["first_prompt_text"], "")

    def test_ctx_series_shape(self):
        _, rows, _ = self.collect()
        sess = bd.session_rows(rows)
        points, dropped = bd.ctx_points([s["id"] for s in sess])
        self.assertEqual(dropped, 0)
        self.assertEqual(len(points), 10)     # 6 in A, 3 in B, 1 in C
        self.assertEqual(len(points[0]), len(bd.CTX_COLUMNS))
        table = bd.compact_ctx(points, bd.Strings())
        self.assertEqual(table["n"], 10)
        self.assertEqual(sorted(table["cols"]), ["cr", "ctx", "cw", "t"])
        self.assertTrue(all(len(v) == 10 for v in table["cols"].values()))

    def test_ctx_encoding_round_trips(self):
        """The wire form is compressed; rehydrateCtx() must undo it exactly.

        Decoded here in Python by the same rules the template applies, so a
        change to one side without the other fails a test rather than a page.
        """
        _, rows, _ = self.collect()
        sess = bd.session_rows(rows)
        points, _ = bd.ctx_points([s["id"] for s in sess])
        strings = bd.Strings()
        table = bd.compact_ctx(points, strings)
        decoded = decode_ctx(table, strings.out)
        self.assertEqual(len(decoded), len(points))
        for got, want in zip(decoded, points):
            self.assertEqual(
                [got[k] for k in bd.CTX_COLUMNS],
                [want[0], want[1], want[2], want[3], want[4], want[5],
                 want[6], want[7], want[8]])

    def test_ctx_cap_drops_whole_sessions(self):
        _, rows, _ = self.collect()
        saved = bd.CTX_CAP
        try:
            bd.CTX_CAP = 5
            sess = bd.session_rows(rows)
            points, dropped = bd.ctx_points([s["id"] for s in sess])
            self.assertEqual(dropped, 1, "session A does not fit")
            self.assertEqual(len({p[0] for p in points}), 2)
        finally:
            bd.CTX_CAP = saved


class Blocks(Fixture):
    def test_five_hour_blocks(self):
        blocks, burn = bd.compute_blocks(
            self.con, now=T0 + timedelta(hours=2), days=365)
        self.assertEqual(len(blocks), 2, "last week's request opens its own block")
        recent = blocks[-1]
        self.assertEqual(recent["start"], T0.replace(minute=0))
        self.assertEqual(recent["end"], T0.replace(minute=0) + timedelta(hours=5))
        self.assertEqual(recent["calls"], 10)
        self.assertTrue(recent["active"])
        self.assertFalse(blocks[0]["active"])
        self.assertEqual(recent["models"], {OPUS, SONNET, HAIKU})
        self.assertIsNotNone(burn)
        self.assertEqual(burn["window_min"], bd.BURN_WINDOW_MIN)
        self.assertGreaterEqual(burn["projected_cost"],
                                round(recent["cost"], 4))
        self.assertEqual(burn["minutes_left"], 180.0)

    def test_a_closed_block_has_no_burn(self):
        _, burn = bd.compute_blocks(self.con, now=T0 + timedelta(days=2),
                                    days=365)
        self.assertIsNone(burn)


class Payload(Fixture):
    def _payload(self, **kw):
        out = os.path.join(self.tmp, "dashboard.html")
        saved = (bd.OUTPUT, report_index.OUTPUT)
        bd.OUTPUT = out
        report_index.OUTPUT = os.path.join(self.tmp, "index.html")
        try:
            bd.build(self.con, check_receiver=False, **kw)
        finally:
            bd.OUTPUT, report_index.OUTPUT = saved
        with open(out, encoding="utf-8") as f:
            html = f.read()
        raw = re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1)
        return json.loads(raw)

    def test_payload_parses_and_carries_the_new_tables(self):
        p = self._payload()
        for key in ("sessions", "ctx", "blocks", "errors", "baseline",
                    "overhead", "insights_report", "cost_basis", "notices",
                    "burn", "ctx_truncated"):
            self.assertIn(key, p)
        for key in ("cols", "strings", "n_rows", "total_rows", "truncated",
                    "redacted", "plan", "providers", "subscription", "window",
                    "unpriced", "generated_at"):
            self.assertIn(key, p, "an existing key went missing")
        self.assertEqual(sorted(p["sessions"]["cols"]),
                         sorted(bd.SESSION_COLUMNS))
        self.assertEqual(sorted(p["blocks"]["cols"]), sorted(bd.BLOCK_COLUMNS))
        self.assertEqual(p["baseline"]["usd_per_active_day"], 13)
        self.assertEqual(p["errors"]["tools"][0][0], "Bash")
        self.assertEqual(p["errors"]["tools"][0][2], 1)
        self.assertGreater(p["overhead"]["tokens"], 0)

    def test_every_column_has_one_value_per_row(self):
        p = self._payload()
        n = p["n_rows"]
        for name in bd.COLUMNS:
            col = p["cols"][name]
            if col == 0:               # the "nothing to carry" sentinel
                continue
            self.assertEqual(len(col), n, f"column {name} is the wrong length")

    def test_strings_are_shared_across_tables(self):
        p = self._payload()
        s = p["strings"]
        sess_ids = [s[i] for i in p["sessions"]["cols"]["id"]]
        self.assertIn("sess-a", sess_ids)
        ctx_sessions = {s[i] for _, i in p["ctx"]["runs"]["session"]}
        self.assertTrue(ctx_sessions <= set(sess_ids))

    def test_no_prompt_text_writes_no_pages(self):
        p = self._payload(redact=True, conversations_n=50)
        self.assertTrue(p["redacted"])
        self.assertEqual(p["conversations"], 0)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "conversations")))


class ConversationPages(Fixture):
    def _write(self, limit=10, redact=False):
        _, rows, _ = self.collect()
        return conversations.write_pages(self.con, self.tmp, rows,
                                         limit=limit, redact=redact), rows

    def test_page_is_written_and_linked(self):
        res, rows = self._write()
        self.assertEqual(res["count"], 1, "only sess-a has a transcript")
        self.assertEqual(res["skipped_missing"], 2)
        page = os.path.join(self.tmp, "conversations", "p1.html")
        self.assertTrue(os.path.exists(page))
        self.assertEqual({r["id"]: r["conv"] for r in rows}["p1"],
                         "conversations/p1.html")
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "conversations", "index.html")))

    def test_user_content_is_escaped(self):
        self._write()
        with open(os.path.join(self.tmp, "conversations", "p1.html"),
                  encoding="utf-8") as f:
            page = f.read()
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;",
                      page)
        self.assertNotIn("<script", page.lower(),
                         "no page this tool writes may contain a script tag")
        self.assertNotIn("<b>hello</b>", page)

    def test_the_conversation_is_actually_rendered(self):
        self._write()
        with open(os.path.join(self.tmp, "conversations", "p1.html"),
                  encoding="utf-8") as f:
            page = f.read()
        self.assertIn("Reading the parser.", page)
        self.assertIn("considering options", page)
        self.assertIn("command=ls -la", page)
        self.assertIn("subagent: general-purpose", page)
        self.assertIn(HAIKU, page)
        self.assertIn("the parser looks fine", page)   # the subagent's file
        self.assertNotIn("a different prompt entirely", page,
                         "another prompt's turns must not leak in")

    def test_rerun_skips_an_unchanged_transcript(self):
        self._write()
        again, _ = self._write()
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["count"], 1)
        os.utime(self.transcript, None)
        third, _ = self._write()
        self.assertEqual(third["written"], 1)

    def test_a_missing_transcript_is_counted_not_raised(self):
        os.remove(self.transcript)
        res, _ = self._write()
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["skipped_missing"], 3)

    def test_pages_are_capped(self):
        page = conversations.Page(budget=200)
        self.assertTrue(page.add("x" * 100))
        self.assertFalse(page.add("y" * 200))
        self.assertIn("longer than one page", page.html())


class Digest(Fixture):
    def _run(self, now):
        reports = os.path.join(self.tmp, "reports")
        os.makedirs(reports, exist_ok=True)
        saved = (digest.REPORTS, report_index.OUTPUT, report_index.REPORTS)
        digest.REPORTS = reports
        report_index.OUTPUT = os.path.join(self.tmp, "index.html")
        report_index.REPORTS = reports
        try:
            res = digest.build_digest(now=now, db_path=self.dbpath)
        finally:
            digest.REPORTS, report_index.OUTPUT, report_index.REPORTS = saved
        with open(res["digest"], encoding="utf-8") as f:
            return res, f.read()

    def test_week_over_week(self):
        res, page = self._run(datetime(2026, 5, 14, 3, 0, tzinfo=UTC))
        self.assertEqual(res["prompts"], 2, "this week's prompts only")
        self.assertIn("on last week", page)
        self.assertIn("Cache hit", page)
        self.assertIn("By session", page)
        self.assertIn("tool calls failed", page)
        self.assertIn("output-token ceiling", page)
        self.assertIn("estimated from public list prices", page)
        self.assertIsNotNone(res["cost_delta"])
        self.assertEqual(res["cache_misses"], 3)
        self.assertGreater(res["cost"], 0)

    def test_prompt_text_is_escaped_in_the_digest(self):
        _, page = self._run(datetime(2026, 5, 14, 3, 0, tzinfo=UTC))
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script", page.lower())

    def test_existing_digests_are_never_overwritten(self):
        now = datetime(2026, 5, 14, 3, 0, tzinfo=UTC)
        first, _ = self._run(now)
        second, _ = self._run(now)
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertTrue(os.path.exists(first["digest"]))

    def test_a_week_with_no_history_says_new(self):
        # A window whose predecessor is empty: no percentage is meaningful.
        self.assertEqual(digest.delta(5, 0)[0], "new")
        self.assertEqual(digest.delta(0, 0)[0], "-")
        self.assertEqual(digest.delta(10, 10)[0], "no change")
        self.assertTrue(digest.delta(15, 10)[0].startswith("+50%"))


class SinceBound(Fixture):
    def test_since_excludes_older_prompts(self):
        cutoff = (T0 - timedelta(days=1)).isoformat()
        by_id, _, _ = self.collect(since=cutoff)
        self.assertIn("p1", by_id)
        self.assertNotIn("p0", by_id)
        all_ids, _, _ = self.collect()
        self.assertIn("p0", all_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
