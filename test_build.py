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

# The Wednesday of the week after T0's (T0 is a Tuesday): the last complete ISO
# week is the one T0 falls in, Monday 2026-05-11 to Monday 2026-05-18.
WED = datetime(2026, 5, 20, 3, 0, tzinfo=UTC)

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
                    "miss": 0, "cause": None, "event": None, "turn": 0,
                    "tools": cols["tools"][i], "terr": 0})
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
    for i in table["sparse"]["turn"]:
        out[i]["turn"] = 1
    for i, n in table["sparse"]["terr"]:
        out[i]["terr"] = n
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
                          first_ts=ts(0, days=-6), last_ts=ts(0, days=-6))

        db.upsert_prompt(con, "p1", "sess-a", "proj", ts(0), SCRIPT_TEXT,
                         "jsonl", 0, None, "human")
        # A background agent reporting back: its own prompt, folded onto p1.
        db.upsert_prompt(con, "p2", "sess-a", "proj", ts(1),
                         "<task-notification>done</task-notification>",
                         "jsonl", 1, "p1", "task-notification")
        db.upsert_prompt(con, "p3", "sess-b", "proj2", ts(10),
                         "second session", "jsonl", 0, None, "human")
        # The ISO week before p1's (a Wednesday, T0 being a Tuesday), so the
        # digest has something to compare against.
        db.upsert_prompt(con, "p0", "sess-c", "proj3", ts(0, days=-6),
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
            request("z1", "p0", "sess-c", ts(0, days=-6), OPUS,
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
        # Skill calls carry the skill's own name in `detail`. Zero result
        # bytes so these rows do not shift the tool-cost attribution the
        # tests above pin to the byte counts they already have.
        db.insert_tool_call(con, "toolu_skill1", "p1", "sess-a", ts(1),
                            "Skill", None, "jsonl", "house-style", 10, 0, 0)
        db.insert_tool_call(con, "toolu_skill2", "p1", "sess-a", ts(1),
                            "Skill", None, "jsonl", "house-style", 10, 0, 0)
        # inside the subagent, on the prompt that folds into p1
        db.insert_tool_call(con, "toolu_skill3", "p2", "sess-a", ts(2),
                            "Skill", "ag1", "jsonl", "dataviz", 10, 0, 0)
        # An OTel row that arrived before the transcript naming it: the tool
        # is Skill, the skill is unknown.
        db.insert_tool_call(con, "toolu_skill4", "p1", "sess-a", ts(1),
                            "Skill", None, "otel")

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


class MainQuerySource(unittest.TestCase):
    """The main conversation answers to two names.

    A transcript labels its requests "main"; the CLI's own telemetry labels
    the same requests "repl_main_thread", and an OTel row wins every conflict,
    so on a machine running the receiver almost every main request ends up
    with the telemetry label. Filtering the context series on "main" alone
    dropped them - one live session in the author's database kept 30 of 344
    requests, and the curve drawn from the survivors jumped days at a time.
    """

    SIDE = ("subagent", "away_summary", "prompt_suggestion", "sdk",
            "generate_session_title", "agent:builtin:general-purpose")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-qs-")
        self.con = db.connect(os.path.join(self.tmp, "metrics.db"))
        db.upsert_session(self.con, "s1", project="p", cwd="/w/p",
                          first_ts=ts(0), last_ts=ts(9))
        db.upsert_prompt(self.con, "q1", "s1", "p", ts(0), "hello", "jsonl",
                         0, None, "human")
        rows = [request("m1", "q1", "s1", ts(0), OPUS, output_tokens=10,
                        cache_create_tokens=1000, cache_1h_tokens=1000),
                request("m2", "q1", "s1", ts(1), OPUS, output_tokens=10,
                        cache_read_tokens=1000,
                        query_source="repl_main_thread")]
        for i, qs in enumerate(self.SIDE):
            rows.append(request("x%d" % i, "q1", "s1", ts(2 + i), OPUS,
                                output_tokens=10, cache_read_tokens=1000,
                                query_source=qs,
                                agent_name="ag" if "agent" in qs else None))
        db.insert_requests_jsonl(self.con, rows)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_telemetry_named_main_requests_are_in_the_series(self):
        _, _, ctx = bd.cache_scan(self.con)
        self.assertEqual(len(ctx["s1"]), 2,
                         "repl_main_thread is the main conversation")

    def test_side_threads_stay_out(self):
        """Each runs against its own context; mixing them in draws a sawtooth."""
        _, _, ctx = bd.cache_scan(self.con)
        self.assertEqual(len(ctx["s1"]), len(bd.MAIN_QUERY_SOURCES),
                         "one point per main-thread name, none per side thread")
        self.assertNotIn("repl_main_thread", self.SIDE)


class PeakContextIsTheMainThreads(unittest.TestCase):
    """A subagent's own context window must never read as the main thread's.

    collect()'s per-prompt peak_ctx uses a `MAX(CASE WHEN <main query source>
    ...)` in SQL for exactly this reason (cache_scan() does the same for the
    session series) - but the fixture every other test in this file shares
    never gives the subagent a context bigger than the main thread's, so the
    CASE could be deleted and nothing here would notice.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-peakctx-")
        self.con = db.connect(os.path.join(self.tmp, "metrics.db"))
        db.upsert_session(self.con, "s1", project="p", cwd="/w/p",
                          first_ts=ts(0), last_ts=ts(0))
        db.upsert_prompt(self.con, "q1", "s1", "p", ts(0), "hello", "jsonl",
                         0, None, "human")
        rows = [
            request("m1", "q1", "s1", ts(0), OPUS, output_tokens=10,
                    input_tokens=500, query_source="main"),
            # A subagent's own window, far larger than the main thread's.
            request("s1r", "q1", "s1", ts(0), HAIKU, output_tokens=10,
                    input_tokens=90000, query_source="subagent",
                    agent_name="ag1"),
        ]
        db.insert_requests_jsonl(self.con, rows)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_subagent_context_never_wins_over_the_main_threads(self):
        rows, _ = bd.collect(self.con)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["q1"]["peak_ctx"], 500,
                         "the subagent's 90000-token window leaked onto a "
                         "figure that should only ever be the main thread's")


class LongPromptsAreBilledOneByOne(unittest.TestCase):
    """Claude Haiku 5.5 is 5x over a 100K-token prompt - per request.

    Every cost path that sums requests before pricing them has to keep the
    long ones apart: two 50K prompts are not a 100K one, and a 120K prompt
    summed with small ones must still bill at the long rate.
    """

    H55 = "claude-haiku-5-5"
    MAIN = (2 * (50000 * 0.1 + 1000 * 0.5)               # two short
            + (10 * 0.5 + 120000 * 0.5 * 0.1 + 1000 * 2.5)) / 1e6  # one long
    AGENT = (150000 * 0.5 + 1000 * 0.1) / 1e6            # one long, one short

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-longprompt-")
        self.con = db.connect(os.path.join(self.tmp, "metrics.db"))
        db.upsert_session(self.con, "s1", project="p", cwd="/w/p",
                          first_ts=ts(0), last_ts=ts(3))
        db.upsert_prompt(self.con, "q1", "s1", "p", ts(0), "hello", "jsonl",
                         0, None, "human")
        db.insert_requests_jsonl(self.con, [
            request("a", "q1", "s1", ts(0), self.H55, input_tokens=50000,
                    output_tokens=1000),
            request("b", "q1", "s1", ts(1), self.H55, input_tokens=50000,
                    output_tokens=1000),
            request("c", "q1", "s1", ts(2), self.H55, input_tokens=10,
                    cache_read_tokens=120000, output_tokens=1000),
            request("d", "q1", "s1", ts(3), self.H55, input_tokens=150000,
                    query_source="subagent", agent_name="ag1"),
            request("e", "q1", "s1", ts(3), self.H55, input_tokens=1000,
                    query_source="subagent", agent_name="ag1"),
        ])
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_row_and_its_breakdown(self):
        rows, _ = bd.collect(self.con)
        r = {x["id"]: x for x in rows}["q1"]
        self.assertAlmostEqual(r["cost"], self.MAIN + self.AGENT, places=4)
        self.assertAlmostEqual(sum(r["comp"]), r["cost"], places=4)

    def test_agent_span(self):
        spans = bd.agent_spans(self.con)["s1"]
        self.assertAlmostEqual(sum(sp["cost"] for sp in spans), self.AGENT,
                               places=9)

    def test_five_hour_block(self):
        blocks, _ = bd.compute_blocks(self.con, now=T0 + timedelta(hours=1),
                                      days=365)
        self.assertAlmostEqual(blocks[-1]["cost"], self.MAIN + self.AGENT,
                               places=9)

    def test_conversation_page_costs(self):
        got = conversations._costs(self.con, ["q1"])
        self.assertAlmostEqual(sum(c for c, _, _ in got.values()),
                               self.MAIN + self.AGENT, places=9)


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


class Skills(Fixture):
    """Skill names are a field of their own, not a label to be parsed back
    out of the tool list. The question the page has to answer is which
    skills ran on a prompt - and, by their absence, which did not."""

    def test_skills_are_named_and_counted(self):
        r = self.collect()[0]["p1"]
        self.assertEqual(dict(r["skills"])["house-style"], 2)

    def test_a_subagents_skill_lands_on_the_parent_prompt(self):
        # p2 folds into p1, and its subagent's skill has to fold with it or
        # a delegated skill would simply vanish from the page.
        r = self.collect()[0]["p1"]
        self.assertEqual(dict(r["skills"])["dataviz"], 1)

    def test_an_unnamed_skill_call_is_counted_not_dropped(self):
        """A skill ran and we cannot yet say which. That is a different
        statement from no skill having run, so it keeps a row under '?'."""
        r = self.collect()[0]["p1"]
        self.assertEqual(dict(r["skills"])["?"], 1)

    def test_skills_are_ordered_by_use(self):
        r = self.collect()[0]["p1"]
        self.assertEqual([s[0] for s in r["skills"]][0], "house-style")

    def test_a_prompt_that_ran_none_says_so_with_an_empty_list(self):
        r = self.collect()[0]["p3"]
        self.assertEqual(r["skills"], [])

    def test_the_skill_stays_in_the_tool_list_with_its_cost_share(self):
        """The two views are the same fact: `skills` answers which ran,
        `tools` keeps the cost attribution under a Skill:<name> label."""
        r = self.collect()[0]["p1"]
        self.assertEqual(dict(r["tools"])["Skill:house-style"], 2)
        self.assertEqual(dict(r["tools"])["Skill"], 1)

    def test_skills_survive_the_payload_encoding(self):
        rows = self.collect()[1]
        cols, strings = bd.compact(rows)
        self.assertIn("skills", bd.COLUMNS)
        i = [r["id"] for r in rows].index("p1")
        got = {strings[s[0]]: s[1] for s in cols["skills"][i]}
        self.assertEqual(got["house-style"], 2)
        self.assertEqual(got["dataviz"], 1)


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


class CacheMissEdgeCases(unittest.TestCase):
    """cache_scan() correctness that the Fixture scenario doesn't exercise:
    folding a miss on an injected turn onto its canonical prompt, and not
    mistaking a NULL effort/speed (or "normal" vs "standard") for a switch.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        db.upsert_session(self.con, "sess-x", project="proj", cwd="/work/proj",
                          first_ts=ts(0), last_ts=ts(5))

    def tearDown(self):
        self.con.close()

    def _requests(self, rows):
        db.insert_requests_jsonl(self.con, rows)
        self.con.commit()

    def test_misses_on_an_injected_turn_land_on_the_canonical_row(self):
        con = self.con
        db.upsert_prompt(con, "human1", "sess-x", "proj", ts(0),
                         "do the thing", "jsonl", 0, None, "human")
        db.upsert_prompt(con, "inject1", "sess-x", "proj", ts(1),
                         "<task-notification>done</task-notification>",
                         "jsonl", 1, "human1", "task-notification")
        self._requests([
            request("w1", "human1", "sess-x", ts(0), OPUS, input_tokens=10,
                    output_tokens=10, cache_read_tokens=5000),
            # The miss lands on a request keyed by the *injected* prompt id.
            request("w2", "inject1", "sess-x", ts(1), OPUS, input_tokens=10,
                    output_tokens=10, cache_create_tokens=6000,
                    cache_1h_tokens=6000),
        ])
        canon = bd.resolve_map(con)
        per_prompt, _, _ = bd.cache_scan(con, canon=canon)
        self.assertNotIn("inject1", per_prompt,
                         "a miss on a folded prompt must not sit under its "
                         "own raw id")
        self.assertIn("human1", per_prompt)
        self.assertEqual(per_prompt["human1"][0], 1)
        self.assertGreater(per_prompt["human1"][1], 0)

    def test_two_raw_ids_folding_to_one_canonical_accumulate(self):
        con = self.con
        db.upsert_prompt(con, "human2", "sess-x", "proj", ts(0),
                         "do the thing", "jsonl", 0, None, "human")
        db.upsert_prompt(con, "inject2a", "sess-x", "proj", ts(1),
                         "<task-notification>a</task-notification>",
                         "jsonl", 1, "human2", "task-notification")
        db.upsert_prompt(con, "inject2b", "sess-x", "proj", ts(2),
                         "<task-notification>b</task-notification>",
                         "jsonl", 1, "human2", "task-notification")
        self._requests([
            request("v1", "human2", "sess-x", ts(0), OPUS, input_tokens=10,
                    output_tokens=10, cache_read_tokens=5000),
            request("v2", "inject2a", "sess-x", ts(1), OPUS, input_tokens=10,
                    output_tokens=10, cache_create_tokens=6000,
                    cache_1h_tokens=6000),
            # A hit that re-warms the cache before the second injected miss.
            request("v3", "inject2b", "sess-x", ts(2), OPUS,
                    input_tokens=10, output_tokens=10,
                    cache_read_tokens=6000),
            request("v4", "human2", "sess-x", ts(3), OPUS, input_tokens=10,
                    output_tokens=10, cache_create_tokens=7000,
                    cache_1h_tokens=7000),
        ])
        canon = bd.resolve_map(con)
        per_prompt, _, _ = bd.cache_scan(con, canon=canon)
        self.assertEqual(per_prompt["human2"][0], 2,
                         "both folded misses must accumulate, not overwrite")

    def test_effort_switch_requires_both_sides_known(self):
        con = self.con
        db.upsert_prompt(con, "human3", "sess-x", "proj", ts(0),
                         "p", "jsonl", 0, None, "human")
        self._requests([
            request("e1", "human3", "sess-x", ts(0), OPUS, input_tokens=10,
                    output_tokens=10, cache_read_tokens=5000, effort="high"),
            request("e2", "human3", "sess-x", ts(1), OPUS, input_tokens=10,
                    output_tokens=10, cache_create_tokens=6000,
                    cache_1h_tokens=6000, effort=None),
        ])
        _, _, ctx = bd.cache_scan(con, canon=bd.resolve_map(con))
        points = ctx["sess-x"]
        self.assertEqual(points[-1][5], 1, "still a genuine cache miss")
        self.assertEqual(points[-1][6], "unknown",
                         "a NULL effort must not be read as effort_switch")

    def test_speed_normal_and_standard_compare_equal(self):
        con = self.con
        db.upsert_prompt(con, "human4", "sess-x", "proj", ts(0),
                         "p", "jsonl", 0, None, "human")
        self._requests([
            request("s1", "human4", "sess-x", ts(0), OPUS, input_tokens=10,
                    output_tokens=10, cache_read_tokens=5000, speed="normal"),
            request("s2", "human4", "sess-x", ts(1), OPUS, input_tokens=10,
                    output_tokens=10, cache_create_tokens=6000,
                    cache_1h_tokens=6000, speed="standard"),
        ])
        _, _, ctx = bd.cache_scan(con, canon=bd.resolve_map(con))
        points = ctx["sess-x"]
        self.assertEqual(points[-1][5], 1)
        self.assertEqual(points[-1][6], "unknown",
                         "'normal' and 'standard' must not read as a switch")


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
        self.assertEqual(sorted(table["cols"]),
                         ["cr", "ctx", "cw", "t", "tools"])
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
            self.assertEqual([got[k] for k in bd.CTX_COLUMNS], list(want))

    def test_tool_calls_land_on_the_request_that_carried_their_results(self):
        """Between the previous request and this one, main thread only.

        The fixture's session A calls Bash at ts(0), and at ts(1) a Bash, an
        Agent tool and three Skill calls, all on the main thread; inside the
        subagent it calls Read and a Skill at ts(2), and those belong to the
        subagent's own bar, not to the turn that launched it. Requests are at
        ts 0, 1, 2, 3, 75 and 76.
        """
        _, rows, _ = self.collect()
        sess = bd.session_rows(rows)
        points, _ = bd.ctx_points([s["id"] for s in sess])
        ti = bd.CTX_COLUMNS.index("tools")
        got = [p[ti] for p in points if p[0] == "sess-a"]
        # ts(0): the Bash at ts(0). ts(1): the Bash, the Agent and the three
        # Skill calls at ts(1). Nothing after that on the main thread.
        self.assertEqual(got, [1, 5, 0, 0, 0, 0])
        self.assertEqual(sum(p[ti] for p in points), 6,
                         "the subagent's own Read and Skill are not the "
                         "session's")

    def test_failed_tool_calls_are_counted_separately(self):
        _, rows, _ = self.collect()
        sess = bd.session_rows(rows)
        points, _ = bd.ctx_points([s["id"] for s in sess])
        te = bd.CTX_COLUMNS.index("terr")
        # toolu_2, the failing Bash, is at ts(1) and lands on that request.
        self.assertEqual([p[te] for p in points if p[0] == "sess-a"],
                         [0, 1, 0, 0, 0, 0])

    def test_turn_flags_mark_the_first_request_of_each_prompt(self):
        """One flag per prompt, on its first request, folded turns included.

        Session A is six main requests across one canonical prompt (the
        task-notification folds onto it), so exactly the first of them is a
        turn - not one per request, and not a second one for the injected
        prompt.
        """
        _, rows, _ = self.collect()
        sess = bd.session_rows(rows)
        points, _ = bd.ctx_points([s["id"] for s in sess])
        turn = bd.CTX_COLUMNS.index("turn")
        by_sess = {}
        for p in points:
            by_sess.setdefault(p[0], []).append(p[turn])
        self.assertEqual(by_sess["sess-a"], [1, 0, 0, 0, 0, 0])
        self.assertEqual(by_sess["sess-b"], [1, 0, 0])
        self.assertEqual(by_sess["sess-c"], [1])

    def test_agent_spans_run_from_launch_to_last_request(self):
        """The launch is in `agents`; the end is only in the requests."""
        bd.collect(self.con)
        spans = bd.EXTRAS["agent_spans"]
        self.assertEqual(list(spans), ["sess-a"])
        (sp,) = spans["sess-a"]
        self.assertEqual(sp["type"], "general-purpose")
        self.assertEqual(sp["desc"], "check the parser")
        self.assertEqual(sp["model"], HAIKU)
        self.assertEqual(sp["calls"], 1)
        self.assertEqual(sp["out"], 250)
        # ts(1) is the agents row; the only request is at ts(4).
        self.assertEqual(sp["t0"], int(bd.parse_ts(ts(1)).timestamp()))
        self.assertEqual(sp["t1"], int(bd.parse_ts(ts(4)).timestamp()))
        # cost_usd is NULL on every jsonl subagent request, so a span that
        # summed the column would report the run as free.
        self.assertGreater(sp["cost"], 0)
        self.assertEqual(sp["est"], 1)

    def test_agent_spans_since_excludes_a_launch_before_the_cutoff(self):
        """digest.py calls agent_spans(since=...) directly (not through
        collect()); the agents-table read inside it applies its own `since`
        clause, separate from the one on api_requests. Miss either half and
        an agent launched - and used - entirely before the cutoff still shows
        up as a request-less, zero-length span dragged into the window.

        ag1 launches at ts(1) and its only request is at ts(4); a cutoff
        after both must drop it from sess-a's spans entirely.
        """
        cutoff = ts(5)
        spans = bd.agent_spans(self.con, since=cutoff)
        self.assertNotIn("sess-a", spans)

    def test_ctx_cap_drops_whole_sessions(self):
        _, rows, _ = self.collect()
        saved = bd.CTX_CAP
        try:
            bd.CTX_CAP = 5
            sess = bd.session_rows(rows)
            points, dropped = bd.ctx_points([s["id"] for s in sess])
            # Newest first: B (3 points) fits, A does not - and C, which
            # would fit behind it, is older than A and goes too. The notice
            # says "the oldest sessions were left out", so what is kept has to
            # be the newest run, not whatever happens to be small enough.
            self.assertEqual(dropped, 2, "session A does not fit, nor C after it")
            self.assertEqual({p[0] for p in points}, {"sess-b"})
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

    def test_burn_rate_scales_with_the_blocks_own_age_not_a_fixed_30_min(self):
        """A block only a few minutes old must not be diluted by dividing
        over the full 30-minute window, and must not absorb a request that
        actually belongs to the block before it just because that request
        falls inside the last 30 wall-clock minutes."""
        old_block_ts = ts(295)   # still inside the block that opened at T0
        new_block_ts = ts(302)   # T0+302min floors to a fresh block at T0+5h
        db.insert_requests_jsonl(self.con, [
            request("burn-old", "p1", "sess-a", old_block_ts, OPUS,
                    output_tokens=1000),
            request("burn-new", "p1", "sess-a", new_block_ts, OPUS,
                    output_tokens=100),
        ])
        self.con.commit()
        now = T0 + timedelta(minutes=304)
        blocks, burn = bd.compute_blocks(self.con, now=now, days=365)
        self.assertTrue(blocks[-1]["active"])
        self.assertEqual(blocks[-1]["calls"], 1,
                         "only the new block's own request belongs to it")
        rate = pricing.resolve(OPUS)
        expect_cost = 100 * rate.out / 1e6      # burn-new alone
        divisor = 4.0                            # now is 4 min into this block
        self.assertAlmostEqual(burn["usd_per_min"], expect_cost / divisor,
                               places=6)
        # The pre-fix behaviour divided by the fixed 30-minute window and
        # summed both requests regardless of which block they belonged to.
        buggy = (expect_cost + 1000 * rate.out / 1e6) / bd.BURN_WINDOW_MIN
        self.assertNotAlmostEqual(burn["usd_per_min"], buggy, places=6)


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
                    "burn", "ctx_truncated", "agent_spans"):
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

    def test_agent_spans_reach_the_payload_and_redact(self):
        """Descriptions are prompt text, so --no-prompt-text drops them."""
        p = self._payload()
        table = p["agent_spans"]
        self.assertEqual(table["n"], 1)
        self.assertEqual(sorted(table["cols"]),
                         sorted(bd.AGENT_SPAN_COLUMNS))
        S = p["strings"]
        self.assertEqual(S[table["cols"]["desc"][0]], "check the parser")
        self.assertEqual(S[table["cols"]["session"][0]], "sess-a")
        red = self._payload(redact=True)
        self.assertIsNone(red["agent_spans"]["cols"]["desc"][0])

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


class PayloadEmbedding(Fixture):
    """A prompt that mentions a script tag must not close the real one.

    The HTML tokenizer finds `</script` before any JavaScript runs, so the
    raw sequence ended the page's only script element: the rest of the
    payload rendered as text, DATA was never assigned, the table drew nothing
    and whatever followed was parsed as markup and executed. SCRIPT_TEXT is
    the fixture's p1 prompt, so every build here carries the hazard.
    """

    def test_embed_json_escapes_the_markup_characters(self):
        out = bd.embed_json({"t": "a </script><b> & c"})
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)
        self.assertNotIn("&", out)
        # and it still parses back to exactly the same string
        self.assertEqual(json.loads(out)["t"], "a </script><b> & c")

    def test_embed_json_leaves_the_structure_intact(self):
        payload = {"a": [1, 2, None], "b": {"c": True}, "d": "x"}
        self.assertEqual(json.loads(bd.embed_json(payload)), payload)

    def test_built_page_has_exactly_one_script_element(self):
        out = os.path.join(self.tmp, "dashboard.html")
        saved = (bd.OUTPUT, report_index.OUTPUT)
        bd.OUTPUT = out
        report_index.OUTPUT = os.path.join(self.tmp, "index.html")
        try:
            bd.build(self.con, check_receiver=False, conversations_n=0)
        finally:
            bd.OUTPUT, report_index.OUTPUT = saved
        with open(out, encoding="utf-8") as f:
            html = f.read()
        self.assertEqual(html.lower().count("<script"), 1)
        self.assertEqual(html.lower().count("</script"), 1)

    def test_the_prompt_text_still_reaches_the_payload(self):
        """Escaped, not dropped - the row must still say what was typed."""
        p = Payload._payload(self)
        self.assertIn(SCRIPT_TEXT, p["cols"]["text"])


