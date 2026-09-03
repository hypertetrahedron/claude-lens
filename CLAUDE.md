# CLAUDE.md — Claude Lens

A local usage dashboard for Claude Code: transcripts and OTel telemetry in,
`metrics.db`, then a self-contained `dashboard.html`.

## Hard constraints

- **Python 3.9+, standard library only.** No third-party packages, ever — not
  for the app, not for the tests. A user should be able to clone and run.
- **`dashboard.html` is self-contained.** No CDN, no external fonts, no build
  step. `template.html` holds the CSS and JS; `build_dashboard.py` substitutes
  the JSON payload into `/*__DATA__*/null`.
- **Windows and Linux/macOS both matter.** Console output stays ASCII (a
  Windows console is often cp1252, and `print` under `pythonw` goes nowhere at
  all — never rely on stderr reaching a user).

## Running things

```
python test_sources.py          # the whole suite
python test_pricing.py          # rates, cache multipliers, fast mode, retirement
python test_receiver.py         # OTel attributes, dirty fingerprint, SessionEnd hook
python test_ingest.py           # transcript parsing, schema v8, --db
python jsonl_ingest.py          # ingest every configured source
python jsonl_ingest.py --db PATH          # ... into a database elsewhere
python build_dashboard.py       # rebuild dashboard.html + index.html
python digest.py                # weekly report into reports/
python jsonl_ingest.py --remote-status    # why a remote host is quiet
```

## The receiver will overwrite your work

`receiver.py` runs as a background service and rebuilds `dashboard.html` about
once a minute. **After editing `build_dashboard.py` or `template.html`,
restart it** — otherwise your rebuild is replaced by output from the code it
started with, which looks like your change did nothing.

It now detects this and stops rebuilding (logging that it needs a restart),
and `build_dashboard.py` prints a note when it sees the receiver listening.
Neither is a substitute for restarting it. On this machine that is the
`ClaudeMetricsReceiver` scheduled task.

## Storage rules

- `api_requests` is keyed by Anthropic `request_id`, `tool_calls`/`edits` by
  `tool_use_id`. OTel rows win on conflict (they carry the CLI's own cost).
- **A re-parse can raise a transcript row, never lower it** (schema v8). One
  API request is written to the transcript once per content block and the last
  one carries the complete usage, so `REQUEST_SQL_JSONL` updates on conflict
  only when `api_requests.source='jsonl'` and `excluded.output_tokens >=
  api_requests.output_tokens`. That is what makes clearing `ingest_state` a
  real repair: a row captured mid-stream is corrected by re-reading the file.
  A transcript still never touches an OTel row, and a row whose transcript has
  been deleted can only be corrected by an in-place `UPDATE` in a migration.
- `tool_calls`, `agents` and `sessions` merge rather than replace: each write
  fills what is NULL and leaves what is known alone, because the transcript
  and OTel each know a different half (sizes vs durations, requested vs
  resolved model). `session_events` has no natural key, so a unique index over
  the whole tuple is what keeps a re-ingest from multiplying it.
- Schema changes are a ritual: add `_migrate_to_N()`, register it in
  `MIGRATIONS`, bump `SCHEMA_VERSION`, update the `CREATE TABLE` text so fresh
  databases are born current, and add a changelog line **both** in `db.py` and
  in the README version table.
- Test any migration against a copy of a real `metrics.db` before letting it
  touch the live one.

## Correctness habits that this project cares about

- **Verify transcript facts against real transcripts.** The JSONL schema has
  changed across CLI versions and assumptions rot. Two examples that cost real
  data: prompts in older files carry no `origin` marker (so a whole session's
  usage was dropped), and `audit.jsonl` only records runs that finished (so
  trusting it hid two thirds of Cowork spend).
- **Never present a guess as a measurement.** Unknown cost is not zero cost —
  an unpriced model is named on the page and counted as $0.00 deliberately,
  and authoritative figures are only spent where they provably cover
  everything (see the `run_cost` coverage guard in `collect()`).
- **Say when data is partial.** Truncated row sets, redacted prompt text and
  unpriced models all surface in the dashboard's notice bar rather than
  silently changing the numbers.

## Testing the browser code

The repo is Python-only, so `template.html` has no JS test runner. The pattern
that works: extract the `<script>` from a built `dashboard.html` and run it in
a small DOM stub under `node` in the scratch directory, driving the real
functions against the real payload. **Do not commit that harness** — it would
make Node a prerequisite.

What *is* committed is `TemplateWiring` in `test_sources.py`: static assertions
that the markup and script still contain the wiring the behaviour depends on.

**The stub has no CSS and cannot catch cascade bugs.** A legend once stayed
visible because `#chart-legend { display: flex }` outranked the `hidden`
attribute, while the stub happily reported `hidden === true`. For anything
visual, reason about the cascade explicitly or check it in a browser.

## Docs

`README.md` (how to use it) and `ROADMAP.md` (what is done, what is deferred,
and why) are updated **in the same change as the code**, not afterwards.

`ROADMAP.md` is a single Markdown table — a blank line between rows silently
splits it into several tables and only the first keeps its headers. Deferred
entries need the reason and what would unblock them; when something deferred
gets built, delete the entry rather than leaving it contradicting the table.

## Privacy

Prompt text is embedded verbatim in `dashboard.html` (first 400 chars);
`--no-prompt-text` blanks it for sharing. `metrics.db`, the generated HTML,
`remote-cache/`, `sources.json` and `pricing.local.json` are all gitignored —
the last two hold machine names and account-specific rates. This is a public
repository: keep real hostnames, session titles and UUIDs out of the docs and
use generic examples.
