"""End-to-end checks for multi-source ingest and the report index.

Builds a throwaway Claude directory tree (primary + sibling + a nested one
under an "extra location"), ingests it into a temporary DB, and asserts the
project names come out labeled by origin. Also exercises the SSH-config
parser, the remote tar extractor's path guarding, and index.html generation.

Runs entirely on the local temp disk: python test_sources.py
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import sources

TS = "2026-08-20T10:00:00.000Z"


def write_transcript(claude_dir, slug, session, cwd, prompt_id, req_id):
    """A minimal but structurally faithful main-session transcript."""
    d = os.path.join(claude_dir, "projects", slug)
    os.makedirs(d, exist_ok=True)
    entries = [
        {"type": "user", "origin": {"kind": "human"}, "promptId": prompt_id,
         "timestamp": TS, "cwd": cwd,
         "message": {"content": [{"type": "text", "text": "do a thing"}]}},
        {"type": "assistant", "timestamp": TS, "requestId": req_id,
         "message": {"model": "claude-opus-5", "content": [],
                     "usage": {"input_tokens": 10, "output_tokens": 20,
                               "cache_read_input_tokens": 5,
                               "cache_creation_input_tokens": 0}}},
    ]
    with open(os.path.join(d, session + ".jsonl"), "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def write_legacy_transcript(claude_dir, slug, session, cwd, prompts):
    """A transcript in the pre-origin format.

    Older CLI builds wrote no `origin` marker at all: a typed prompt and a
    tool result are both type "user" with a promptId, distinguishable only by
    shape. Each prompt here is followed by an assistant turn and a tool
    result, so the tool result gets a chance to be miscounted as a prompt.
    """
    d = os.path.join(claude_dir, "projects", slug)
    os.makedirs(d, exist_ok=True)
    entries = []
    for i, text in enumerate(prompts):
        entries.append({
            "type": "user", "promptId": f"legacy-p{i}", "promptSource": "sdk",
            "userType": "external", "sessionId": session, "cwd": cwd,
            "timestamp": TS, "version": "2.1.175",
            "message": {"content": [{"type": "text", "text": text}]}})
        entries.append({
            "type": "assistant", "timestamp": TS, "sessionId": session,
            "requestId": f"legacy-req{i}", "version": "2.1.175",
            "message": {"model": "claude-opus-5", "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_read_input_tokens": 5,
                                  "cache_creation_input_tokens": 0}}})
        entries.append({
            "type": "user", "promptId": f"legacy-p{i}", "sessionId": session,
            "timestamp": TS, "version": "2.1.175",
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": f"toolu_{i}",
                                     "content": "ok"}]}})
    with open(os.path.join(d, session + ".jsonl"), "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class MultiSourceIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="claude-lens-test-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.primary = os.path.join(cls.home, ".claude")
        cls.sibling = os.path.join(cls.home, ".claude-work")
        # An "extra location" pointing well above the real Claude dir, which
        # is what requirement 4 is about: backups/<machine>/.claude
        cls.backup_root = os.path.join(cls.tmp, "backups")
        cls.nested = os.path.join(cls.backup_root, "oldlaptop", ".claude")
        # A sibling that is a *container* of Claude dirs rather than one
        # itself - only found if siblings get the same depth search as extra
        # locations.
        cls.archived = os.path.join(cls.home, ".claude-archive", "oldbox",
                                    ".claude")

        write_transcript(cls.primary, "-y-work-alpha", "s-aaaa",
                         "/y/work/alpha", "p-aaaa", "req-aaaa")
        write_transcript(cls.sibling, "-y-work-beta", "s-bbbb",
                         "/y/work/beta", "p-bbbb", "req-bbbb")
        write_transcript(cls.nested, "-y-work-alpha", "s-cccc",
                         "/y/work/alpha", "p-cccc", "req-cccc")
        write_transcript(cls.archived, "-y-work-delta", "s-eeee",
                         "/y/work/delta", "p-eeee", "req-eeee")
        # A legacy-format transcript, in the primary directory
        write_legacy_transcript(
            cls.primary, "-y-work-legacy", "s-legacy", "/y/work/legacy",
            ["first legacy prompt", "second legacy prompt"])
        # Decoys: a directory named like Claude's but with no projects/, and a
        # deep tree that must be pruned rather than walked forever.
        os.makedirs(os.path.join(cls.home, ".claude-empty", "todos"))
        os.makedirs(os.path.join(cls.backup_root, "junk", "node_modules", "x"))

        os.environ["CLAUDE_CONFIG_DIR"] = cls.primary
        cls.dbpath = os.path.join(cls.tmp, "metrics.db")
        cls._connect = db.connect
        db.connect = lambda path=cls.dbpath, cross_thread=False: \
            MultiSourceIngest._connect(path, cross_thread)

        import jsonl_ingest
        cls.stats = jsonl_ingest.run(
            force=True,
            config=sources.SourceConfig(extra_locations=[cls.backup_root]))

    @classmethod
    def tearDownClass(cls):
        db.connect = cls._connect
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- discovery ---------------------------------------------------------

    def test_looks_like_claude_dir(self):
        self.assertTrue(sources.looks_like_claude_dir(self.primary))
        self.assertFalse(sources.looks_like_claude_dir(
            os.path.join(self.home, ".claude-empty")))
        self.assertFalse(sources.looks_like_claude_dir(self.home))

    def test_unnamed_dir_recognised_by_its_transcripts(self):
        """A copied/renamed Claude dir is still recognised (requirement 4)."""
        odd = os.path.join(self.tmp, "restored-2024")
        write_transcript(odd, "-y-work-gamma", "s-dddd", "/y/work/gamma",
                         "p-dddd", "req-dddd")
        self.assertTrue(sources.looks_like_claude_dir(odd))
        shutil.rmtree(odd)

    def test_discovers_primary_sibling_and_nested(self):
        roots = sources.discover_local([self.backup_root])
        by_label = {r.label: r for r in roots}
        self.assertEqual(roots[0].label, "")               # primary unlabeled
        self.assertEqual(roots[0].origin, "primary")
        self.assertIn(".claude-work", by_label)            # requirement 1
        self.assertIn("oldlaptop", by_label)               # requirement 4
        self.assertEqual(by_label["oldlaptop"].path, self.nested)
        # the decoy without projects/ is not a source
        self.assertNotIn(".claude-empty", by_label)

    def test_sibling_container_is_searched_to_depth(self):
        """A .claude* sibling holding Claude dirs, not being one itself."""
        roots = sources.discover_local()
        by_label = {r.label: r for r in roots}
        self.assertIn("oldbox", by_label)
        self.assertEqual(by_label["oldbox"].path, self.archived)
        self.assertEqual(by_label["oldbox"].origin, "sibling")

    def test_sibling_depth_is_bounded_like_extra_locations(self):
        """depth=1 cannot reach two levels into .claude-archive."""
        shallow = sources.discover_local(scan_siblings=True, depth=1)
        self.assertNotIn("oldbox", {r.label for r in shallow})
        deep = sources.discover_local(scan_siblings=True, depth=2)
        self.assertIn("oldbox", {r.label for r in deep})

    def test_labels_from_separate_discoveries_are_de_collided(self):
        """A backup folder and an SSH host sharing a name must not merge."""
        roots = [sources.Root("/a", "", "primary"),
                 sources.Root("/b", "oldlaptop", "local"),
                 sources.Root("/c", "oldlaptop", "remote"),
                 sources.Root("/d", "oldlaptop", "remote")]
        self.assertEqual([r.label for r in sources.dedupe_labels(roots)],
                         ["", "oldlaptop", "oldlaptop-2", "oldlaptop-3"])

    def test_siblings_can_be_disabled(self):
        roots = sources.discover_local(scan_siblings=False)
        self.assertEqual([r.label for r in roots], [""])

    def test_depth_limits_the_search(self):
        # .claude sits two levels below backup_root; depth 1 must not reach it
        self.assertEqual(sources.find_claude_dirs(self.backup_root, 1), [])
        self.assertEqual(sources.find_claude_dirs(self.backup_root, 2),
                         [self.nested])

    # -- ingest + display names --------------------------------------------

    def test_projects_are_labeled_by_origin(self):
        import build_dashboard
        self.assertEqual(len(self.stats["sources"]), 4)

        con = db.connect()
        rows, _ = build_dashboard.collect(con)
        con.close()
        projects = sorted(r["project"] for r in rows)
        self.assertEqual(projects, [
            ".claude-work/beta",       # sibling directory
            "alpha",                   # primary: name unchanged
            "legacy",                  # primary, pre-origin transcript
            "legacy",
            "oldbox/delta",            # nested inside a sibling container
            "oldlaptop/alpha",         # nested under an extra location
        ])

    def test_label_recorded_on_sessions(self):
        con = db.connect()
        labels = dict(con.execute(
            "SELECT session_id, source_label FROM sessions"))
        con.close()
        self.assertEqual(labels["s-aaaa"], "")
        self.assertEqual(labels["s-bbbb"], ".claude-work")
        self.assertEqual(labels["s-cccc"], "oldlaptop")

    def test_same_project_name_on_two_sources_stays_separate(self):
        """alpha exists on the primary and in the backup; both must survive."""
        import build_dashboard
        con = db.connect()
        rows, _ = build_dashboard.collect(con)
        con.close()
        alphas = {r["project"] for r in rows if r["project"].endswith("alpha")}
        self.assertEqual(alphas, {"alpha", "oldlaptop/alpha"})

    def test_schema_is_current(self):
        con = db.connect()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         db.SCHEMA_VERSION)
        con.close()


class LegacyTranscripts(unittest.TestCase):
    """Transcripts written before the origin marker existed.

    Their human prompts carry no origin at all, so the strict rule dropped
    them - and with them every API request in the file, since usage is
    attributed to the prompt above it. These check the shape-based fallback
    recovers them WITHOUT loosening anything on modern transcripts.
    """

    def setUp(self):
        import jsonl_ingest
        self.ji = jsonl_ingest
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-legacy-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, entries):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def test_detects_vintage_per_file(self):
        modern = self._write("modern.jsonl", [
            {"type": "user", "sessionId": "s-modern", "promptId": "p1",
             "origin": {"kind": "human"},
             "message": {"content": "hello"}}])
        legacy = self._write("legacy.jsonl", [
            {"type": "user", "sessionId": "s-legacy", "promptId": "p1",
             "promptSource": "sdk",
             "message": {"content": [{"type": "text", "text": "hello"}]}}])
        self.assertEqual(self.ji.scan_header(modern, "fallback"),
                         ("s-modern", False))
        self.assertEqual(self.ji.scan_header(legacy, "fallback"),
                         ("s-legacy", True))

    def test_origin_on_injected_turns_alone_does_not_mean_modern(self):
        """Older files carry origin on task-notifications but not on prompts."""
        path = self._write("mixed.jsonl", [
            {"type": "user", "sessionId": "s1", "promptId": "p1",
             "origin": {"kind": "task-notification"}, "promptSource": "sdk",
             "message": {"content": "<task-notification>done</task-notification>"}},
            {"type": "user", "sessionId": "s1", "promptId": "p2",
             "promptSource": "sdk",
             "message": {"content": [{"type": "text", "text": "real prompt"}]}}])
        _, legacy = self.ji.scan_header(path, "x")
        self.assertTrue(legacy, "only a human origin marks a file as modern")

    def test_legacy_prompt_recognised_tool_result_is_not(self):
        prompt = {"type": "user", "promptId": "p1", "promptSource": "sdk",
                  "message": {"content": [{"type": "text", "text": "do it"}]}}
        tool_result = {"type": "user", "promptId": "p1",
                       "message": {"content": [{"type": "tool_result",
                                                "tool_use_id": "t1"}]}}
        self.assertTrue(self.ji.is_human_prompt(prompt, legacy=True))
        self.assertFalse(self.ji.is_human_prompt(tool_result, legacy=True))
        # ...and neither counts on a modern transcript
        self.assertFalse(self.ji.is_human_prompt(prompt, legacy=False))

    def test_legacy_rule_filters_harness_injected_turns(self):
        for text in ("<command-name>/clear</command-name>",
                     "<local-command-caveat>Caveat: ...",
                     "<system-reminder>hi</system-reminder>",
                     "<ide_opened_file>opened X",
                     "Caveat: The messages below were generated...",
                     "This session is being continued from a previous...",
                     "[Request interrupted by user]",
                     "Another Claude session sent a message: hi"):
            entry = {"type": "user", "promptId": "p", "promptSource": "sdk",
                     "message": {"content": [{"type": "text", "text": text}]}}
            self.assertFalse(self.ji.is_human_prompt(entry, legacy=True), text)

    def test_legacy_rule_skips_meta_system_sidechain_and_empty(self):
        base = {"type": "user", "promptId": "p", "promptSource": "sdk",
                "message": {"content": [{"type": "text", "text": "hi"}]}}
        for tweak in ({"isMeta": True}, {"promptSource": "system"},
                      {"isSidechain": True},
                      {"message": {"content": [{"type": "text", "text": "  "}]}}):
            entry = dict(base, **tweak)
            self.assertFalse(self.ji.is_human_prompt(entry, legacy=True), tweak)

    def test_modern_non_prompts_never_become_prompts(self):
        """The exact shapes that appear in real modern transcripts."""
        for text in ("<command-name>/clear</command-name>",
                     "This session is being continued from a previous...",
                     "[Request interrupted by user]",
                     "just some text with no origin at all"):
            entry = {"type": "user", "promptId": "p",
                     "message": {"content": text}}
            self.assertFalse(self.ji.is_human_prompt(entry, legacy=False), text)

    def test_inline_sidechain_is_attributed_to_a_subagent(self):
        """Older layouts interleaved subagent turns into the main file."""
        self.assertIsNone(self.ji.inline_agent({"type": "assistant"}))
        self.assertEqual(self.ji.inline_agent(
            {"isSidechain": True, "attributionAgent": "Explore"}), "Explore")
        self.assertEqual(self.ji.inline_agent(
            {"isSidechain": True, "agentId": "agent-7"}), "agent-7")
        self.assertEqual(self.ji.inline_agent({"isSidechain": True}), "subagent")

    def test_end_to_end_legacy_file_yields_prompts_and_usage(self):
        tmpdb = os.path.join(self.tmp, "m.db")
        claude = os.path.join(self.tmp, ".claude")
        write_legacy_transcript(claude, "-srv-app", "s-leg", "/srv/app",
                                ["first prompt", "second prompt"])
        orig = db.connect
        db.connect = lambda path=tmpdb, cross_thread=False: orig(path, cross_thread)
        try:
            con = db.connect()
            self.ji.ingest_tree(con, os.path.join(claude, "projects"),
                                "oldbox", force=True)
            con.commit()
            prompts = con.execute(
                "SELECT text FROM prompts ORDER BY text").fetchall()
            reqs = con.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0]
            out = con.execute(
                "SELECT SUM(output_tokens) FROM api_requests").fetchone()[0]
            con.close()
        finally:
            db.connect = orig
        self.assertEqual([p[0] for p in prompts],
                         ["first prompt", "second prompt"])
        self.assertEqual(reqs, 2, "usage must attach, not be dropped")
        self.assertEqual(out, 40)


class SshConfig(unittest.TestCase):
    def test_hosts_and_includes(self):
        tmp = tempfile.mkdtemp(prefix="claude-lens-ssh-")
        try:
            inc = os.path.join(tmp, "extra.conf")
            with open(inc, "w", encoding="utf-8") as f:
                f.write("Host included-box\n  HostName 10.0.0.9\n")
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("Include extra.conf\n"
                        "Host build-server\n  HostName 10.0.0.1\n"
                        "Host *\n  User les\n"          # wildcard: not a machine
                        "Host !nope\n"                  # negation: not a machine
                        "Host a b   # trailing comment\n")
            hosts = sources.ssh_config_hosts(cfg)
            self.assertEqual(hosts, ["included-box", "build-server", "a", "b"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_config_is_not_an_error(self):
        self.assertEqual(sources.ssh_config_hosts("/no/such/ssh/config"), [])


class RemoteExtract(unittest.TestCase):
    def test_traversal_members_are_refused(self):
        """A hostile remote must not write outside its own cache directory."""
        tmp = tempfile.mkdtemp(prefix="claude-lens-tar-")
        try:
            payload = os.path.join(tmp, "payload.jsonl")
            with open(payload, "w", encoding="utf-8") as f:
                f.write("{}\n")
            archive = os.path.join(tmp, "a.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(payload, arcname="./.claude/projects/p/ok.jsonl")
                tar.add(payload, arcname="../escaped.jsonl")
                tar.add(payload, arcname="/tmp/absolute.jsonl")
            dest = os.path.join(tmp, "cache")
            os.makedirs(dest)
            with tarfile.open(archive, mode="r|gz") as tar:
                sources._safe_extract(tar, dest)
            self.assertTrue(os.path.exists(os.path.join(
                dest, ".claude", "projects", "p", "ok.jsonl")))
            # `..` is refused outright; an absolute member is landed inside the
            # cache (tar strips the leading slash) rather than at the real path
            self.assertFalse(os.path.exists(os.path.join(tmp, "escaped.jsonl")))
            for root, _, files in os.walk(dest):
                for name in files:
                    self.assertTrue(
                        os.path.abspath(os.path.join(root, name)).startswith(
                            os.path.abspath(dest) + os.sep))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_remote_script_is_posix_sh(self):
        """Guard against bashisms creeping into the script sent to remotes."""
        for bashism in ("[[", "function ", "$'", "&>"):
            self.assertNotIn(bashism, sources.REMOTE_SH)


class FailureBackoff(unittest.TestCase):
    """A misconfigured remote must cost the background receiver ~nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-backoff-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self._connect = db.connect
        db.connect = lambda path=self.dbpath, cross_thread=False: \
            self._connect(path, cross_thread)

    def tearDown(self):
        db.connect = self._connect
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_classifies_real_openssh_messages(self):
        cases = {
            "les@box: Permission denied (publickey).": "auth",
            "Host key verification failed.": "auth",
            "ssh: Could not resolve hostname nope: Name or service not known":
                "unreachable",
            "ssh: connect to host box port 22: Connection refused":
                "unreachable",
            "ssh: connect to host box port 22: Connection timed out":
                "unreachable",
            "timed out after 300s": "unreachable",
            "something entirely new": "other",
        }
        for message, expected in cases.items():
            self.assertEqual(sources.classify_error(message), expected, message)

    def test_auth_failures_park_the_host_for_hours(self):
        """A missing key will not appear in 15 minutes - do not retry hourly."""
        self.assertEqual(sources.retry_delay("auth", 1), sources.AUTH_RETRY_S)
        self.assertEqual(sources.retry_delay("auth", 9), sources.AUTH_RETRY_S)
        self.assertEqual(sources.retry_delay("no_claude", 1),
                         sources.NO_CLAUDE_RETRY_S)

    def test_transient_failures_back_off_exponentially_then_cap(self):
        delays = [sources.retry_delay("unreachable", n) for n in range(1, 12)]
        self.assertEqual(delays[0], sources.RETRY_BASE_S)
        self.assertEqual(delays[1], sources.RETRY_BASE_S * 2)
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])))
        self.assertEqual(delays[-1], sources.RETRY_MAX_S)

    def test_parked_host_is_skipped_without_any_ssh(self):
        import jsonl_ingest
        con = db.connect()
        db.record_remote_failure(con, "dead-box", "Permission denied (publickey).",
                                 time.time() + 3600)
        con.commit()

        calls = []
        original = sources.fetch_remote

        def spy(host, **kwargs):
            calls.append(host)
            return {"host": host, "files": 0, "cache": "", "error": None,
                    "kind": None, "elapsed": 0.0}

        sources.fetch_remote = spy
        try:
            cfg = sources.SourceConfig(remotes=["dead-box"])
            skipped = jsonl_ingest.fetch_remotes(con, cfg, respect_backoff=True)
            self.assertEqual(calls, [], "parked host must not be contacted")
            self.assertIn("backing off", skipped[0]["skipped"])

            # ...but an explicit run ignores the backoff entirely
            jsonl_ingest.fetch_remotes(con, cfg, respect_backoff=False)
            self.assertEqual(len(calls), 1)
        finally:
            sources.fetch_remote = original
            con.close()

    def test_repeated_failures_escalate_and_success_clears(self):
        import jsonl_ingest
        con = db.connect()
        cfg = sources.SourceConfig(remotes=["flaky"])
        original = sources.fetch_remote

        def fail(host, **kwargs):
            return {"host": host, "files": 0, "cache": "", "elapsed": 0.1,
                    "error": "ssh: connect to host flaky port 22: "
                             "Connection refused",
                    "kind": "unreachable"}

        sources.fetch_remote = fail
        try:
            for expected in (1, 2, 3):
                jsonl_ingest.fetch_remotes(con, cfg, respect_backoff=False)
                state = db.get_remote_state(con, "flaky")
                self.assertEqual(state["fail_count"], expected)
            # each failure pushes the next attempt further out
            first_delay = sources.retry_delay("unreachable", 1)
            self.assertGreater(state["next_attempt"] - time.time(), first_delay)
            # a failure must not advance the incremental watermark
            self.assertEqual(state["last_fetch"], 0.0)

            # respect_backoff=False: the host is parked after three failures,
            # and this asserts what a *successful* fetch does to that state
            sources.fetch_remote = lambda host, **kw: {
                "host": host, "files": 3, "cache": "", "error": None,
                "kind": None, "elapsed": 0.2}
            jsonl_ingest.fetch_remotes(con, cfg, respect_backoff=False)
            state = db.get_remote_state(con, "flaky")
            self.assertEqual(state["fail_count"], 0)
            self.assertEqual(state["next_attempt"], 0.0)
            self.assertIsNone(state["last_error"])
            self.assertGreater(state["last_fetch"], 0)
        finally:
            sources.fetch_remote = original
            con.close()

    def test_budget_stops_a_pass_full_of_dead_hosts(self):
        import jsonl_ingest
        con = db.connect()
        cfg = sources.SourceConfig(remotes=[f"box{i}" for i in range(6)],
                                   remote_budget=1)
        original = sources.fetch_remote

        def slow(host, **kwargs):
            time.sleep(0.4)
            return {"host": host, "files": 0, "cache": "", "elapsed": 0.4,
                    "error": "ssh: connect to host: Connection refused",
                    "kind": "unreachable"}

        sources.fetch_remote = slow
        try:
            results = jsonl_ingest.fetch_remotes(con, cfg, respect_backoff=True)
            attempted = [r for r in results if "skipped" not in r]
            budgeted = [r for r in results
                        if r.get("skipped") == "remote time budget spent"]
            self.assertLess(len(attempted), 6, "budget should cut the pass short")
            self.assertTrue(budgeted)
            self.assertEqual(len(results), 6, "every host still reported")
        finally:
            sources.fetch_remote = original
            con.close()

    def test_fetch_remote_never_raises(self):
        """Whatever goes wrong, the caller gets a result dict, not a traceback."""
        original = sources.subprocess.run

        def boom(*a, **k):
            raise RuntimeError("something unexpected deep in subprocess")

        sources.subprocess.run = boom
        try:
            res = sources.fetch_remote("whatever", timeout=5)
            self.assertIsNotNone(res["error"])
            self.assertIn("RuntimeError", res["error"])
            self.assertEqual(res["kind"], "other")
        finally:
            sources.subprocess.run = original

    def test_ssh_invocation_cannot_prompt_or_hang(self):
        """The flags that make a credential problem fail fast, not block."""
        seen = {}

        def capture(cmd, **kwargs):
            seen["cmd"] = cmd
            raise subprocess.TimeoutExpired(cmd, 1)

        original = sources.subprocess.run
        sources.subprocess.run = capture
        try:
            sources.fetch_remote("box", connect_timeout=8)
        finally:
            sources.subprocess.run = original
        joined = " ".join(seen["cmd"])
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("NumberOfPasswordPrompts=0", joined)
        self.assertIn("ConnectTimeout=8", joined)
        self.assertIn("ServerAliveInterval=15", joined)
        self.assertIn("ServerAliveCountMax=3", joined)


class ReportIndex(unittest.TestCase):
    def test_index_lists_dashboard_and_reports(self):
        import report_index
        tmp = tempfile.mkdtemp(prefix="claude-lens-idx-")
        try:
            reports = os.path.join(tmp, "reports")
            os.makedirs(reports)
            open(os.path.join(tmp, "dashboard.html"), "w").close()
            for name in ("digest-2026-W33.html", "digest-2026-W32-101500.html",
                         "index.html"):
                open(os.path.join(reports, name), "w").close()
            report_index.BASE, report_index.REPORTS = tmp, reports
            out = report_index.build(os.path.join(tmp, "index.html"))
            with open(out, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("dashboard.html", html)
            self.assertIn("reports/digest-2026-W33.html", html)
            self.assertIn("Week 33 of 2026", html)
            self.assertIn("(re-run)", html)          # the -HHMMSS digest
            # reports/index.html is the digest list, not a report of its own
            self.assertNotIn("reports/index.html", html)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