class ConversationPages(Fixture):
    def _write(self, limit=10, redact=False):
        _, rows, _ = self.collect()
        return conversations.write_pages(self.con, self.tmp, rows,
                                         limit=limit, redact=redact), rows

    def test_the_cap_is_counted_so_the_page_can_say_so(self):
        """A row past the limit had no link and the page never said why.

        Unlinked reads as "transcript gone", which is a different and worse
        thing than "outside the configured window".
        """
        res, rows = self._write(limit=1)
        self.assertEqual(res["capped"], 2)
        res, _ = self._write(limit=10)
        self.assertEqual(res["capped"], 0, "nothing was cut, so say nothing")

    def test_the_cap_is_reported_in_the_notice_bar(self):
        p = Payload._payload(self, conversations_n=1)
        joined = " ".join(p["notices"])
        self.assertIn("Conversation pages cover the newest", joined)
        self.assertIn("--conversations", joined)

    def test_telemetry_redacted_prompt_text_is_named_in_the_notice_bar(self):
        """"<REDACTED>" from OTel must not pass for --no-prompt-text."""
        self.con.execute("UPDATE prompts SET text = ? WHERE prompt_id = 'p1'",
                         (db.REDACTED_PROMPT,))
        self.con.commit()
        joined = " ".join(Payload._payload(self)["notices"])
        self.assertIn("1 prompt(s) show", joined)
        self.assertIn("OTEL_LOG_USER_PROMPTS", joined)

    def test_no_cap_notice_when_nothing_was_cut(self):
        p = Payload._payload(self, conversations_n=500)
        self.assertNotIn("Conversation pages cover",
                         " ".join(p["notices"]))

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

    def test_path_traversal_id_cannot_escape_the_conversations_folder(self):
        _, rows, _ = self.collect()
        base = next(r for r in rows if r["id"] == "p1")
        evil = dict(base)
        evil["id"] = "../../evil"
        res = conversations.write_pages(self.con, self.tmp, [evil], limit=10)
        self.assertEqual(res["count"], 1)
        outside = os.path.normpath(
            os.path.join(self.tmp, os.pardir, "evil.html"))
        self.assertFalse(
            os.path.exists(outside),
            "a crafted id must never write outside conversations/")
        fname = conversations.safe_filename("../../evil")
        self.assertTrue(fname.endswith(".html"))
        self.assertNotIn("/", fname)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "conversations", fname)))
        self.assertEqual(evil["conv"], f"conversations/{fname}")

    def test_stale_pages_are_pruned_after_a_rebuild(self):
        self._write(limit=10)
        stale = os.path.join(self.tmp, "conversations", "stale-prompt.html")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("<html>old</html>")
        # Not a page this module would ever write - must survive pruning.
        keep_file = os.path.join(self.tmp, "conversations", "notes.txt")
        with open(keep_file, "w", encoding="utf-8") as f:
            f.write("not a conversation page")
        self._write(limit=10)
        self.assertFalse(
            os.path.exists(stale),
            "a page outside the current window must be removed")
        self.assertTrue(os.path.exists(keep_file),
                        "only conversation-page filenames may be pruned")
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "conversations", "index.html")))


