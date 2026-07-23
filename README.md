# Claude Lens

A local usage dashboard for Claude Code: per-prompt token/cost tracking for
all Claude Code sessions of the signed-in user. One dashboard row per prompt, with per-model input/output/cache tokens,
tool-call counts, file/line changes, cost (with cache-savings counterfactual),
and duration — subagent work and harness-injected turns folded into the prompt
that caused them. Only Claude Code is tracked; no other AI tools.

Works on Windows and Linux/macOS. No dependencies beyond Python 3.9+ (stdlib
only); the dashboard itself is a single self-contained HTML file.

## Quick start — generate the dashboard with one file

```
Windows:      .\generate-dashboard.ps1
Linux/macOS:  ./generate-dashboard.sh      (chmod +x once if needed)
```

That parses every Claude Code transcript under `~/.claude/projects`
(`CLAUDE_CONFIG_DIR` is honored if set), builds `metrics.db` and
`dashboard.html` next to the script, and opens the dashboard. Re-running is
incremental — only new/changed transcripts are parsed. Flags: `-Force` /
`--force` re-parses everything; `-NoOpen` / `--no-open` skips the browser.

In this one-shot mode all costs are estimated from the pricing table in
`pricing.py` (shown with a `~` prefix). That's the whole setup — everything
below is optional.

## Optional: live mode (OTel receiver)

For minute-fresh data and exact CLI-reported costs, run the receiver
(`python receiver.py`). It listens on `127.0.0.1:4318`, ingests Claude Code's
OpenTelemetry events live, re-runs the transcript reconciler hourly (heals any
gaps — transcripts on disk are the safety net), and rebuilds `dashboard.html`
within a minute of new data.

1. Enable telemetry for all sessions — add to `~/.claude/settings.json`:

```json
"env": {
  "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
  "OTEL_LOGS_EXPORTER": "otlp",
  "OTEL_METRICS_EXPORTER": "none",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
  "OTEL_LOG_USER_PROMPTS": "1"
}
```

   (Prompt text never leaves the machine — the receiver binds localhost only.
   Sessions started before enablement are still captured via transcripts.)

2. Keep the receiver running at login:
   - **Windows** — Task Scheduler: at-logon task running `pythonw receiver.py`
     in this directory, no execution time limit, restart on failure.
   - **Linux** — systemd user unit (`~/.config/systemd/user/claude-metrics.service`):

     ```ini
     [Unit]
     Description=Claude Code usage metrics receiver
     [Service]
     WorkingDirectory=%h/path/to/this/repo
     ExecStart=/usr/bin/python3 receiver.py
     Restart=on-failure
     [Install]
     WantedBy=default.target
     ```
     then `systemctl --user enable --now claude-metrics`.

   Only one receiver can run (port 4318 is the lock).

## Optional: weekly digest

`python digest.py` writes a self-contained report for the last 7 full days
(UTC) to `reports/digest-<YYYY>-W<week>.html` — totals, per-project,
per-model-family, top-10 most expensive prompts, daily breakdown. Existing
digests are **never overwritten** (same-week re-runs get a `-HHMMSS` suffix);
`reports/index.html` links them all. Schedule it weekly (Task Scheduler /
cron) if wanted.

## Architecture

```
Claude Code sessions ──OTLP/HTTP (json)──► receiver.py (127.0.0.1:4318, optional)
                                              │  live events → metrics.db
~/.claude/projects/**/*.jsonl ──ingest────►   │  + reconcile hourly
        (generate-dashboard script)           ▼
                                        build_dashboard.py ──► dashboard.html
```

Both sources write the same SQLite DB; dedupe on Anthropic request ids and
tool-use ids makes their overlap harmless (live OTel rows win, since they
carry the CLI's authoritative `cost_usd`).

| File | Role |
|---|---|
| `generate-dashboard.ps1` / `.sh` | One-shot: ingest + build + open |
| `jsonl_ingest.py` | Transcript ingest/reconcile (`--force` = full re-parse) |
| `build_dashboard.py` | Aggregates metrics.db → dashboard.html |
| `receiver.py` | Optional live OTLP listener + scheduler |
| `digest.py` | Weekly digest with collision-proof filenames |
| `template.html` | Dashboard UI (no external deps) |
| `db.py` / `pricing.py` | Storage / pricing table for estimates |
| `check_live.py` | Diagnostic: dump recent live rows |

## Chart metrics

The chart card offers nine views (dropdown, persisted): output tokens/day,
tokens & lines per **active minute** (day total ÷ summed wall-clock span of
each prompt; days under a minute of activity are skipped), cost/day, **cost
composition** (stacked $: cache read / cache write / output / uncached input —
cache reads typically dominate despite the 0.1x discount because input volume
dwarfs output), cache hit rate, cost per 1K lines written (≥50 lines/day),
model mix (stacked by family), and subagent share. Cost components are derived
from the pricing table and scaled to sum to the CLI-reported cost where
available; cost views show the "without caching" counterfactual.

## Other dashboard features

- **Current 5h window tile** — ccusage-style rate-limit block (starts at the
  floored hour of the first request after the previous block ends): output
  tokens, cost, reset time. Global, not filter-scoped.
- **Per-prompt detail** (click a row) — cost-composition donut + table,
  per-model breakdown, files changed with lines +/−, tool-call histogram,
  subagents.
- **Group by project** — subtotal header rows, ordered by cost, click to
  collapse.
- **Export CSV** — the current filtered/sorted view, incl. cost components.
- **Auto-refresh** — the page reloads every 5 minutes; filters, chart choice,
  and grouping persist (localStorage). Light/dark theme with toggle.

## File-change tracking

Files / Lines ± / chars per prompt come from the `structuredPatch` diffs that
Edit/Write tool results leave in transcripts (subagent edits included).
Changes made via Bash (git operations, scripts, generators) leave no diff in
transcripts and aren't counted. These columns update on ingest/reconcile, not
via OTel (its tool events carry no diffs).

## Known limitations

- Headless `claude -p` prompts that predate OTel enablement show
  "(prompt text unavailable)"; live sessions carry text via OTel.
- A brand-new session may show project `?` until the next ingest maps its
  session to a working directory.
- Costs for backfilled rows are estimates (`~` prefix); keep `pricing.py`
  current if models change. Live rows use the CLI's own cost figure.
