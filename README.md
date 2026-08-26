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
(`CLAUDE_CONFIG_DIR` is honored if set) **plus any sibling `.claude*`
directory**, builds `metrics.db`, `dashboard.html` and `index.html` next to
the script, and opens the dashboard. Re-running is incremental — only
new/changed transcripts are parsed. Flags: `-Force` / `--force` re-parses
everything; `-NoOpen` / `--no-open` skips the browser; `-Index` / `--index`
opens the report index instead of the dashboard.

`index.html` is the one page to bookmark: it links the live dashboard and
every archived report, newest first, and is rewritten on every build.

To pull in other machines or other directories, see
[Aggregating multiple sources](#aggregating-multiple-sources).

In this one-shot mode all costs are estimated from the pricing table in
`pricing.py` (shown with a `~` prefix). That's the whole setup — everything
below is optional.

## Aggregating multiple sources

One Claude account is often used from more than one directory and more than
one machine. Every source is ingested into the same database and every row
keeps its origin: **project names from anywhere but the primary `~/.claude`
are prefixed with a label** — `build-server/gem-trip`, `.claude-work/api` —
so grouping, filtering and per-project costs stay honest even when two
machines both have a project called `src`.

The places looked at:

| Source | Found how | Label |
|---|---|---|
| Primary `~/.claude` | `CLAUDE_CONFIG_DIR`, else `~/.claude` | none (names unchanged) |
| Sibling directories | any `.claude*` folder next to the primary one, searched to the same depth as an extra location | the folder name |
| Extra locations | each configured path searched a few levels deep | the folder name, or its parent when the folder is just `.claude` |
| Remote machines | SSH, from `~/.ssh/config` or an explicit list | the host name |
| **Cowork** | Claude Desktop's session store, auto-detected | `cowork` |

A directory counts as a Claude directory when it has a `projects/`
subdirectory **and** either a `.claude*` name, a known settings/state file, or
actual transcripts inside `projects/`. That last rule is what lets an extra
location point *above* the real directory — at a backup drive holding
`backups/<machine>/.claude`, say — and still be found. Searching stops at each
match and skips the usual heavy directories (`node_modules`, `.git`, …).

Siblings get the same depth search, so `~/.claude-archive/oldbox/.claude`
works as well as a plain `~/.claude-work`. Only the top-level name is filtered
to `.claude*`, so this never walks the whole home directory.

### Cowork (Claude Desktop)

Cowork runs Claude Code inside a per-session sandbox, and each sandbox keeps a
complete Claude directory of its own — ordinary transcripts, in the ordinary
format. They are picked up automatically when Claude Desktop is installed, at:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Claude\local-agent-mode-sessions` |
| macOS | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Linux | `~/.config/Claude/local-agent-mode-sessions` |

All sandboxes share the single label `cowork`, and each session is named by
the **title the desktop app shows** — `cowork/Refactor the billing module`.
Without that, every session would appear as `local_9f3c1a20-…/outputs`, since
each sandbox is its own directory and their working directory is always the
same `outputs` path. That sandbox cwd is deliberately not recorded, so the
title is what the dashboard groups on.

Turn it off with `--no-cowork` (or `"cowork": false`); point it somewhere else
with `--cowork-dir PATH`. Nothing happens on a machine without the desktop app.

**Cost is estimated from `pricing.py`, the same as every other source**, even
though each sandbox also has an `audit.jsonl` carrying a CLI-reported
`total_cost_usd`. That looks like the better source and isn't: those records
only exist for runs that finished and reported, and a run that never reported
leaves no trace in them. On the machine this was built against, `audit.jsonl`
accounted for $7.47 while the transcripts showed $21.85 — the difference was a
single session, the most expensive one, that audit never recorded at all.
Across the sessions audit *does* cover, the estimate lands within 3% in
aggregate ($7.23 vs $7.47). Complete coverage with a small estimation error
beats exact figures with a two-thirds hole in them.

### One-off, from the command line

```
.\generate-dashboard.ps1 -ExtraDir D:\backups -Remote box1,box2
./generate-dashboard.sh  --extra-dir ~/backups --remote box1 --remote box2
```

| Flag (ps1 / sh) | Effect |
|---|---|
| `-ExtraDir` / `--extra-dir` | also search this path for Claude directories (repeatable) |
| `-Depth` / `--depth` | how many levels below an extra dir to search (default 4) |
| `-NoSiblings` / `--no-siblings` | ignore sibling `.claude*` directories |
| `-NoCowork` / `--no-cowork` | ignore Claude Desktop's Cowork sessions |
| `-CoworkDir` / `--cowork-dir` | read a Cowork session store from this path instead |
| `-Remote` / `--remote` | collect from this SSH host (repeatable) |
| `-SshConfig` / `--ssh-config` | collect from every host named in `~/.ssh/config` |
| `-RemoteFull` / `--remote-full` | re-fetch all remote transcripts, not just new ones |
| `-SshTimeout` / `--ssh-timeout` | per-host time limit (default 300s) |

`python jsonl_ingest.py --list-ssh-hosts` prints exactly which hosts
`--ssh-config` would use, before any connection is made;
`--remote-status` prints what each host last did and why it may be paused.

### Standing configuration

Copy `sources.example.json` to `sources.json` (gitignored) to make it
permanent — this is also the only way the live receiver picks up extra
sources, since it reconciles on its own schedule:

```json
{
  "extra_locations": ["D:/backups/claude-machines"],
  "scan_sibling_claude_dirs": true,
  "depth": 4,
  "remotes": ["build-server", "mac-mini"],
  "use_ssh_config": false,
  "ssh_options": ["-i", "~/.ssh/id_claude"],
  "ssh_connect_timeout": 8,
  "ssh_timeout": 300,
  "remote_budget": 600,
  "cowork": true,
  "cowork_paths": []
}
```

CLI flags add to this file rather than replacing it. The three timing keys
bound how long a bad host may make anything wait — see
[A broken remote must never cost you anything](#a-broken-remote-must-never-cost-you-anything).

### How remote collection works

For each host, one `ssh` call runs a small POSIX shell script (`sh`, `find`,
`tar` — no agent, nothing installed) that finds every `.claude*` directory
with a `projects/` subtree under `$HOME` and streams the transcripts back as a
gzipped tar. They land in `remote-cache/<host>/` and are then ingested exactly
like a local directory.

- **Incremental.** Only transcripts modified since the last successful fetch
  are sent (with an hour of slack for clock skew). The first fetch from a busy
  machine is the slow one.
- **Read-only and quiet.** Nothing is written on the remote, and no `sudo` is
  used. Only `*.jsonl` transcripts are transferred — no settings, no
  credentials.
- **Requirements.** Key-based SSH auth and a POSIX remote. Windows remotes are
  not supported — copy their `.claude` to a shared folder and use
  `--extra-dir`.
- **Trust.** Extraction refuses any archive member that would land outside the
  host's own cache directory.

### A broken remote must never cost you anything

Missing or wrong SSH credentials are the most common problem here, and a
background collector that stalls on them is worse than no collector. Four
mechanisms make a bad host cheap:

| Mechanism | Effect |
|---|---|
| `BatchMode=yes` + `NumberOfPasswordPrompts=0` | a missing key fails in well under a second instead of waiting on a password prompt |
| `ConnectTimeout` (default 8s) | bounds a machine that is powered off or firewalled |
| `ServerAliveInterval=15` × 3 | a connection that dies mid-transfer is torn down in ~45s, not at the overall timeout; a slow-but-progressing transfer is untouched |
| Failure backoff | a failed host is *parked* rather than retried every pass |

Backoff is by failure kind, because a missing key is a state and not a blip:
auth failures park the host for **6h**, a machine with no Claude directory for
**12h**, and anything transient backs off **15m → 30m → 1h …** capped at 12h.
One success clears all of it. On top of that, `remote_budget` (default 600s)
caps what *all* hosts together may take in a single background pass.

Measured on this setup:

```
pass 1:  9.83s   10.255.255.1       FAIL   8.0s  retry in 15m [unreachable]
                 raspberrypi.local  ok     1.8s  21 files
pass 2:  0.32s   10.255.255.1       SKIPPED (backing off, retry in 14m)
                 raspberrypi.local  ok     0.3s  0 files
```

A wrong key against a reachable host fails in **0.24s**. Throughout, a host
that cannot be reached is still *reported* from its existing cache — its
history does not vanish from the dashboard because the machine is asleep — and
the local reconcile always runs regardless.

`python jsonl_ingest.py --remote-status` shows why a host is quiet:

```
HOST                         LAST OK              FAILS  STATUS
raspberrypi.local            2026-08-22 15:22         0  ok
build-server                 never                    3  backing off 6h - Permission denied (publickey).
```

An explicit `--remote HOST` run always ignores the backoff — if you just fixed
the key, you do not have to wait for it.

## Older transcript formats

Claude Code's transcript format has changed over time, and Claude Lens reads
the older shapes as well as the current one — worth knowing about if you have
history going back a while, or you collect from a machine running an older
CLI.

The consequential difference is how a typed prompt is marked. Current builds
tag it `origin.kind == "human"`. Builds before that wrote no `origin` at all,
which makes a typed prompt and a tool result structurally alike — both are
`type: "user"` carrying a `promptId`. Because every API request is attributed
to the prompt above it, an unrecognised prompt did not merely go missing
itself: **the whole session's token usage went with it.**

Each transcript is therefore classified on its own, by whether any user entry
in it actually uses the marker — not by version number, since the marker
appeared mid-way through the 2.1.x series. Files that use it keep the strict
rule. Files that don't fall back to recognising a prompt by shape: a user turn
that is not a tool result, not harness-injected, not a subagent's own turn,
and that has text.

The loose rule is applied *only* where the marker is genuinely absent. Modern
transcripts are full of user entries with plain text and no origin — `/clear`
wrappers, compaction summaries, `[Request interrupted by user]` — and reading
those as prompts would invent rows and misattribute usage.

Also handled: subagent work interleaved into the main transcript (older
layouts flagged it `isSidechain`; current ones use
`<session>/subagents/agent-*.jsonl`), and session ids taken from the
transcript body rather than assumed from the filename.

Upgrading re-parses every transcript once (schema v5) to backfill what earlier
versions dropped. On the machine this was developed against, one older remote
went from 209 recorded API requests to 1,201, and from 25K output tokens to
917K.

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

## Sharing and size

`build_dashboard.py` takes two options that matter once a dashboard leaves
your machine or your history gets long:

| Flag | Effect |
|---|---|
| `--no-prompt-text` | Blanks prompt text. Every number survives; nothing you typed is embedded. The page says it was redacted. |
| `--max-rows N` | Embeds only the newest N prompts (default 8000; `0` for no limit). |

The payload is re-parsed by the browser on every load and every 5-minute
auto-refresh, and each prompt costs roughly a kilobyte — so an unbounded
history eventually makes the page slow to open. When rows are dropped the
dashboard says so, so a shrinking "All" view is never a mystery.

Prompt text is otherwise embedded verbatim (first 400 characters), which is
worth remembering before sending `dashboard.html` to anyone.

## Keeping the receiver current

The receiver owns `dashboard.html` — it rebuilds it about once a minute using
the code it started with. Edit the builder or the template while it is
running and it would otherwise overwrite your rebuild with output from the old
code, which is a baffling way to lose work.

So it fingerprints its own files at startup, and once any of them changes it
**stops writing `dashboard.html`** and logs:

```
ERROR code changed on disk (template.html); no longer rebuilding
dashboard.html - restart this receiver to pick up the new version
```

Ingestion carries on, and a rebuild you run by hand is left alone. Restart the
receiver to resume live rebuilds. `build_dashboard.py` also prints a note when
it sees a receiver listening on 127.0.0.1:4318, so the reminder appears where
you are standing.

(It does not restart itself. Exiting and re-execing were both tried: Windows
Task Scheduler did not restart on a non-zero exit, and a re-exec left nothing
running at all — worse than a stale page.)

## Optional: weekly digest

`python digest.py` writes a self-contained report for the last 7 full days
(UTC) to `reports/digest-<YYYY>-W<week>.html` — totals, per-project,
per-product (when more than one is present), per-model-family, top-10 most
expensive prompts, daily breakdown. Existing
digests are **never overwritten** (same-week re-runs get a `-HHMMSS` suffix);
`reports/index.html` links them all, and the top-level `index.html` is
refreshed too so a new digest appears next to the live dashboard. Schedule it
weekly (Task Scheduler / cron) if wanted.

## Architecture

```
Claude Code sessions ──OTLP/HTTP (json)──► receiver.py (127.0.0.1:4318, optional)
                                              │  live events → metrics.db
~/.claude/projects/**/*.jsonl      ──┐        │  + reconcile hourly
.claude*/, extra locations         ──┼─ingest►│                dashboard.html
remote hosts ──ssh──► remote-cache/──┘        ▼              ┌► index.html
        (sources.py / generate-dashboard) build_dashboard.py ─┘
```

Both sources write the same SQLite DB; dedupe on Anthropic request ids and
tool-use ids makes their overlap harmless (live OTel rows win, since they
carry the CLI's authoritative `cost_usd`).

| File | Role |
|---|---|
| `generate-dashboard.ps1` / `.sh` | One-shot: ingest + build + open |
| `sources.py` | Finds Claude directories: local, sibling, nested, remote |
| `jsonl_ingest.py` | Transcript ingest/reconcile (`--force` = full re-parse) |
| `build_dashboard.py` | Aggregates metrics.db → dashboard.html |
| `report_index.py` | Writes index.html linking every report |
| `receiver.py` | Optional live OTLP listener + scheduler |
| `digest.py` | Weekly digest with collision-proof filenames |
| `template.html` | Dashboard UI (no external deps) |
| `db.py` / `pricing.py` | Storage / pricing table for estimates |
| `check_live.py` | Diagnostic: dump recent live rows |
| `test_sources.py` | Tests for multi-source ingest and the report index |

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
- **Configurable columns** — the ⚙ button opens a panel to add, remove, and
  reorder table columns (persisted). Beyond the defaults, opt-in columns
  include per-prompt derived metrics: tokens/min and lines/min (over the
  prompt's own duration), cache hit %, cost per 1K lines, subagent share,
  cost without caching, API calls, cache-read tokens, chars written.
- **Date range** — Today, 7d, **MTD** (calendar month to date, in your own
  timezone), 30d, 90d, All. MTD is a calendar period rather than a rolling
  window, so early in the month it covers less than 7d.
- **Product selector** — when a database holds more than one Claude product,
  a selector appears between the date range and the project list, offering
  **All products**, **Claude Code** and **Claude Cowork**. It scopes
  everything: tiles, chart, table and CSV. The project list narrows to the
  selection, and the `cowork/` label prefix is dropped from names once it is
  redundant — under *All products* it stays, because there it is what keeps
  two products' projects apart. Claude Code is the default. On a Code-only
  install the selector stays hidden. Rows are tagged by the ingester rather
  than matched on their name, so a local project that happens to be called
  `cowork` is still Claude Code; and the option list is built from the data,
  so a product added later shows up rather than becoming unreachable.
- **Plan-limit gauges** — when Claude Desktop is installed, a tile shows the
  account's current 5-hour and 7-day rate-limit usage, and two chart views
  show the daily peak of each. These come from the desktop app's own
  sampling, are **account-wide**, and are not affected by the project,
  product or model filters — only by the date range. The *Current 5h window*
  tile is likewise account-wide (rate limits apply to the account, so
  scoping it to a project would misrepresent it).
- **Session names** — sessions started from Claude Desktop carry the title
  the app shows. Add the optional **Session** column to see it.
- **Group by project** — subtotal header rows, ordered by cost, click to
  collapse; subtotals follow the configured columns (rates aggregate at the
  group level).
- **Export CSV** — the current filtered/sorted view, incl. cost components.
- **Notices** — if a build embedded only the newest N prompts, or withheld
  prompt text, the page says so rather than quietly showing less.
- **Auto-refresh** — the page reloads every 5 minutes; filters, chart choice,
  and grouping persist (localStorage). Light/dark theme with toggle.

## File-change tracking

Files / Lines ± / chars per prompt come from the `structuredPatch` diffs that
Edit/Write tool results leave in transcripts (subagent edits included).
Changes made via Bash (git operations, scripts, generators) leave no diff in
transcripts and aren't counted. These columns update on ingest/reconcile, not
via OTel (its tool events carry no diffs).

## Database versioning

The schema version lives in SQLite's `PRAGMA user_version`. Databases created
before version tracking (user_version 0) are treated as **v1**. Every entry
point opens the DB through `db.connect()`, which runs any pending migrations
automatically — updating an older instance "just works" on the next run; a
database *newer* than the code refuses to open with a clear error.

**Contributors:** any schema change must ship as a `_migrate_to_N()` function
registered in `MIGRATIONS` in `db.py`, bump `SCHEMA_VERSION`, update the
`CREATE TABLE` statements to match (fresh databases are created current), and
add a changelog line in `db.py` and below.

| Version | Change |
|---|---|
| 1 | Initial schema |
| 2 | `tool_calls.detail` (skill names); transcript-derived tool names upgrade generic live-telemetry rows (`mcp_tool` → `mcp__server__tool`); one-time transcript re-parse to backfill |
| 3 | `sessions.source_label` (which Claude directory or remote machine a session came from) and `remote_state` (last successful SSH fetch per host, keeping transfers incremental). Existing rows keep an empty label, which is exactly what they were: the primary `~/.claude` |
| 4 | `remote_state.fail_count` / `next_attempt`: an unreachable or unauthenticated host is backed off instead of retried every pass, so a misconfigured remote costs the background receiver nothing |
| 5 | No column change — clears `ingest_state` to force one re-parse of every transcript, backfilling usage that older formats had dropped (see [Older transcript formats](#older-transcript-formats)) |
| 6 | `sessions.title` (the name Claude Desktop gives a session), `run_cost` (a CLI-reported cost per session, spent only where it provably covers every run), and an index on `api_requests.session_id` |

## Known limitations

- Headless `claude -p` prompts that predate OTel enablement show
  "(prompt text unavailable)"; live sessions carry text via OTel.
- Format handling is derived from transcripts written by CLI 2.1.168 and
  later. Much older layouts may still parse — nothing assumes a version — but
  they have not been tested against real data.
- A brand-new session may show project `?` until the next ingest maps its
  session to a working directory.
- Claude Chat conversations are not collectable: they live server-side, and
  the desktop app keeps no local per-conversation or token record. The only
  local signal is `plan-usage-history.json` (percent-of-plan-limit, sampled
  every 5 minutes, account-wide, 30-day rolling) — it cannot be attributed to
  a product or a conversation, so it is not ingested.
- Remote collection needs a POSIX remote reachable with key-based SSH;
  Windows remotes have to go through a shared folder and `--extra-dir`.
- Remote rows are only as fresh as the last fetch — the live OTel receiver
  covers this machine, not the others.
- A remote parked by backoff stays stale until its retry window opens; run
  `--remote HOST` explicitly (or check `--remote-status`) if that is a
  surprise.
- Costs for backfilled rows are estimates (`~` prefix); keep `pricing.py`
  current if models change. Live rows use the CLI's own cost figure.