class SafeFilenames(unittest.TestCase):
    def test_normal_id_keeps_its_own_name(self):
        self.assertEqual(conversations.safe_filename("p1"), "p1.html")

    def test_traversal_id_is_hashed_not_joined_raw(self):
        fname = conversations.safe_filename("../../pwned")
        self.assertNotIn("/", fname)
        self.assertNotIn("..", fname)
        self.assertRegex(fname, r"^[0-9a-f]{24}\.html$")

    def test_hashing_is_deterministic(self):
        self.assertEqual(conversations.safe_filename("../x"),
                         conversations.safe_filename("../x"))


class InsightsReportLink(unittest.TestCase):
    def test_emits_a_file_url_not_a_raw_path(self):
        tmp = tempfile.mkdtemp(prefix="claude-lens-insights-")
        try:
            udir = os.path.join(tmp, "usage-data")
            os.makedirs(udir)
            report = os.path.join(udir, "report.html")
            with open(report, "w", encoding="utf-8") as f:
                f.write("<html></html>")
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = tmp
            try:
                href = bd.insights_report()
            finally:
                if old is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old
            self.assertIsNotNone(href)
            self.assertTrue(href.startswith("file:"))
            # Same encoding report_index.py uses for the same purpose.
            self.assertEqual(href, report_index._file_url(report))
            self.assertNotEqual(href, report,
                               "a raw filesystem path is not a usable href")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UnpricedCounts(unittest.TestCase):
    def setUp(self):
        bd.UNPRICED.clear()

    def tearDown(self):
        bd.UNPRICED.clear()

    def test_note_unpriced_counts_requests_not_calls_to_it(self):
        """collect() calls note_unpriced() once per GROUP BY group, passing
        that group's request count - a single call standing for many rows
        must not be counted as one row."""
        bd.note_unpriced("mystery-model", tokens=1000, uncosted=True, calls=7)
        bd.note_unpriced("mystery-model", tokens=500, uncosted=False, calls=3)
        e = bd.UNPRICED["mystery-model"]
        self.assertEqual(e["rows"], 10)
        self.assertEqual(e["uncosted_rows"], 7)
        self.assertEqual(e["tokens"], 1500)

    def test_default_calls_is_one(self):
        bd.note_unpriced("solo-model", tokens=10, uncosted=True)
        self.assertEqual(bd.UNPRICED["solo-model"]["rows"], 1)

    def test_unpriced_models_are_all_returned(self):
        """The payload's array is the notice bar's count AND its list.

        Slicing it made the page claim six models when there were more, and
        leave the rest unnamed anywhere - against the rule that an unpriced
        model is named on the page rather than silently costed at $0.00.
        """
        for i in range(9):
            bd.note_unpriced("mystery-%d" % i, tokens=100 * (i + 1),
                             uncosted=True)
        self.assertEqual(len(bd.unpriced_models()), 9)


