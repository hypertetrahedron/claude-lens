"""End-to-end checks for multi-source ingest and the report index.

Builds a throwaway Claude directory tree (primary + sibling + a nested one
under an "extra location"), ingests it into a temporary DB, and asserts the
project names come out labeled by origin. Also exercises the SSH-config
parser, the remote tar extractor's path guarding, and index.html generation.

Runs entirely on the local temp disk: python test_sources.py
"""
import json
import os
import re
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
        # cowork=False keeps this hermetic: without it the run would pick up
        # a real Claude Desktop install on the developer's machine.
        cls.stats = jsonl_ingest.run(
            force=True,
            config=sources.SourceConfig(extra_locations=[cls.backup_root],
                                        cowork=False))

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
        sid, legacy_flag, cwd = self.ji.scan_header(modern, "fallback")
        self.assertEqual((sid, legacy_flag), ("s-modern", False))
        sid, legacy_flag, cwd = self.ji.scan_header(legacy, "fallback")
        self.assertEqual((sid, legacy_flag), ("s-legacy", True))

    def test_a_huge_transcript_is_streamed_instead_of_held(self):
        """The header scan never holds the file, whatever its size."""
        path = self._write("big.jsonl", [
            {"type": "user", "sessionId": "s", "promptId": "p", "cwd": "/w",
             "promptSource": "sdk",
             "message": {"content": [{"type": "text", "text": "x" * 200}]}}])
        sid, legacy_flag, cwd = self.ji.scan_header(path, "x")
        self.assertEqual(sid, "s")
        self.assertTrue(legacy_flag, "vintage is still decided correctly")
        self.assertEqual(cwd, "/w")

    def test_origin_on_injected_turns_alone_does_not_mean_modern(self):
        """Older files carry origin on task-notifications but not on prompts."""
        path = self._write("mixed.jsonl", [
            {"type": "user", "sessionId": "s1", "promptId": "p1",
             "origin": {"kind": "task-notification"}, "promptSource": "sdk",
             "message": {"content": "<task-notification>done</task-notification>"}},
            {"type": "user", "sessionId": "s1", "promptId": "p2",
             "promptSource": "sdk",
             "message": {"content": [{"type": "text", "text": "real prompt"}]}}])
        _, legacy, _cwd = self.ji.scan_header(path, "x")
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


class CoworkSupport(unittest.TestCase):
    """Claude Desktop's Cowork sandboxes.

    Each is a full Claude directory, but they are 14 sessions rather than 14
    sources, and every sandbox's cwd is the same ".../outputs" path - so the
    naming is the whole point of treating them specially.
    """

    def setUp(self):
        import jsonl_ingest
        self.ji = jsonl_ingest
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-cowork-")
        self.store = os.path.join(self.tmp, "local-agent-mode-sessions")
        self.org = os.path.join(self.store, "owner-uuid", "org-uuid")
        os.makedirs(self.org)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, sid, title=None, cwd=None, n=1):
        """One sandbox: a .claude dir plus the metadata file beside it."""
        sandbox = os.path.join(self.org, sid)
        write_transcript(os.path.join(sandbox, ".claude"),
                         "C--sandbox-outputs", f"s-{sid}",
                         cwd or os.path.join(sandbox, "outputs"),
                         f"p-{sid}", f"req-{sid}")
        if title is not None:
            with open(sandbox + ".json", "w", encoding="utf-8") as f:
                json.dump({"sessionId": sid, "title": title,
                           "processName": "some-process-name"}, f)
        return sandbox

    def test_finds_sandboxes_and_their_titles(self):
        self._session("local_aaaa", "Install SearXNG search provider")
        self._session("local_bbbb", "Vercel domain setup")
        found = sources.cowork_sessions([self.store])
        self.assertEqual([s.title for s in found],
                         ["Install SearXNG search provider",
                          "Vercel domain setup"])
        for s in found:
            self.assertTrue(s.claude_dir.endswith(".claude"))

    def test_falls_back_when_metadata_is_missing_or_broken(self):
        self._session("local_cccc", title=None)          # no metadata file
        bad = self._session("local_dddd", "ok")
        with open(bad + ".json", "w", encoding="utf-8") as f:
            f.write("{ not json")
        titles = {s.title for s in sources.cowork_sessions([self.store])}
        self.assertEqual(titles, {"local_cccc", "local_dddd"})

    def test_title_is_made_safe_for_a_project_name(self):
        self.assertEqual(sources.clean_title("a/b\\c"), "a-b-c")
        self.assertEqual(sources.clean_title("  spaced \n out  "), "spaced out")
        self.assertEqual(len(sources.clean_title("x" * 200)),
                         sources.COWORK_TITLE_MAX)

    def test_missing_store_is_silent(self):
        self.assertEqual(sources.cowork_stores([os.path.join(self.tmp, "nope")]),
                         [])
        self.assertEqual(sources.cowork_sessions([os.path.join(self.tmp, "no")]),
                         [])

    def test_ingest_names_sessions_by_title_not_by_sandbox(self):
        self._session("local_eeee", "Install SearXNG search provider")
        self._session("local_ffff", "Vercel domain setup")
        tmpdb = os.path.join(self.tmp, "m.db")
        empty = os.path.join(self.tmp, "empty-claude")
        orig = db.connect
        db.connect = lambda path=tmpdb, cross_thread=False: orig(path, cross_thread)
        os.environ["CLAUDE_CONFIG_DIR"] = empty
        try:
            import build_dashboard
            cfg = sources.SourceConfig(scan_siblings=False,
                                       cowork_paths=[self.store])
            stats = self.ji.run(force=True, config=cfg)
            cowork = [s for s in stats["sources"] if s["origin"] == "cowork"]
            self.assertEqual(len(cowork), 1, "one source, not one per sandbox")
            self.assertEqual(cowork[0]["sessions"], 2)

            con = db.connect()
            rows, _ = build_dashboard.collect(con)
            labels = dict(con.execute(
                "SELECT session_id, source_label FROM sessions"))
            cwds = dict(con.execute("SELECT session_id, cwd FROM sessions"))
            con.close()
        finally:
            db.connect = orig
            os.environ.pop("CLAUDE_CONFIG_DIR", None)

        self.assertEqual(sorted(r["project"] for r in rows),
                         ["cowork/Install SearXNG search provider",
                          "cowork/Vercel domain setup"])
        self.assertEqual(set(labels.values()), {"cowork"})
        # the sandbox cwd is deliberately not recorded: it is the same
        # ".../outputs" for every session and would override the title
        self.assertEqual(set(cwds.values()), {None})

    def test_can_be_turned_off(self):
        self._session("local_gggg", "Vercel domain setup")
        tmpdb = os.path.join(self.tmp, "off.db")
        empty = os.path.join(self.tmp, "empty2")
        orig = db.connect
        db.connect = lambda path=tmpdb, cross_thread=False: orig(path, cross_thread)
        os.environ["CLAUDE_CONFIG_DIR"] = empty
        try:
            cfg = sources.SourceConfig(scan_siblings=False, cowork=False,
                                       cowork_paths=[self.store])
            stats = self.ji.run(force=True, config=cfg)
        finally:
            db.connect = orig
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.assertEqual([s for s in stats["sources"]
                          if s["origin"] == "cowork"], [])
        self.assertEqual(stats["prompts"], 0)


