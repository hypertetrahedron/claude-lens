# Roadmap

Status legend: Not Started · In Progress · Complete · Deferred
Last updated: 2026-08-26

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
| All-products view | Complete | The product selector gained an "All products" entry so combined totals are reachable again; Claude Code stays the default. Prefixes are kept under All, where they are what distinguishes two products' projects. |
| Product-aware weekly digest | Complete | `digest.py` adds a "By product" section whenever a week covers more than one, instead of pooling Cowork spend into Claude Code totals. |
| Product list derived from data | Complete | The selector is built from the `kind` values actually present, with `PRODUCT_META` supplying display names and falling back to the raw id, so a product added later is reachable rather than filtered into invisibility. |
| Receiver staleness guard | Complete | The receiver fingerprints its own files and stops writing `dashboard.html` once they change, so a hand-run rebuild is never overwritten by older code; ingestion continues and the log says to restart. `build_dashboard.py` prints a matching note when a receiver is listening. Restarting itself was tried and rejected - see Deferred. |
| Plan-limit gauges | Complete | Claude Desktop's `plan-usage-history.json` (5h and 7d percent-of-limit, every 5 min, 30-day rolling) drives a tile and two chart views. Account-wide by nature, so only the date range applies; the 5h window tile is now labelled account-wide for the same reason. Absent cleanly when the desktop app is not installed. |
| Session names | Complete | Titles from Claude Desktop's `claude-code-sessions` metadata are stored on `sessions.title` and offered as an optional Session column. |
| Audit-backed Cowork cost | Complete | `run_cost` holds each sandbox's CLI-reported `total_cost_usd` with its run count; `collect()` reprices a session only when that count matches the prompts found, otherwise the estimate stands. 22 of this machine's rows repriced to exact figures. |
| Payload size cap | Complete | `--max-rows` (default 8000) embeds only the newest prompts; the page states when rows were dropped. |
| Shareable builds | Complete | `--no-prompt-text` blanks prompt text while keeping every number, and the page says it was redacted. |
| Single-pass transcript reads | Complete | `scan_header` returns the parsed entries it already read, bounded by a byte budget, so a transcript is parsed once instead of scanned then parsed. Oversized files fall back to streaming. |
| Per-session index | Complete | `idx_req_session` on `api_requests(session_id)`; per-session work is no longer a full scan. |
| Gapless stacked bars | Complete | The 2px inter-segment gap is gone; segments are flush with a 1px floor each. Measured on real data: 87 sub-2px segments in the cost-composition chart and 15 in the model-mix chart now render that the gap would have erased. |
| Gapless, cost-ordered donuts | Complete | The per-prompt cost donut lost its 2px slice stroke, gained cheapest-first slice order matching the bars, and a MIN_SWEEP floor so a small component still draws. Measured: 289 of 494 prompts had a slice below the floor, and every ring still closes to an exact turn. |
| Ironbow cost ramp | Complete | Stacked series are coloured by cost, darkest cheapest to brightest dearest, for both model families and cache/output components; stacking order matches. Replaces the old fixed palette where haiku and fable were both greens at opposite ends of the cost spectrum (now 126-148 dE apart). Ends trimmed per theme; all stops >= 3:1 on their background, adjacent stops >= 37 dE. |
| Bedrock / Vertex support | Complete | Model ids from any provider are canonicalised at ingest (schema v7 stores `provider` and `model_raw`, and rewrites ids already in the DB). Rates are per provider with `pricing.local.json` overrides for region rate cards and deployment-ARN aliases; promotional pricing is first-party only. Subscription-only tiles hide when no first-party traffic exists, and unpriced models are named on the page rather than only on stderr. Verified: a Bedrock transcript that previously totalled $0.00 now costs correctly, and an opaque deployment ARN is reported as unpriced instead of silently zero. |
| Project-run highlighting | Complete | Sorting by project marks the first row of each project's run (rule above, name un-muted), so consecutive rows from one project are separable. Applied only under a project sort - elsewhere it would draw a division that does not exist - and not in grouped mode, whose header rows already separate. |
| Project CLAUDE.md | Complete | Records the constraints and habits that are not obvious from the code: stdlib-only, the receiver overwriting `dashboard.html`, why a re-parse cannot correct a stored row, verifying transcript facts against real data, and the DOM-stub testing pattern with its blind spot for CSS. |

## Deferred

- **Claude Chat usage** — conversations live server-side; the desktop app
  keeps no local per-conversation or token record (Local Storage and IndexedDB
  hold auth/UI state only, and the HTTP cache carries no accounting). The one
  local signal, `plan-usage-history.json`, is account-wide and cannot be split
  by product or conversation, so it cannot answer "what did Chat cost". It is
  now charted for what it *can* answer — see the plan-limit gauges above —
  but per-conversation Chat accounting stays out of reach.
- **Bedrock cost verification against a live account** — the Bedrock and
  Vertex rate tables default to Anthropic list prices, and provider behaviour
  (whether Claude Code reports `cost_usd` over OTel on Bedrock, whether the 1h
  cache tier exists there) has not been checked against a real deployment. The
  override file exists precisely because these are assumptions. Would be
  unblocked by access to an account on either route.
- **Provisioned Throughput and batch billing** — both break the per-token
  model entirely (hourly commitment; ~50% discount). Nothing here can estimate
  them; reconciling against AWS Cost Explorer or the CUR would be the real
  answer.
- **Moving metrics.db off the network share** — the database lives on an SMB
  share here, where SQLite WAL is not reliable (a transient `disk I/O error`
  was seen once). A `--db` flag pointing at local disk, with only the reports
  on the share, would remove the risk. Explicitly deferred by request.
- **Receiver restarting itself on a code change** — tried twice and rejected.
  Exiting with a non-zero code did not make Windows Task Scheduler restart the
  task, and `os.execv` left nothing listening at all. Stopping rebuilds and
  logging is the safe half; a supervisor that genuinely restarts (systemd
  `Restart=on-failure`) could take the stricter behaviour later.
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