class UnpricedPayload(Fixture):
    def test_every_unpriced_model_reaches_the_payload(self):
        """collect() rebuilds UNPRICED, so the models have to be real rows."""
        for i in range(9):
            db.upsert_request(self.con, request(
                "unp-%d" % i, "p1", "sess-a", ts(2), "mystery-model-%d" % i,
                input_tokens=100 * (i + 1), output_tokens=10), "jsonl")
        self.con.commit()
        p = Payload._payload(self)
        names = [u["model"] for u in p["unpriced"]]
        self.assertEqual(len(names), 9, "the list was sliced again")
        self.assertEqual(names[0], "mystery-model-8", "dearest first")
        for i in range(9):
            self.assertIn("mystery-model-%d" % i, names)


class RedactedPayload(Fixture):
    """--no-prompt-text makes the file safe to hand to someone else."""

    def test_insights_link_is_withheld(self):
        """It is a file: URL under the home directory, so it names the user."""
        p = Payload._payload(self, redact=True)
        self.assertIsNone(p["insights_report"])

    def test_insights_link_is_present_without_redaction(self):
        p = Payload._payload(self)
        # None only when the CLI has written no report on this machine; the
        # point is that redaction is what decides it, not chance.
        self.assertEqual(p["insights_report"], bd.insights_report())


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
        res, page = self._run(WED)
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
        _, page = self._run(WED)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script", page.lower())

    def test_existing_digests_are_never_overwritten(self):
        first, _ = self._run(WED)
        second, _ = self._run(WED)
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