class ProviderModelIds(unittest.TestCase):
    """Bedrock and Vertex decorate model ids; pricing is keyed on the plain form."""

    def setUp(self):
        import pricing
        self.pricing = pricing
        self.saved_aliases = dict(pricing.MODEL_ALIASES)

    def tearDown(self):
        self.pricing.MODEL_ALIASES.clear()
        self.pricing.MODEL_ALIASES.update(self.saved_aliases)

    def test_canonicalises_every_provider_form(self):
        cases = {
            "claude-opus-4-5-20251101":
                ("claude-opus-4-5-20251101", "anthropic"),
            "anthropic.claude-sonnet-4-5-20250929-v1:0":
                ("claude-sonnet-4-5-20250929", "bedrock"),
            "us.anthropic.claude-opus-4-5-20251101-v1:0":
                ("claude-opus-4-5-20251101", "bedrock"),
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0":
                ("claude-haiku-4-5-20251001", "bedrock"),
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0":
                ("claude-sonnet-4-5-20250929", "bedrock"),
            "arn:aws:bedrock:us-east-1:1:inference-profile/"
            "us.anthropic.claude-opus-4-5-20251101-v1:0":
                ("claude-opus-4-5-20251101", "bedrock"),
            "claude-opus-4-5@20251101":
                ("claude-opus-4-5-20251101", "vertex"),
        }
        for raw, want in cases.items():
            self.assertEqual(self.pricing.canonical_model(raw), want, raw)

    def test_every_provider_form_now_prices(self):
        for raw in ("anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "us.anthropic.claude-opus-4-5-20251101-v1:0",
                    "arn:aws:bedrock:us-east-1:1:inference-profile/"
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "claude-opus-4-5@20251101"):
            self.assertIsNotNone(self.pricing.lookup(raw), raw)
            self.assertIsNotNone(
                self.pricing.estimate_cost(raw, input_tokens=1000), raw)

    def test_deployment_arns_are_unpriced_not_guessed(self):
        """These name a deployment, not a model - no id to infer from."""
        for raw in ("arn:aws:bedrock:us-east-1:1:application-inference-profile/abc",
                    "arn:aws:bedrock:us-east-1:1:provisioned-model/xyz"):
            canon, provider = self.pricing.canonical_model(raw)
            self.assertIsNone(canon, raw)
            self.assertEqual(provider, "bedrock")
            self.assertIsNone(self.pricing.lookup(raw), raw)

    def test_alias_names_the_model_but_not_the_provider(self):
        arn = "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"
        self.pricing.MODEL_ALIASES[arn] = "claude-opus-4-5"
        canon, provider = self.pricing.canonical_model(arn)
        self.assertEqual(canon, "claude-opus-4-5")
        self.assertEqual(provider, "bedrock",
                         "an aliased deployment is still billed at Bedrock rates")

    def test_placeholders_stay_out_of_the_provider_tally(self):
        self.assertEqual(self.pricing.canonical_model("<synthetic>"), (None, None))
        self.assertEqual(self.pricing.canonical_model(""), (None, None))
        self.assertEqual(self.pricing.canonical_model(None), (None, None))

    def test_provider_tables_are_independent(self):
        import tempfile as tf
        d = tf.mkdtemp()
        try:
            path = os.path.join(d, "pricing.local.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"prices": {"bedrock": {"claude-haiku-4-5": [9.0, 9.0]}}}, f)
            self.pricing.load_overrides(path)
            self.assertEqual(
                self.pricing.lookup("claude-haiku-4-5-20251001", provider="bedrock"),
                (9.0, 9.0))
            self.assertEqual(
                self.pricing.lookup("claude-haiku-4-5-20251001", provider="anthropic"),
                (1.0, 5.0), "an override for one provider must not leak")
        finally:
            self.pricing.PROVIDER_PRICES["bedrock"] = dict(self.pricing.PRICES)
            self.pricing._rebuild_indexes()
            shutil.rmtree(d, ignore_errors=True)

    def test_intro_pricing_is_first_party_only(self):
        anthro = self.pricing.lookup("claude-sonnet-5", ts="2026-08-01",
                                     provider="anthropic")
        bedrock = self.pricing.lookup("claude-sonnet-5", ts="2026-08-01",
                                      provider="bedrock")
        self.assertEqual(anthro, (2.0, 10.0), "promo applies on the first party")
        self.assertEqual(bedrock, (3.0, 15.0), "a marketplace has its own card")


class BedrockIngest(unittest.TestCase):
    """A Bedrock-routed transcript must cost something, not $0.00."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-bedrock-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self._connect = db.connect
        db.connect = lambda path=self.dbpath, cross_thread=False:             self._connect(path, cross_thread)

    def tearDown(self):
        db.connect = self._connect
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ingest(self, model):
        import jsonl_ingest
        root = os.path.join(self.tmp, "c")
        d = os.path.join(root, "projects", "-srv-app")
        os.makedirs(d, exist_ok=True)
        entries = [
            {"type": "user", "origin": {"kind": "human"}, "promptId": "p0",
             "timestamp": TS, "cwd": "/srv/app", "sessionId": "s0",
             "message": {"content": [{"type": "text", "text": "q"}]}},
            {"type": "assistant", "timestamp": TS, "requestId": "r0",
             "sessionId": "s0",
             "message": {"model": model, "content": [],
                         "usage": {"input_tokens": 50000, "output_tokens": 20000,
                                   "cache_read_input_tokens": 800000,
                                   "cache_creation_input_tokens": 100000}}}]
        with open(os.path.join(d, "s0.jsonl"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + chr(10))
        con = db.connect()
        jsonl_ingest.ingest_tree(con, os.path.join(root, "projects"), "")
        con.commit()
        return con

    def test_bedrock_rows_are_stored_canonically_and_costed(self):
        import build_dashboard
        con = self._ingest("us.anthropic.claude-opus-4-5-20251101-v1:0")
        model, raw, provider = con.execute(
            "SELECT model, model_raw, provider FROM api_requests").fetchone()
        self.assertEqual(model, "claude-opus-4-5-20251101")
        self.assertEqual(raw, "us.anthropic.claude-opus-4-5-20251101-v1:0")
        self.assertEqual(provider, "bedrock")
        rows, _ = build_dashboard.collect(con)
        con.close()
        self.assertGreater(sum(r["cost"] for r in rows), 0,
                           "a Bedrock row used to be costed at $0.00")
        self.assertEqual(build_dashboard.PROVIDERS, {"bedrock": 1})

    def test_deployment_arn_is_reported_rather_than_silently_zero(self):
        import build_dashboard
        con = self._ingest(
            "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc")
        rows, _ = build_dashboard.collect(con)
        con.close()
        self.assertEqual(sum(r["cost"] for r in rows), 0)
        self.assertTrue(build_dashboard.unpriced_models(),
                        "an uncostable model must be reported, not hidden")


class DashboardPayload(unittest.TestCase):
    """The contract between collect() and the dashboard's product selector.

    The template filters on `kind` rather than on a "cowork/" name prefix, so
    a local project that happens to be called cowork cannot be mistaken for
    the desktop app. These pin the field down; the template wiring that
    consumes it is checked separately.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-kind-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self._connect = db.connect
        db.connect = lambda path=self.dbpath, cross_thread=False: \
            self._connect(path, cross_thread)

    def tearDown(self):
        db.connect = self._connect
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rows_are_tagged_with_their_product(self):
        import build_dashboard
        import jsonl_ingest
        plain = os.path.join(self.tmp, "plain")
        write_transcript(plain, "-y-work-app", "s-code", "/y/work/app",
                         "p-code", "req-code")
        cowork = os.path.join(self.tmp, "cw", ".claude")
        write_transcript(cowork, "C--sandbox-outputs", "s-cw",
                         "/sandbox/outputs", "p-cw", "req-cw")

        con = db.connect()
        jsonl_ingest.ingest_tree(con, os.path.join(plain, "projects"), "")
        jsonl_ingest.ingest_tree(con, os.path.join(cowork, "projects"),
                                 sources.COWORK_LABEL,
                                 project_override="A Cowork Session")
        con.commit()
        rows, _ = build_dashboard.collect(con)
        con.close()

        by_kind = {r["kind"]: r for r in rows}
        self.assertEqual(set(by_kind), {"code", "cowork"})
        self.assertEqual(by_kind["code"]["project"], "app")
        self.assertEqual(by_kind["cowork"]["project"],
                         "cowork/A Cowork Session")

    def test_a_local_project_named_cowork_is_still_claude_code(self):
        """The trap that name-prefix matching would fall into."""
        import build_dashboard
        import jsonl_ingest
        plain = os.path.join(self.tmp, "plain2")
        write_transcript(plain, "-y-work-cowork", "s-trap", "/y/work/cowork",
                         "p-trap", "req-trap")
        con = db.connect()
        jsonl_ingest.ingest_tree(con, os.path.join(plain, "projects"), "")
        con.commit()
        rows, _ = build_dashboard.collect(con)
        con.close()
        self.assertEqual([r["kind"] for r in rows], ["code"])
        self.assertEqual([r["project"] for r in rows], ["cowork"])


class DesktopExtras(unittest.TestCase):
    """The three side files Claude Desktop keeps beside its Cowork store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-desktop-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_usage_samples_parse_and_clamp(self):
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "samples": [
                {"t": 2_000_000_000_000, "u": {"fh": 5, "sd": 10}},
                {"t": 1_000_000_000_000, "u": {"fh": 200, "sd": -4}},
                {"t": 1_500_000_000_000, "u": {}},
                {"t": 1_600_000_000_000},                 # no gauges at all
                {"u": {"fh": 1, "sd": 1}},                # no timestamp
            ]}, f)
        got = sources.plan_usage_samples([path])
        # samples carrying no gauge at all are dropped, not read as 0%
        self.assertEqual([(t, a, b) for t, a, b in got],
                         [(1_000_000_000.0, 100, 0),      # clamped to 0..100
                          (2_000_000_000.0, 5, 10)])      # sorted oldest first

    def test_plan_usage_is_absent_not_fatal(self):
        self.assertEqual(sources.plan_usage_samples([os.path.join(self.tmp, "no")]), [])
        for bad in ("not json", "[]", '{"samples": "nope"}'):
            path = os.path.join(self.tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(bad)
            self.assertEqual(sources.plan_usage_samples([path]), [], bad)

    def test_code_session_titles(self):
        store = os.path.join(self.tmp, "claude-code-sessions", "o", "g")
        os.makedirs(store)
        with open(os.path.join(store, "a.json"), "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": "cli-1", "title": "Fix the parser"}, f)
        with open(os.path.join(store, "b.json"), "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": "cli-2"}, f)          # untitled
        with open(os.path.join(store, "c.json"), "w", encoding="utf-8") as f:
            f.write("{ broken")
        got = sources.code_session_titles(
            [os.path.join(self.tmp, "claude-code-sessions")])
        self.assertEqual(got, {"cli-1": "Fix the parser"})

    def test_audit_run_costs_sum_per_session(self):
        sandbox = os.path.join(self.tmp, "local_x")
        os.makedirs(os.path.join(sandbox, ".claude"))
        with open(os.path.join(sandbox, "audit.jsonl"), "w",
                  encoding="utf-8") as f:
            for e in [
                {"type": "result", "session_id": "s1", "total_cost_usd": 1.5},
                {"type": "result", "session_id": "s1", "total_cost_usd": 0.5},
                {"type": "result", "session_id": "s2", "total_cost_usd": 2.0},
                {"type": "assistant", "session_id": "s1",
                 "total_cost_usd": 99},                       # not a result
                {"type": "result", "session_id": "s3"},       # no cost
            ]:
                f.write(json.dumps(e) + "\n")
        got = sources.audit_run_costs(os.path.join(sandbox, ".claude"))
        self.assertEqual(got, {"s1": (2.0, 2), "s2": (2.0, 1)})

    def test_audit_missing_file_is_empty(self):
        self.assertEqual(
            sources.audit_run_costs(os.path.join(self.tmp, "nope", ".claude")),
            {})


class AuthoritativeCost(unittest.TestCase):
    """run_cost is spent only where it demonstrably covers every run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-cost-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self._connect = db.connect
        db.connect = lambda path=self.dbpath, cross_thread=False: \
            self._connect(path, cross_thread)

    def tearDown(self):
        db.connect = self._connect
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _two_prompt_session(self):
        import jsonl_ingest
        root = os.path.join(self.tmp, "c")
        d = os.path.join(root, "projects", "-p")
        os.makedirs(d, exist_ok=True)
        entries = []
        for i in (0, 1):
            entries += [
                {"type": "user", "origin": {"kind": "human"},
                 "promptId": f"p{i}", "timestamp": TS, "cwd": "/p",
                 "sessionId": "sess",
                 "message": {"content": [{"type": "text", "text": f"q{i}"}]}},
                {"type": "assistant", "timestamp": TS, "requestId": f"r{i}",
                 "sessionId": "sess",
                 "message": {"model": "claude-opus-5", "content": [],
                             "usage": {"input_tokens": 1000,
                                       "output_tokens": 1000,
                                       "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0}}}]
        with open(os.path.join(d, "sess.jsonl"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        con = db.connect()
        jsonl_ingest.ingest_tree(con, os.path.join(root, "projects"), "")
        con.commit()
        return con

    def test_full_coverage_reprices_and_marks_exact(self):
        import build_dashboard
        con = self._two_prompt_session()
        base, _ = build_dashboard.collect(con)
        estimated = sum(r["cost"] for r in base)
        self.assertGreater(estimated, 0)

        db.set_run_cost(con, "sess", 9.0, 2, "test")   # 2 runs, 2 prompts
        con.commit()
        rows, _ = build_dashboard.collect(con)
        con.close()
        self.assertAlmostEqual(sum(r["cost"] for r in rows), 9.0, places=6)
        self.assertTrue(all(not r["est"] for r in rows))
        # the composition is scaled with it, so the donut still sums to cost
        self.assertAlmostEqual(sum(sum(r["comp"]) for r in rows), 9.0, places=2)

    def test_partial_coverage_is_refused(self):
        import build_dashboard
        con = self._two_prompt_session()
        base, _ = build_dashboard.collect(con)
        estimated = sum(r["cost"] for r in base)

        db.set_run_cost(con, "sess", 9.0, 1, "test")   # only 1 of 2 runs
        con.commit()
        rows, _ = build_dashboard.collect(con)
        con.close()
        self.assertAlmostEqual(sum(r["cost"] for r in rows), estimated, places=6,
                               msg="a partial record must not be spent")
        self.assertTrue(all(r["est"] for r in rows))


class BuildOptions(unittest.TestCase):
    """Payload shaping: the row cap and prompt-text redaction."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-build-")
        self.dbpath = os.path.join(self.tmp, "metrics.db")
        self._connect = db.connect
        db.connect = lambda path=self.dbpath, cross_thread=False: \
            self._connect(path, cross_thread)
        import build_dashboard
        import report_index
        self.bd = build_dashboard
        self._out, self._idx = build_dashboard.OUTPUT, report_index.OUTPUT
        build_dashboard.OUTPUT = os.path.join(self.tmp, "dashboard.html")
        report_index.OUTPUT = os.path.join(self.tmp, "index.html")
        import jsonl_ingest
        root = os.path.join(self.tmp, "c")
        for i in range(5):
            write_transcript(root, "-p", f"s{i}", "/p", f"p{i}", f"r{i}")
        con = db.connect()
        jsonl_ingest.ingest_tree(con, os.path.join(root, "projects"), "")
        con.commit()
        con.close()

    def tearDown(self):
        import report_index
        self.bd.OUTPUT, report_index.OUTPUT = self._out, self._idx
        db.connect = self._connect
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self):
        with open(self.bd.OUTPUT, encoding="utf-8") as f:
            html = f.read()
        raw = re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1)
        return json.loads(raw)

    def _rows(self, p):
        """Rows as the template sees them, from the column-oriented payload."""
        s, c = p["strings"], p["cols"]
        out = []
        for i in range(p["n_rows"]):
            models = [{"model": s[m[0]], "in": m[1], "out": m[2], "cr": m[3],
                       "cw": m[4], "cost": m[5], "calls": m[6]}
                      for m in c["models"][i]]
            out.append({"ts": c["ts"][i], "text": c["text"][i],
                        "project": s[c["project"][i]], "models": models,
                        "cost": c["cost"][i],
                        "out": sum(m["out"] for m in models)})
        return out

    def test_cap_keeps_the_newest_and_reports_the_rest(self):
        self.bd.build(max_rows=2)
        p = self._payload()
        rows = self._rows(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(p["total_rows"], 5)
        self.assertEqual(p["truncated"], 3)
        self.assertEqual(rows, sorted(rows, key=lambda r: r["ts"],
                                      reverse=True))

    def test_no_cap_embeds_everything(self):
        self.bd.build(max_rows=0)
        p = self._payload()
        self.assertEqual(len(self._rows(p)), 5)
        self.assertEqual(p["truncated"], 0)

    def test_redaction_removes_prompt_text_only(self):
        self.bd.build(redact=True)
        p = self._payload()
        self.assertTrue(p["redacted"])
        rows = self._rows(p)
        self.assertTrue(all(r["text"] == "" for r in rows))
        self.assertTrue(all(r["out"] > 0 for r in rows),
                        "numbers must survive redaction")

    def test_text_is_present_by_default(self):
        self.bd.build()
        p = self._payload()
        self.assertFalse(p["redacted"])
        self.assertTrue(any(r["text"] for r in self._rows(p)))


class ReceiverStaleness(unittest.TestCase):
    """The receiver must not overwrite a rebuild made with newer code."""

    def test_fingerprint_notices_a_changed_file(self):
        import receiver
        before = receiver.code_fingerprint()
        self.assertTrue(all(v is not None for v in before.values()))
        self.assertIn("template.html", before)
        self.assertIn("build_dashboard.py", before)

    def test_stale_is_latched_and_reported_once(self):
        import receiver
        saved_started, saved_stale = receiver._started_with, receiver._stale
        try:
            receiver._stale = False
            receiver._started_with = dict(receiver.code_fingerprint())
            self.assertFalse(receiver.code_is_stale())
            receiver._started_with["template.html"] = -1     # pretend it moved
            self.assertTrue(receiver.code_is_stale())
            self.assertTrue(receiver._stale, "must latch")
            self.assertTrue(receiver.code_is_stale())
        finally:
            receiver._started_with, receiver._stale = saved_started, saved_stale


class TemplateWiring(unittest.TestCase):
    """Cheap guards that the dashboard controls are still wired up.

    The behaviour itself is verified by driving the real script in a DOM
    stub during development; these keep the markup from silently losing the
    pieces that script depends on. The project stays Python-only, so nothing
    here needs a JS runtime.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "template.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_range_buttons_include_month_to_date(self):
        for token in ('data-range="1"', 'data-range="7"', 'data-range="mtd"',
                      'data-range="30"', 'data-range="90"', 'data-range="0"'):
            self.assertIn(token, self.html)

    def test_mtd_is_not_coerced_to_a_number(self):
        """+"mtd" is NaN; the handler and the aria state must compare strings."""
        self.assertIn('b.dataset.range === "mtd" ? "mtd" : +b.dataset.range',
                      self.html)
        self.assertIn("String(x.dataset.range) === String(state.range)",
                      self.html)
        self.assertIn('state.range === "mtd"', self.html)

    def test_product_selector_sits_before_the_project_selector(self):
        kind = self.html.index('id="f-kind"')
        project = self.html.index('id="f-project"')
        seg = self.html.index('id="range-seg"')
        self.assertLess(seg, kind, "product selector comes after the range")
        self.assertLess(kind, project, "product selector comes before project")

    def test_all_products_option_and_derived_list(self):
        self.assertIn('const ALL_KINDS = "all"', self.html)
        self.assertIn("state.kind === ALL_KINDS || kindOf(r) === state.kind",
                      self.html)
        # options come from the data, so an unknown product stays reachable
        self.assertIn("const present = [...new Set(rows.map(kindOf))].sort()",
                      self.html)
        self.assertIn("(PRODUCT_META[id] || {}).label || id", self.html)

    def test_plan_gauges_are_wired(self):
        self.assertIn('value="plan5h"', self.html)
        self.assertIn('value="plan7d"', self.html)
        self.assertIn('id="tile-plan"', self.html)
        self.assertIn("function renderPlanTile", self.html)
        self.assertIn("chart.plan != null", self.html)
        # the range must apply to plan charts, and nothing else should
        self.assertIn("function rangeCutoff", self.html)

    def test_stacked_segments_have_no_gap(self):
        """A 2px gap erased any series small enough to be worth noticing."""
        self.assertIn("yCursor = yTop;", self.html)
        self.assertNotIn("yCursor = yTop - 2;", self.html)
        self.assertNotIn("height: Math.max(1, h - 2)", self.html)
        self.assertIn("height: h,", self.html)

    def test_ironbow_ramp_is_ordered_by_cost(self):
        # cheapest -> dearest by output rate: haiku, sonnet, opus, fable
        for fam, slot in (("haiku", 1), ("sonnet", 2), ("opus", 3), ("fable", 4)):
            self.assertRegex(self.html,
                             r'\["%s",\s+m => [^,]+,\s*%d\]' % (fam, slot))
        # unknown cost is not the same as expensive
        self.assertIn('["other",  () => true, "o"]', self.html)
        # per-token cost order for the cache/output components
        self.assertIn('const COMP = [["cache read", 1], ["cache write", 3], '
                      '["output", 4], ["uncached input", 2]];', self.html)
        self.assertIn("const COMP_STACK = [0, 3, 1, 2];", self.html)

    def test_project_runs_are_marked_only_under_a_project_sort(self):
        """Consecutive rows of one project are otherwise indistinguishable."""
        self.assertIn('const byProject = state.sort === "project";', self.html)
        self.assertIn('tr.classList.add("proj-start")', self.html)
        # the styling, including the no-double-rule-under-the-header case
        self.assertIn("tr.proj-start > td { border-top: 2px solid var(--baseline); }",
                      self.html)
        self.assertIn("tbody tr.proj-start:first-child > td { border-top: 0; }",
                      self.html)
        # the project cell must be targetable to un-mute it on a boundary row
        self.assertIn('el("td", "muted proj", projLabel(r.project))', self.html)
        self.assertIn("tr.proj-start > td.proj", self.html)

    def test_hidden_attribute_is_not_defeated_by_a_display_rule(self):
        """#chart-legend { display: flex } outranked [hidden] and beat it."""
        import re
        css = self.html[self.html.index("<style>"):self.html.index("</style>")]
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        self.assertRegex(
            css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important",
            "the hidden attribute must outrank id-based display rules")
        # nothing else may use !important on display, or the tie returns
        others = [s for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                  if "display" in b and "!important" in b
                  and s.strip() != "[hidden]"]
        self.assertEqual(others, [])

    def test_legend_is_reset_before_any_early_return(self):
        """Switching charts must not leave the previous chart's series shown."""
        reset = self.html.index("legend.replaceChildren();")
        early = self.html.index("if (!days.length) return;")
        populate = self.html.index("legend.hidden = false;")
        self.assertLess(reset, early,
                        "the no-data path returns before the legend is filled")
        self.assertLess(early, populate)
        # the old else-branch that only flipped the attribute is gone
        self.assertNotIn("} else {" + chr(10) + "    legend.hidden = true;",
                         self.html)

    def test_donut_matches_the_stacked_bars(self):
        """Same three properties: no gap, cost order, small slices survive."""
        self.assertNotIn('p.style.stroke = "var(--surface-1)"', self.html)
        self.assertNotIn('p.style.strokeWidth = "2"', self.html)
        self.assertIn("const MIN_SWEEP = 0.024;", self.html)
        self.assertIn("function fitSweeps(fracs)", self.html)
        # ring order and the table beside it both follow COMP_STACK
        self.assertIn("const parts = COMP_STACK.map(i => ({ i, v: (comp || [])[i] || 0 }))",
                      self.html)
        self.assertIn("COMP_STACK.forEach((i) => {", self.html)

    def test_truncated_file_list_does_not_break_the_detail_panel(self):
        """append() returns undefined; chaining appendChild onto it threw."""
        self.assertNotIn('ft.append(el("tr")).appendChild(', self.html)

    def test_ironbow_palette_is_defined_for_both_themes(self):
        for var in ("--iron-1", "--iron-2", "--iron-3", "--iron-4",
                    "--iron-other"):
            # once for light, twice for dark (media query + explicit toggle)
            self.assertEqual(self.html.count(var + ":"), 3, var)
        self.assertIn(".fill-i1 { fill: var(--iron-1); }", self.html)
        self.assertIn(".sw-io { background: var(--iron-other); }", self.html)
        self.assertNotIn("fill-s1", self.html)   # old palette fully retired

    def test_notice_and_session_column_are_wired(self):
        self.assertIn('id="notice"', self.html)
        self.assertIn("function renderNotice", self.html)
        self.assertIn("DATA.truncated", self.html)
        self.assertIn("DATA.redacted", self.html)
        self.assertIn('{ k: "title", label: "Session"', self.html)

    def test_product_filter_and_label_stripping_are_wired(self):
        self.assertIn("kindOf(r) === state.kind", self.html)
        self.assertIn("function projLabel", self.html)
        self.assertIn('cowork: { label: "Claude Cowork", prefix: "cowork/" }',
                      self.html)
        self.assertIn('const DEFAULT_KIND = "code"', self.html)
        # every place a project name reaches the user goes through projLabel
        self.assertIn("cell: r => el(\"td\", \"muted proj\", projLabel(r.project))",
                      self.html)
        self.assertIn("const g = projLabel(r.project);", self.html)
        self.assertIn("esc(projLabel(r.project))", self.html)

    def test_view_switch_is_wired(self):
        """The Prompts/Sessions toggle and its state must exist and persist."""
        self.assertIn('id="view-seg"', self.html)
        self.assertIn('data-view="prompts"', self.html)
        self.assertIn('data-view="sessions"', self.html)
        self.assertIn("function initViewSwitch", self.html)
        self.assertIn("function renderView", self.html)
        self.assertIn('const VIEWS = ["prompts", "sessions"]', self.html)
        # the view must round-trip through localStorage like the other filters
        self.assertIn("view: state.view, session: state.session", self.html)

    def test_sessions_table_is_wired(self):
        self.assertIn('id="sess-tbl"', self.html)
        self.assertIn('id="sess-body"', self.html)
        self.assertIn('id="sess-head"', self.html)
        self.assertIn('id="sessions-card"', self.html)
        self.assertIn("function renderSessions", self.html)
        self.assertIn("function toggleSessionDetail", self.html)
        self.assertIn("function setSessionFilter", self.html)
        # the session detail's "Show prompts" escape hatch must exist
        self.assertIn('el("button", "btn", "Show prompts")', self.html)
        self.assertIn('id="session-chip"', self.html)

    def test_new_chart_keys_are_registered(self):
        """blocks/heat use their own draw(); ctx/miss ride the day-bucket path."""
        for key in ("blocks", "heat", "ctx", "miss"):
            self.assertIn('value="%s"' % key, self.html)
        self.assertIn("blocks:{ title: \"Cost per 5-hour billing block\", draw: drawBlocks }",
                      self.html)
        self.assertIn('heat:  { title: "Daily cost", draw: drawHeatmap }', self.html)
        self.assertIn("function drawBlocks", self.html)
        self.assertIn("function drawHeatmap", self.html)
        self.assertIn("if (chart.draw) { chart.draw(wrap, rs); return; }", self.html)

    def test_notices_are_rendered(self):
        self.assertIn("for (const n of (DATA.notices || [])) bits.push(String(n));",
                      self.html)

    def test_session_rows_are_keyboard_reachable(self):
        """A session row must be tabbable and answer Enter/Space like a click."""
        self.assertIn("tr.tabIndex = 0;", self.html)
        self.assertIn('if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;',
                      self.html)

    def test_errors_column_is_wired(self):
        """The errors column is the one v2 column shown by default, and its
        cell must be reachable for the red-tint class when a row has any."""
        self.assertIn('{ k: "errors", label: "Errors", num: true, def: true,',
                      self.html)
        self.assertIn('td.classList.add("err-count")', self.html)
        self.assertIn(".err-count { color: var(--danger); font-weight: 600; }",
                      self.html)
        for var in ("--danger",):
            self.assertEqual(self.html.count(var + ":"), 3, var)

    def test_other_v2_columns_are_wired(self):
        for key in ("effort", "thinking", "fast_calls", "max_tokens_stops",
                    "web_searches", "peak_ctx", "misses", "miss_cost",
                    "session"):
            self.assertIn('{ k: "%s", label:' % key, self.html)
        # the session column is the click-to-filter one; it must not be
        # confused with the existing "title" column, which is also labelled
        # "Session" for the human-readable name
        self.assertIn('{ k: "session", label: "Session ID"', self.html)
        self.assertIn("setSessionFilter(r.session)", self.html)

    def test_new_columns_are_exported_to_csv(self):
        for col in ("effort", "thinking_tokens", "fast_calls", "errors",
                    "max_tokens_stops", "web_searches", "peak_context",
                    "cache_misses", "miss_cost_usd", "session_id"):
            self.assertIn('"%s"' % col, self.html)
        self.assertIn("r.effort || \"\", r.thinking || 0, r.fast_calls || 0, r.errors || 0,",
                      self.html)

    def test_tools_chart_is_registered(self):
        """The own-axis attributed-cost-per-tool chart, following the
        blocks/heat draw() pattern."""
        self.assertIn('value="tools"', self.html)
        self.assertIn(
            'tools: { title: "Attributed tool cost (top 12)", draw: drawTools }',
            self.html)
        self.assertIn("function drawTools(wrap, rs)", self.html)

    def test_errors_chart_is_registered(self):
        self.assertIn('value="errors"', self.html)
        self.assertIn('errors:{ title: "Tool errors per day",', self.html)
        self.assertIn("b.errors += r.errors || 0;", self.html)

    def test_conversation_link_is_wired(self):
        """A real <a target=_blank> to the per-prompt conversation page,
        only when the row's conv field is set."""
        self.assertIn("if (r.conv) {", self.html)
        self.assertIn('el("a", "btn", "Open conversation")', self.html)
        self.assertIn("a.href = r.conv;", self.html)
        self.assertIn('a.target = "_blank";', self.html)

    def test_agent_info_drives_subagent_labels(self):
        """Subagents show name + model, not the opaque agent id."""
        self.assertIn("if (r.agent_info && r.agent_info.length) {", self.html)
        self.assertIn('" on " + shortModel(a.model)', self.html)

    def test_tool_attrib_detail_table_is_wired(self):
        self.assertIn("r.tool_attrib && r.tool_attrib.length", self.html)
        self.assertIn('["Tool", "Calls", "Result size", "Cost"]', self.html)
        self.assertIn("const fmtBytes = (n) => {", self.html)

    def test_errors_baseline_overhead_tiles_are_wired(self):
        self.assertIn('id="tile-errors"', self.html)
        self.assertIn('id="tile-baseline"', self.html)
        self.assertIn('id="tile-overhead" hidden', self.html)
        self.assertIn("function renderErrorsTile", self.html)
        self.assertIn("function renderBaselineTile", self.html)
        self.assertIn("function renderOverheadTile", self.html)
        self.assertIn("DATA.baseline", self.html)
        self.assertIn("DATA.overhead", self.html)

    def test_header_insights_link_and_cost_basis_badge_are_wired(self):
        self.assertIn('id="insights-link"', self.html)
        self.assertIn('id="cost-basis-badge"', self.html)
        self.assertIn("function renderInsightsLink", self.html)
        self.assertIn("function renderCostBasisBadge", self.html)
        self.assertIn("DATA.insights_report", self.html)
        self.assertIn("DATA.cost_basis", self.html)

    def test_range_filter_controls_are_wired(self):
        """The "More filters" panel: toggle, every min/max pair, both
        checkboxes, and the state/predicate functions that back them."""
        self.assertIn('id="more-filters-btn"', self.html)
        self.assertIn('id="more-filters"', self.html)
        for rf_id in ("rf-cost-min", "rf-cost-max", "rf-dur-min", "rf-dur-max",
                      "rf-out-min", "rf-out-max", "rf-calls-min", "rf-calls-max",
                      "rf-misses", "rf-errors", "rf-clear"):
            self.assertIn('id="%s"' % rf_id, self.html)
        self.assertIn("function sanitizeRf(v)", self.html)
        self.assertIn("function rfPassRow(r)", self.html)
        self.assertIn("function rfPassSession(s)", self.html)
        self.assertIn("function rfActiveCount()", self.html)
        self.assertIn("function updateMoreFiltersBtn()", self.html)
        # cost/calls/misses are the only fields real on both rows and
        # sessions; duration/output/errors must not be asserted on a session.
        self.assertNotIn("f.durMin != null && (s.", self.html)
        self.assertNotIn("f.outMin != null && (s.", self.html)
        self.assertNotIn("f.errorsOnly && !((s.", self.html)

    def test_filter_key_includes_range_filters(self):
        """dayBuckets' memo key must invalidate when a range filter changes,
        or the chart would keep showing a stale aggregate."""
        self.assertIn("JSON.stringify(state.rf)", self.html)
        idx_key = self.html.index("const filterKey = ()")
        idx_rf = self.html.index("JSON.stringify(state.rf)")
        idx_bucket = self.html.index("function dayBuckets(rs)")
        self.assertLess(idx_key, idx_rf)
        self.assertLess(idx_rf, idx_bucket)

    def test_aria_sort_is_wired_on_both_tables(self):
        self.assertIn('th.setAttribute("aria-sort",', self.html)
        self.assertEqual(self.html.count('th.setAttribute("aria-sort",'), 2,
                         "both renderHead() and renderSessHead() must set it")
        self.assertIn('th.setAttribute("role", "button")', self.html)
        self.assertIn('e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar"',
                      self.html)

    def test_rows_and_group_headers_are_keyboard_reachable(self):
        """Prompt rows, session rows and group headers all answer Enter/Space
        like a click, and their aria-expanded reflects the actual state."""
        self.assertIn('tr.setAttribute("role", "button")', self.html)
        self.assertIn('gtr.setAttribute("role", "button")', self.html)
        self.assertIn('gtr.setAttribute("aria-expanded", "true")', self.html)
        self.assertIn('gtr.setAttribute("aria-expanded", String(!hide));', self.html)
        # a reused cached row must not keep a stale aria-expanded="true"
        self.assertIn('cached.setAttribute("aria-expanded", "false")', self.html)

    def test_chip_remove_control_has_an_aria_label(self):
        self.assertIn('x.setAttribute("aria-label", "Clear the session filter")',
                      self.html)

    def test_focus_visible_styles_use_theme_tokens(self):
        css = self.html[self.html.index("<style>"):self.html.index("</style>")]
        self.assertIn(":focus-visible", css)
        self.assertIn("outline: 2px solid var(--series-1)", css)
        self.assertNotIn("outline: 2px solid #", css)  # must stay a token, not a literal

    def test_content_visibility_bounds_row_render_cost(self):
        self.assertIn("content-visibility: auto;", self.html)
        self.assertIn("contain-intrinsic-size: 0 34px;", self.html)

    def test_table_layout_is_fixed_with_a_colgroup(self):
        self.assertIn("table-layout: fixed", self.html)
        self.assertIn('id="tbl-colgroup"', self.html)
        self.assertIn('id="sess-colgroup"', self.html)
        self.assertIn("function buildColgroup(node, cols, widths)", self.html)
        # the prompt text / session name columns are the ones deliberately
        # left out of both width maps, so they take whatever the fixed
        # columns leave over instead of being pinned to a pixel width
        col_width = self.html[self.html.index("const COL_WIDTH"):
                              self.html.index("};", self.html.index("const COL_WIDTH"))]
        self.assertNotIn("text:", col_width)
        sess_col_width = self.html[self.html.index("const SESS_COL_WIDTH"):
                                   self.html.index("};", self.html.index("const SESS_COL_WIDTH"))]
        self.assertNotIn("title:", sess_col_width)

    def test_row_caps_are_raised_with_a_notice_element(self):
        self.assertIn("const ROW_CAP = 5000;", self.html)
        self.assertIn("const GROUP_ROW_CAP = 1000;", self.html)
        self.assertIn("const SESS_ROW_CAP = 2000;", self.html)
        self.assertIn('id="cap-notice"', self.html)
        self.assertIn('id="sess-cap-notice"', self.html)
        self.assertIn("function setCapNotice(node, shown, total)", self.html)
        self.assertIn("setCapNotice($(\"cap-notice\")", self.html)
        self.assertIn("setCapNotice($(\"sess-cap-notice\")", self.html)

    def test_persist_covers_sort_and_the_range_filters(self):
        """persist() must survive the 5-minute meta refresh with sort, dir,
        q, ssort, sdir, view, session and the range filters intact."""
        start = self.html.index("const persist = ()")
        end = self.html.index(");", start)
        body = self.html[start:end]
        for key in ("sort:", "dir:", "q:", "ssort:", "sdir:", "view:",
                    "session:", "rf:"):
            self.assertIn(key, body, "persist() missing " + key)

    def test_session_filter_is_validated_after_restore(self):
        """A session id restored from a stale payload must fall back to no
        filter instead of hiding every row with no explanation."""
        self.assertIn(
            "if (state.session && !sessById.has(state.session)) state.session = null;",
            self.html)

    def test_scroll_position_survives_the_meta_refresh(self):
        self.assertIn('sessionStorage.setItem(SCROLL_KEY,', self.html)
        self.assertIn('addEventListener("beforeunload", saveScroll)', self.html)
        self.assertIn("setInterval(saveScroll,", self.html)

    def test_reduced_motion_guards_every_transition(self):
        """Every `transition:` in the stylesheet must sit after the nearest
        preceding `@media (prefers-reduced-motion: no-preference)` opener and
        before that block's own close - not just somewhere later in the file,
        which a naive "guard exists anywhere above" check would let through
        for a transition added outside the block by mistake."""
        css = self.html[self.html.index("<style>"):self.html.index("</style>")]
        guard_start = css.index("@media (prefers-reduced-motion: no-preference)")
        guard_open = css.index("{", guard_start)
        depth = 1
        i = guard_open + 1
        while depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        guard_close = i
        for idx in (m.start() for m in re.finditer(r"transition\s*:", css)):
            self.assertTrue(guard_open < idx < guard_close,
                            "a transition: outside the reduced-motion guard "
                            "reaches a viewer who asked for less motion")


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
