# Roadmap

Status legend: Not Started · In Progress · Complete · Deferred
Last updated: 2026-08-25

| Feature | Status | Notes |
|---|---|---|
| Additional local `.claude*` directories | Complete | Siblings of the primary dir are discovered and ingested; projects prefixed with the folder name. Siblings are searched to the same depth as extra locations, so a container like `~/.claude-archive/oldbox/.claude` resolves too. Covered by `test_sources.py`. |
| Deep search of candidate locations | Complete | `--extra-dir` paths are searched to `--depth` levels (default 4) for anything Claude-shaped, so a path above the real dir still works. Recognises renamed/copied dirs by their transcripts. |
| Remote collection over SSH | Complete | One `sh`/`find`/`tar` call per host into `remote-cache/<host>/`, incremental by mtime, failures contained per host. Verified live against a LAN host on 2026-08-22: 21 transcripts pulled, 14 sessions ingested under the host's label; re-run transferred 0 files in 0.3s; an unresolvable host returned a clean error without aborting the build. |
| Remote host selection | Complete | Explicit `--remote HOST` (repeatable) and `--ssh-config` for every host in `~/.ssh/config`; `--list-ssh-hosts` previews the list without connecting. |
| Report index page | Complete | `index.html` written next to `dashboard.html` on every build and every digest run, linking the dashboard and all archived reports newest-first. |
| Source labels in the dashboard | Complete | `sessions.source_label` (schema v3) prefixes project names for every non-primary source, keeping same-named projects on different machines distinct. |
| Standing source configuration | Complete | `sources.json` (from `sources.example.json`); also how the live receiver picks up extra sources. CLI flags add to it. |
| Receiver keeps SSH off the DB lock | Complete | `receiver.reconcile()` fetches remotes on a separate connection before taking `_db_lock`, so a slow transfer can't stall live telemetry. |
| Fast, graceful remote failure | Complete | Schema v4 adds `remote_state.fail_count`/`next_attempt`. Failures are classified (auth / unreachable / no-Claude-dir) and the host is parked: 6h for auth, 12h for no-Claude-dir, 15m doubling to 12h for transient. `remote_budget` caps a whole background pass; `ConnectTimeout=8` + `ServerAlive` bound a single host; `BatchMode`+`NumberOfPasswordPrompts=0` make a missing key fail in 0.24s. `--remote-status` shows why a host is quiet. Verified live: dead host costs 8s once then 0.3s per pass, working host unaffected. |

| Older transcript formats | Complete | Prompt-marker vintage is detected per file, not by version: files without an `origin` marker fall back to recognising prompts by shape, which recovers sessions whose usage was previously dropped wholesale. Modern files keep the strict rule (verified byte-identical: 256 prompts before and after). Also handles inline `isSidechain` subagent turns and session ids read from the transcript body. Schema v5 forces the one-time re-parse. Verified on real data: a 2.1.16x-2.1.17x remote went 209 -> 1,201 API requests and 25K -> 917K output tokens. |

| MTD date range | Complete | Calendar month-to-date alongside Today/7d/30d/90d/All. Kept as the string `"mtd"` rather than a day count, since `+"mtd"` is NaN; handler, aria state and persistence all compare as strings. |
| Product selector (Code / Cowork) | Complete | Sits between the date range and the project list, scopes tiles/chart/table/CSV, narrows the project list and strips the redundant `cowork/` prefix. Defaults to Claude Code; hidden entirely when only one product has data. Filtering keys off the row's `kind` from `collect()`, not a name prefix. |
| Cowork (Claude Desktop) collection | Complete | Session sandboxes under the desktop app's `local-agent-mode-sessions` store are auto-detected per platform and ingested under one `cowork` label, each session named by the app's own title instead of `local_<uuid>/outputs`. `--no-cowork` / `--cowork-dir` to override. Verified live: 14 sessions, 22 transcripts, 27 prompts, $21.85. |
| Claude Chat collection | Deferred | No local data exists to collect - see below. |

## Deferred

- **Claude Chat usage** — conversations live server-side; the desktop app
  keeps no local per-conversation or token record (Local Storage and IndexedDB
  hold auth/UI state only, and the 211 MB HTTP cache carries no accounting).
  The one local signal is `plan-usage-history.json`: percent-of-plan-limit for
  the 5-hour and 7-day windows, sampled every 5 minutes on a 30-day rolling
  window. It is account-wide and cannot be split by product or conversation,
  so it would not answer "what did Chat cost". Charting it as a rate-limit
  gauge is the only thing it could support, and that is a different feature.
- **Cost from Cowork's `audit.jsonl`** — each sandbox logs a CLI-reported
  `total_cost_usd`, which sounds authoritative but only exists for runs that
  finished and reported. Measured here: audit accounted for $7.47 against the
  transcripts' $21.85, the gap being one whole session — the most expensive
  one — that audit never recorded. Across sessions audit does cover, our
  estimate is within 3% ($7.23 vs $7.47). Would be worth revisiting with a
  per-session coverage guard (audit run count == prompt count) that upgrades
  only fully covered sessions.
- **Windows remotes over SSH** — the collector assumes a POSIX remote (`sh`,
  `find`, `tar`). Supporting Windows would need a second, PowerShell-based
  collection path and a way to detect which to send. Workaround today: share
  the machine's `.claude` folder and point `--extra-dir` at it. Would be
  unblocked by demand for it plus a Windows host to test against.
- **Pre-2.1.168 transcript layouts** — format handling is derived from real
  transcripts written by CLI 2.1.168 through 2.1.240. Nothing in the ingester
  assumes a version, so older layouts may well parse, but there is no data on
  hand to confirm it. Would be unblocked by a sample of a genuinely old
  `~/.claude` tree.
- **Live telemetry from remote machines** — remote rows are only as fresh as
  the last fetch. True live coverage would mean running a receiver per machine
  and merging, or exposing this receiver beyond localhost, which the current
  "prompt text never leaves the machine" guarantee rules out.