class RedactionSweep(Fixture):
    """--no-prompt-text has to withhold what an earlier build already wrote.

    Pages from an unredacted build, the titles Claude Desktop gives a session
    (a summary of the conversation) and the links on index.html all say what
    was typed; clearing the prompt text alone left every one of them behind.
    """
    TITLE = "Secret plan to gut the billing module"

    def setUp(self):
        super().setUp()
        db.set_session_title(self.con, "sess-a", self.TITLE)
        self.con.commit()
        self.cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(os.path.join(self.cfg, "usage-data"))
        with open(os.path.join(self.cfg, "usage-data", "report.html"), "w",
                  encoding="utf-8") as f:
            f.write("<html></html>")
        self._saved_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        self._saved = (bd.OUTPUT, report_index.OUTPUT, report_index.BASE,
                       report_index.REPORTS)
        bd.OUTPUT = os.path.join(self.tmp, "dashboard.html")
        report_index.OUTPUT = os.path.join(self.tmp, "index.html")
        report_index.BASE = self.tmp
        report_index.REPORTS = os.path.join(self.tmp, "reports")

    def tearDown(self):
        (bd.OUTPUT, report_index.OUTPUT, report_index.BASE,
         report_index.REPORTS) = self._saved
        if self._saved_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved_env
        super().tearDown()

    def _build(self, **kw):
        bd.build(self.con, check_receiver=False, **kw)
        with open(bd.OUTPUT, encoding="utf-8") as f:
            html = f.read()
        with open(report_index.OUTPUT, encoding="utf-8") as f:
            return html, f.read()

    def test_titles_are_withheld_from_rows_and_sessions(self):
        html, _ = self._build()
        self.assertIn(self.TITLE, html, "sanity: an ordinary build shows it")
        html, _ = self._build(redact=True)
        self.assertNotIn("Secret plan", html)

    def test_session_title_is_blank_in_the_session_rows(self):
        _, rows, _ = self.collect()
        self.assertEqual(
            {s["id"]: s["title"] for s in bd.session_rows(rows)}["sess-a"],
            self.TITLE)
        for s in bd.session_rows(rows, redact=True):
            self.assertIsNone(s["title"])

    def test_pages_from_an_earlier_build_are_removed(self):
        _, idx = self._build(conversations_n=50)
        folder = os.path.join(self.tmp, "conversations")
        self.assertTrue(os.path.exists(os.path.join(folder, "p1.html")))
        self.assertIn("conversations/index.html", idx)
        self.assertIn("file:", idx, "the /insights link is there to be withheld")
        _, idx = self._build(redact=True, conversations_n=50)
        self.assertFalse(os.path.exists(os.path.join(folder, "p1.html")))
        self.assertFalse(os.path.exists(os.path.join(folder, "index.html")))
        self.assertNotIn("conversations/", idx)
        self.assertNotIn("file:", idx)
        self.assertIn("dashboard.html", idx)

    def test_only_pages_this_tool_wrote_are_removed(self):
        self._build(conversations_n=50)
        folder = os.path.join(self.tmp, "conversations")
        notes = os.path.join(folder, "notes.txt")
        mine = os.path.join(folder, "my-own-page.html")
        for path, text in ((notes, "keep"), (mine, "<html>not generated</html>")):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        self._build(redact=True)
        self.assertTrue(os.path.exists(notes))
        self.assertTrue(os.path.exists(mine))
        self.assertFalse(os.path.exists(os.path.join(folder, "p1.html")))


class RepricedModelCosts(Fixture):
    """A CLI session total scales the whole row, per-model costs included."""

    def test_model_costs_still_add_up_to_the_row(self):
        db.set_run_cost(self.con, "sess-a", 7.0, 1, "test")
        self.con.commit()
        by_id, _, _ = self.collect()
        r = by_id["p1"]
        self.assertEqual(bd.REPRICED["rows"], 1)
        self.assertAlmostEqual(r["cost"], 7.0, places=3)
        self.assertTrue(r["est"], "a share of the session total is an estimate")
        self.assertAlmostEqual(sum(m["cost"] for m in r["models"]), r["cost"],
                               places=3)
        self.assertAlmostEqual(sum(r["comp"]), r["cost"], places=3)

    def test_blocks_follow_the_repricing_and_say_they_are_estimates(self):
        now = T0 + timedelta(hours=2)
        plain, _ = bd.compute_blocks(self.con, now=now, days=365)
        scaled, _ = bd.compute_blocks(self.con, now=now, days=365,
                                      factors={"sess-a": 2.0})
        a = sum(r["cost"] for r in self.collect()[1] if r["session"] == "sess-a")
        self.assertAlmostEqual(scaled[-1]["cost"] - plain[-1]["cost"], a,
                               places=3)
        self.assertTrue(scaled[-1]["est"])


class CompositionMatchesTheTotal(Fixture):
    def test_unsplit_cache_writes_use_the_providers_multiplier(self):
        """comp used the 1h multiplier whatever the provider's was, so the
        split drifted from the total and was only rescued by rescaling."""
        saved = dict(pricing.UNSPLIT_CACHE_MULT)
        pricing.UNSPLIT_CACHE_MULT[pricing.BEDROCK] = 1.25
        try:
            db.upsert_prompt(self.con, "pbd", "sess-bd", "proj4", ts(20),
                             "bedrock", "jsonl", 0, None, "human")
            db.insert_requests_jsonl(self.con, [request(
                "bd1", "pbd", "sess-bd", ts(20), OPUS,
                cache_create_tokens=1000000, provider=pricing.BEDROCK)])
            self.con.commit()
            by_id, _, _ = self.collect()
            r = by_id["pbd"]
            rate = pricing.resolve(OPUS, ts(20), pricing.BEDROCK)
            want = bd.cache_write_cost(rate, 0, 0, 1000000, pricing.BEDROCK)
            self.assertAlmostEqual(r["comp"][1], want, places=3)
            self.assertAlmostEqual(r["cost"], want, places=3)
        finally:
            pricing.UNSPLIT_CACHE_MULT.clear()
            pricing.UNSPLIT_CACHE_MULT.update(saved)


class WindowAndBlockEstimates(Fixture):
    NOW = T0 + timedelta(hours=2)

    def _blocks(self):
        return bd.compute_blocks(self.con, now=self.NOW, days=365)[0]

    def test_an_estimated_block_says_so(self):
        b = self._blocks()[-1]
        self.assertTrue(b["est"])
        self.assertEqual(b["unpriced"], 0)
        w = bd.current_window(self._blocks(), now=self.NOW)
        self.assertTrue(w["est"])
        self.assertEqual(w["unpriced"], 0)

    def test_a_block_of_cli_figures_is_exact(self):
        db.insert_requests_jsonl(self.con, [request(
            "exact1", "p9", "sess-x", ts(days=3), OPUS, input_tokens=10,
            output_tokens=10, cost_usd=0.5)])
        self.con.commit()
        b = bd.compute_blocks(self.con, now=T0 + timedelta(days=3, hours=1),
                              days=365)[0][-1]
        self.assertFalse(b["est"])
        self.assertEqual(b["unpriced"], 0)
        self.assertAlmostEqual(b["cost"], 0.5)

    def test_unpriced_requests_are_counted_not_hidden(self):
        db.insert_requests_jsonl(self.con, [
            request("u1", "p9", "sess-x", ts(days=3), "mystery-model",
                    input_tokens=100, output_tokens=10),
            request("u2", "p9", "sess-x", ts(1, days=3), "mystery-model",
                    input_tokens=100, output_tokens=10),
            # a placeholder with no tokens is not a model that billed anything
            request("u3", "p9", "sess-x", ts(2, days=3), "<synthetic>"),
            # a CLI-reported figure is a cost, whatever the model is called
            request("u4", "p9", "sess-x", ts(3, days=3), "mystery-model",
                    input_tokens=100, cost_usd=0.25),
        ])
        self.con.commit()
        now = T0 + timedelta(days=3, hours=1)
        blocks = bd.compute_blocks(self.con, now=now, days=365)[0]
        b = blocks[-1]
        self.assertEqual(b["unpriced"], 2)
        self.assertTrue(b["est"])
        w = bd.current_window(blocks, now=now)
        self.assertEqual(w["unpriced"], 2)
        self.assertEqual(w["requests"], 4)

    def test_window_and_open_block_count_the_same_requests(self):
        """The tile skipped requests with no prompt id; the block did not."""
        db.insert_requests_jsonl(self.con, [
            request("np1", None, "sess-a", ts(30), OPUS, output_tokens=777)])
        self.con.commit()
        blocks = self._blocks()
        w = bd.current_window(blocks, now=self.NOW)
        self.assertEqual(w["requests"], blocks[-1]["calls"])
        self.assertEqual(w["requests"], 11)
        self.assertEqual(w["out"], blocks[-1]["out"])
        self.assertAlmostEqual(w["cost"], blocks[-1]["cost"], places=3)

    def test_an_idle_window_is_none(self):
        later = T0 + timedelta(days=2)
        self.assertIsNone(bd.current_window(
            bd.compute_blocks(self.con, now=later, days=365)[0], now=later))

    def test_the_payload_carries_the_new_fields(self):
        blocks = self._blocks()
        enc = bd.compact_table(
            [dict(b, start=b["start"].isoformat(), end=b["end"].isoformat(),
                  models=sorted(b["models"])) for b in blocks],
            bd.BLOCK_COLUMNS, bd.Strings(), list_str_cols=("models",))
        self.assertEqual(enc["cols"]["est"], [1] * len(blocks))
        self.assertEqual(enc["cols"]["unpriced"], [0] * len(blocks))


class PageRefresh(Fixture):
    def _write(self):
        _, rows, _ = self.collect()
        return conversations.write_pages(self.con, self.tmp, rows,
                                         limit=10), rows

    def test_a_cost_change_rewrites_a_page_whose_transcript_did_not_move(self):
        self._write()
        again, _ = self._write()
        self.assertEqual(again["written"], 0, "nothing changed, nothing written")
        # A late OTel row, a corrected rate: the cost moves, the transcript does not.
        self.con.execute(
            "UPDATE api_requests SET cost_usd = 9.99 WHERE request_id = 'r6'")
        self.con.commit()
        third, rows = self._write()
        self.assertEqual(third["written"], 1)
        with open(os.path.join(self.tmp, "conversations", "p1.html"),
                  encoding="utf-8") as f:
            page = f.read()
        row = next(r for r in rows if r["id"] == "p1")
        self.assertIn(conversations.fmt_cost(row["cost"], row["est"]), page)
        fourth, _ = self._write()
        self.assertEqual(fourth["written"], 0)

    def test_a_page_without_a_fingerprint_is_rewritten_once(self):
        self._write()
        page = os.path.join(self.tmp, "conversations", "p1.html")
        with open(page, encoding="utf-8") as f:
            text = f.read()
        with open(page, "w", encoding="utf-8") as f:
            f.write(conversations.FINGERPRINT_RE.sub("", text))
        again, _ = self._write()
        self.assertEqual(again["written"], 1)


class DigestWeek(Fixture):
    """The digest covers one whole ISO week, whatever day it is run."""
    _run = Digest._run

    def test_the_window_ends_on_the_most_recent_monday(self):
        mon = datetime(2026, 5, 18, tzinfo=UTC)
        for day in range(7):       # Monday through Sunday
            now = datetime(2026, 5, 18 + day, 15, 30, tzinfo=UTC)
            start, end = digest.week_bounds(now)
            self.assertEqual(end, mon)
            self.assertEqual(start, mon - timedelta(days=7))
            self.assertEqual(tuple(start.isocalendar())[:2], (2026, 20))
        self.assertEqual(digest.week_bounds(WED)[1], mon)

    def test_label_filename_and_contents_agree(self):
        """On a Wednesday the old window ran Wed-Tue, across two ISO weeks."""
        db.upsert_prompt(self.con, "pnext", "sess-b", "proj2", ts(0, days=6),
                         "this week, not last", "jsonl", 0, None, "human")
        db.insert_requests_jsonl(self.con, [request(
            "nx1", "pnext", "sess-b", ts(0, days=6), OPUS, output_tokens=50)])
        self.con.commit()
        res, page = self._run(WED)
        self.assertIn("digest-2026-W20", os.path.basename(res["digest"]))
        self.assertIn("2026-05-11", page)
        self.assertIn("2026-05-17", page)
        self.assertEqual(res["prompts"], 2, "p1 and p3; pnext is on the 18th")
        self.assertNotIn("this week, not last", page)

    def test_api_errors_are_counted_in_the_week_only(self):
        db.insert_requests_jsonl(self.con, [
            request("e1", "p1", "sess-a", ts(5), OPUS, error="overloaded"),
            request("e2", "p1", "sess-a", ts(6), OPUS, error="overloaded"),
            # the previous week, and the one before it: inside collect()'s
            # prefilter margin or outside it, neither is this week's figure
            request("e3", "p0", "sess-c", ts(0, days=-6), OPUS, error="x"),
            request("e4", "p0", "sess-c", ts(0, days=-12), OPUS, error="x"),
            # the week after
            request("e5", "p1", "sess-a", ts(0, days=7), OPUS, error="x"),
        ])
        self.con.commit()
        _, page = self._run(WED)
        self.assertIn("2 API errors", page)

    def test_tool_errors_until_is_exclusive(self):
        everything = bd.tool_errors(self.con)
        early = bd.tool_errors(self.con, until=ts(1))
        # the fixture's four tool calls plus its four Skill calls
        self.assertEqual(sum(t[1] for t in everything["tools"]), 8)
        self.assertEqual(sum(t[1] for t in early["tools"]), 1)


class ReceiverPort(unittest.TestCase):
    def test_port_overrides_the_default_address(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            port = srv.getsockname()[1]
            self.assertTrue(bd.receiver_running(port=port))
        finally:
            srv.close()
        self.assertFalse(bd.receiver_running(port=port))


if __name__ == "__main__":
    unittest.main(verbosity=2)
