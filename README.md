# Claude Lens

A local usage dashboard for Claude Code: one row per prompt, with per-model
input/output/cache tokens, tool-call counts, file/line changes, cost (with a
cache-savings counterfactual) and duration — subagent work and
harness-injected turns folded into the prompt that caused them.

It reads every Claude Code transcript it can find: the signed-in user's
`~/.claude`, other `.claude*` directories, backup or archive locations,
**other machines over SSH**, and **Claude Cowork** sessions from the desktop
app — all in one database, each row labelled with where it came from. Traffic
routed through **Amazon Bedrock** or **Vertex AI** is recognised and costed
too. Only Claude Code and Cowork are tracked; no other AI tools.

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

The wrapper also passes the build's own options straight through, so the
one-shot script covers the same ground as running the two Python steps by
hand: `-Db` / `--db PATH` (use a database somewhere other than next to the
script — both the ingest and the build are told), `-MaxRows` / `--max-rows N`,
`-NoPromptText` / `--no-prompt-text`, and `-Conversations` / `--conversations
N`. See [Sharing and size](#sharing-and-size).

`index.html` is the one page to bookmark: it links the live dashboard, the
per-prompt conversation pages when a build wrote them, Claude Code's own
`/insights` report when the CLI has written one (under `~/.claude/usage-data`,
or `$CLAUDE_CONFIG_DIR`), and every archived report, newest first. It is
rewritten on every build and every digest run.

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
  "cowork_paths": [],
  "db": "C:/claude-lens/metrics.db"
}
```

CLI flags add to this file rather than replacing it. The three timing keys
bound how long a bad host may make anything wait — see
[A broken remote must never cost you anything](#a-broken-remote-must-never-cost-you-anything).

### Where the database lives

`metrics.db` sits next to the scripts by default. Every entry point
(`jsonl_ingest.py`, `build_dashboard.py`, `digest.py`, `receiver.py`,
`check_live.py`, `report_index.py`) takes `--db PATH` to put it somewhere
else, and resolves the location the same way:

| Order | Source |
|---|---|
| 1 | `--db PATH` on the command line |
| 2 | the `CLAUDE_LENS_DB` environment variable |
| 3 | the `"db"` key in `sources.json` |
| 4 | `metrics.db` beside the scripts |

The reason to move it: SQLite's write-ahead log is not reliable on a network
share, and a checkout that lives on one will eventually meet a transient
`disk I/O error`. Point `"db"` at local disk and leave the reports on the
share. The setting is read by every entry point, so setting it once in
`sources.json` moves the database for the background receiver too.

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

## What a prompt row actually says

### The text

A prompt is not always one block of text. Working through the IDE extension,
what reaches the transcript is *several* text blocks: one or more envelopes
describing the editor's state, then the sentence the person typed.

```
<ide_opened_file>The user opened the file /very/long/path/to/module.py in
the IDE. This may or may not be related to the current task.</ide_opened_file>
rename the widget factory
```

Claude Lens drops the envelopes -- `<ide_opened_file>`, `<ide_selection>`,
`<ide_diagnostics>`, any other `<ide_...>` wrapper, and `<system-reminder>` --
and stores what is left. Before it did, every IDE-driven prompt was stored
envelope-first, so the dashboard showed rows of identical "The user opened the
file..." text and searching for a phrase someone had typed found nothing.

If a turn is *nothing but* an envelope, the envelope's contents are stored
rather than an empty string: a row that says something is more use than a
blank one. A turn is treated as harness-injected only when every one of its
text blocks is an envelope or a known harness opener.

### The kind

Not every prompt was typed. `prompts.kind` says which is which:

| kind | What it is |
|---|---|
| `human` | someone typed it (`origin.kind == "human"`); `injected = 0` |
| `task-notification` | a background agent reporting back |
| `coordinator` | a coordinating agent driving the session |
| `loop` | a turn dispatched by `/loop` |
| `scheduled` | a turn dispatched by a schedule or cron routine |
| `team` | a message from another Claude session |
| `command` | a slash command dispatched by the harness (the receiver reads it from OTel's `command_name` / `command_source`) |
| `other` | marked as non-human by a marker this version does not know |

An origin marker this version has never seen is stored as `other` rather than
dropped, so a kind a later CLI invents still shows up as "not typed by a
person" instead of quietly counting as one.

Everything but `human` is stored with `injected = 1` and a `canonical_id`
pointing at the most recent human prompt at or before it, so its cost is
folded into the turn that caused it. This matters most for background agents:
their transcripts are attributed to the `<task-notification>` prompt, not to
the prompt you typed, and without the fold their spend appears as rows nobody
recognises.

## Pricing and cost estimates

Rows that came from a transcript carry no cost — Claude Code only reports one
over OTel — so their cost is estimated from the table in `pricing.py` and
shown with a `~` prefix. Live rows use the CLI's own figure and are never
re-estimated. A model with no entry is **named on the page and counted as
$0.00 deliberately**; an unknown cost is not a zero cost.

Four things can move a request off the base rate, and `pricing.resolve()`
applies them in this order:

| | What it does | Applies to |
|---|---|---|
| Promotion (`INTRO_PRICES`) | A rate that expires on a date, applied only when the request's timestamp is before it | First-party only — a promotion on the Anthropic API says nothing about a marketplace's rate card. Empty today: Claude Sonnet 5's $2/$10 lived here until the September 2026 rise was cancelled and the rate became permanent |
| Fast mode (`FAST_PRICES`) | 2x list for requests the transcript recorded as `usage.speed = "fast"` | Claude Opus 5 and Opus 4.8, Anthropic provider only — fast mode is not offered on Bedrock, Vertex or Foundry |
| Data residency (`GEO_PREMIUM_MULT`) | 1.1x input and output when a request pinned inference to a geography (`usage.inference_geo = "us"`) | Models that support pinning — Opus 4.6 / Sonnet 4.6 and later, first-party only |
| Cache read (`CACHE_READ_MULT_BY_MODEL`) | A per-model multiple of the input rate, default 0.1 | Claude Fable 5.1 and Claude Mythos 5.1 read cache at **0.025x** — $0.25/MTok against a $10 input rate. On a cache-heavy agent, using 0.1x there overstates the bill fourfold |

The cache multipliers apply to whichever input rate came out of the first
three, so a fast-mode cache read costs a tenth of the *fast* input rate, not
of list.

Retired models keep their rates — old transcripts still need costing — and
gain a retirement date in `pricing.RETIRED`, so a model whose line simply
stops can be annotated rather than leaving a reader to wonder.
`pricing.status(model)` answers `"active"` / `"retired"` / `"unknown"`, where
`"unknown"` is the same set of ids that are reported as unpriced.

`pricing.tool_prompt_tokens(model)` holds Anthropic's published per-model size
of the tool-use system prompt (286 tokens on Opus 5, 675 on Opus 4.7, 400 for
a model with no published figure). Claude Code sends tool definitions on every
request whether or not a tool is called, so this is a floor on every turn's
input and the basis of the dashboard's harness-overhead estimate.

Everything above is overridable in `pricing.local.json` without editing the
table — per-provider rates, `cache_read_mult`, `fast_prices`,
`unsplit_cache_multiplier` and deployment-ARN `model_aliases`.
`pricing.example.json` documents the shape and is the file to copy.

## Bedrock and Vertex

Claude Code can reach the same models through the Anthropic API, through
Amazon Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) or through Vertex AI
(`CLAUDE_CODE_USE_VERTEX=1`). **Collection is identical** — transcripts land in
the same place in the same format — but each route decorates the model id
differently, and that used to matter a great deal:

| Route | Model id as recorded |
|---|---|
| Anthropic API | `claude-opus-4-5-20251101` |
| Bedrock | `us.anthropic.claude-opus-4-5-20251101-v1:0` |
| Bedrock (ARN) | `arn:aws:bedrock:…:inference-profile/us.anthropic.claude-…` |
| Vertex AI | `claude-opus-4-5@20251101` |

Pricing is keyed on the plain Anthropic form, so **every Bedrock id used to
miss the table and be costed at $0.00** — silently, because the warning went
to stderr and the receiver runs without a console. Ids are now canonicalised
on the way in: the decoration is stripped, the original is kept in
`api_requests.model_raw`, and the detected provider in `api_requests.provider`.
Upgrading rewrites the ids already stored (schema v7), since transcript rows
are insert-or-ignore and a re-parse would never touch them.

What follows from the provider:

- **Rates are per provider.** The Bedrock and Vertex tables start as copies of
  the Anthropic list price, which is where they have historically sat. Treat
  that as an assumption, not a measurement — Bedrock rates vary by region,
  batch inference is discounted, and Provisioned Throughput bills per
  model-unit-hour, where a per-token estimate means nothing at all. Paste your
  own rate card into `pricing.local.json` (see `pricing.example.json`).
- **Promotional pricing is first-party only.** A promotion on the Anthropic
  API says nothing about a marketplace's rate card.
- **Deployment ARNs are reported, not guessed.** An application inference
  profile or provisioned-model ARN names a *deployment*, not a model — nothing
  in the id says which model it serves. Those stay unpriced and are named in
  the dashboard until you map them under `model_aliases`. An alias sets the
  model; the provider still comes from the original id, so an aliased Bedrock
  deployment is billed at Bedrock rates.
- **Subscription-only tiles disappear.** The plan gauges and the 5-hour
  rate-limit block describe a Claude subscription. Traffic billed to a cloud
  account is governed by that account's throughput quotas instead, so those
  tiles are hidden rather than shown as zero.
- **Unpriced models are named on the page**, not just on stderr — the failure
  that started all this was a confident, wrong $0.00 with nothing to explain
  it. This applies to any unpriced model, not only Bedrock ones.

Rows recorded before provider tracking carry no provider and are treated as
first-party, so nothing changes for an existing install.

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

   Only one receiver can run (port 4318 is the lock). `--port N` moves it and
   `--db PATH` chooses the database; `python receiver.py --help` lists both.

### What live rows carry that transcripts do not

Every attribute below is read straight from Claude Code's documented
[OpenTelemetry events](https://code.claude.com/docs/en/monitoring-usage):

| Event | Stored as |
|---|---|
| `claude_code.api_request` | `effort`, `speed` (`fast`/`normal`), `cost_usd` (or `cost_usd_micros`), `duration_ms`, and `context_tokens` = `input_tokens` + `cache_read_tokens` + `cache_creation_tokens` |
| `claude_code.api_error` | an `api_requests` row with `error` set (`"<status_code>: <error>"`) and every token count zero, so failures are visible without inflating usage |
| `claude_code.tool_result` | `input_bytes` (`tool_input_size_bytes`), `result_bytes` (`tool_result_size_bytes`), `duration_ms`, `is_error` (from `success`) and `error_type` |
| `claude_code.user_prompt` | `prompts.kind` from `command_name` / `command_source`, so `/loop`, `/schedule` and other command dispatches are separable from typed prompts |

Two details worth knowing:

- **Rows are keyed on `request_id`, falling back to `client_request_id`.** The
  Anthropic request id exists only when the API answered; a timeout or a
  connection failure has none. Without the client-side id those rows would all
  be written with a NULL primary key — which SQLite accepts as often as it is
  asked — and a flaky network would fill the table with rows nothing can join
  or deduplicate.
- **An error never overwrites a success.** A retried attempt can carry the id
  of an attempt that later succeeded, so an existing row only gains the error
  text; its token counts are left alone.

### List price or your price

If your organization has contracted rates, an administrator sets
[`modelPricing`](https://code.claude.com/docs/en/settings-reference#modelpricing)
in managed settings and Claude Code reports *those* rates in `cost_usd`. Since
the numbers on the page then mean something different, every live row is
stamped with the basis in force when it arrived — `cost_basis` is `contracted`
when `modelPricing` is found, `list` otherwise. The files checked, in order:

```
$CLAUDE_CONFIG_DIR/settings.json  (or ~/.claude/settings.json)
/Library/Application Support/ClaudeCode/managed-settings.json   macOS
/etc/claude-code/managed-settings.json                          Linux and WSL
C:\Program Files\ClaudeCode\managed-settings.json               Windows
   ...plus managed-settings.d/*.json next to each
```

`modelPricing` is a managed-scope key — Claude Code ignores it in user,
project and local settings. The user file is read anyway so that someone
trying the setting locally sees why the label did not change, but only a
managed source actually alters the costs Claude Code reports. The result is
cached for five minutes.

`python check_live.py` reports the fill rate of each of these columns over the
newest rows, which is the quick way to confirm live capture is working (and to
tell "my CLI is too old for this attribute" from "the receiver is not running").

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

Both flags — and `--conversations N` and `--db PATH` — are accepted by
`generate-dashboard.sh` / `.ps1` too, so a redacted build is one command:
`./generate-dashboard.sh --no-prompt-text --no-open`.

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

## Optional: SessionEnd hook (fresh data without a daemon)

A background receiver is the most complete option, but it is a service to
install and keep alive. If you would rather have nothing running, let Claude
Code tell this project when a session finishes: `hooks/session_end_hook.py`
reads the [hook payload](https://code.claude.com/docs/en/hooks) from stdin,
ingests the one transcript it names, and rebuilds `dashboard.html`.

Add to `~/.claude/settings.json`:

```json
"hooks": {
  "SessionEnd": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python3 /path/to/claude-lens/hooks/session_end_hook.py",
          "timeout": 60
        }
      ]
    }
  ]
}
```

On Windows use `hooks\session_end_hook.ps1`; on Linux/macOS
`hooks/session_end_hook.sh` is there if you would rather not name an
interpreter. Both wrappers find a Python and the script relative to
themselves, so the repository can live anywhere.

**Set the `timeout`.** SessionEnd hooks share a 1.5-second budget unless a
per-hook `timeout` raises it (up to 60 seconds), and a dashboard rebuild does
not fit in 1.5 seconds. If you would rather keep the hook instant, add
`--no-build` to the command and let the next `generate-dashboard` run render
the page; the ingest alone is milliseconds.

What the hook guarantees:

| Guarantee | Why |
|---|---|
| Always exits 0 | A usage dashboard is never worth failing the end of someone's work. Anything that goes wrong is logged and swallowed. |
| Writes nothing to stdout | Claude Code interprets hook stdout, so everything the ingester or builder prints is captured into the log instead. |
| Skips the rebuild when a receiver is listening | The receiver owns `dashboard.html`; two writers would fight. |
| Logs to `hooks/hook.log` | Rotated at 1 MB, one backup kept. One line per session: what was ingested, whether the page was rebuilt, and how long it took. |

`--db PATH` picks a database, and `--transcript PATH` ingests a named file
instead of the one on stdin (useful when testing the hook by hand).

Why a hook rather than watching the files: Anthropic documents the transcript
JSONL layout as internal and subject to change, and points at hooks as the
supported way to react to a session's lifecycle. Parsing the transcripts is
still this project's own business — but the *timing* no longer depends on
polling something undocumented. `PreCompact` and `Stop` receive the same
`transcript_path` and would work with the identical script if you want the
page refreshed mid-session; `Stop` fires after every assistant turn, so expect
it to be noisy.

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
sources.py discovers, jsonl_ingest.py parses:

    ~/.claude/projects/**/*.jsonl        the signed-in user
    other .claude*/ and extra locations  siblings, backups, archives
    Cowork sandboxes                     Claude Desktop
    remote hosts ──ssh──► remote-cache/  other machines
    plan history, session names          Claude Desktop, optional
                     │
                     ▼
Claude Code ──OTLP/HTTP (json)──►  receiver.py  ──►  metrics.db
              (optional, live)          │                │
        reconciles hourly, rebuilds ────┘                ├──► build_dashboard.py ──► dashboard.html
        the dashboard within a minute                    └──► digest.py ──────────► reports/*.html
                                                              both refresh ───────► index.html
```

Both sources write the same SQLite DB; dedupe on Anthropic request ids and
tool-use ids makes their overlap harmless (live OTel rows win, since they
carry the CLI's authoritative `cost_usd`).

| File | Role |
|---|---|
| `generate-dashboard.ps1` / `.sh` | One-shot: ingest + build + open |
| `sources.py` | Finds Claude data: local, sibling, nested, remote, Cowork, plan history |
| `sources.example.json` | Template for `sources.json` (extra dirs, remotes, Cowork) |
| `jsonl_ingest.py` | Transcript ingest/reconcile (`--force` = full re-parse) |
| `build_dashboard.py` | Aggregates metrics.db → dashboard.html |
| `report_index.py` | Writes index.html linking every report |
| `receiver.py` | Optional live OTLP listener + scheduler (`--db`, `--port`) |
| `hooks/session_end_hook.py` | Optional SessionEnd hook: ingest one transcript, refresh the page (`.sh` / `.ps1` wrappers alongside) |
| `digest.py` | Weekly digest with collision-proof filenames |
| `template.html` | Dashboard UI (no external deps) |
| `db.py` / `pricing.py` | Storage / pricing, model-id canonicalisation |
| `pricing.example.json` | Template for `pricing.local.json` rate + alias overrides |
| `check_live.py` | Diagnostic: recent live rows + fill rates for the live-only columns (`--db`) |
| `test_sources.py` | Test suite (discovery, ingest, pricing, payload, template wiring) |
| `test_pricing.py` | Test suite (rates, cache multipliers, fast mode, retirement, overrides) |
| `test_receiver.py` | Test suite (OTel attribute mapping, dirty fingerprint, SessionEnd hook) |

## Chart metrics

The chart card offers eleven views (dropdown, persisted): output tokens/day,
tokens & lines per **active minute** (day total ÷ summed wall-clock span of
each prompt; days under a minute of activity are skipped), cost/day, **cost
composition** (stacked $: cache read / cache write / output / uncached input —
cache reads typically dominate despite the 0.1x discount because input volume
dwarfs output), cache hit rate, cost per 1K lines written (≥50 lines/day),
model mix (stacked by family), subagent share, and — when Claude Desktop is
installed — the daily peak of the account's 5-hour and 7-day **plan limits**.
The plan views are account-wide, so only the date range applies to them.

**Stacked charts are coloured by cost, on an ironbow ramp** — darkest is
cheapest, brightest is dearest. Model families run haiku → sonnet → opus →
fable (roughly $5 → $50 per Mtok of output), and cost components run cache
read → uncached input → cache write → output (0.1x → ~5x the input rate).
Stacking follows the same order, so a bar reads bottom-to-top as a cost
gradient. A model whose price is unknown gets a neutral grey rather than a
place on the ramp — unknown is not the same as expensive. The ramp's extreme
ends are trimmed per theme, because near-black vanishes on the dark page and
pale yellow vanishes on the light one; every stop clears 3:1 against its
background, and adjacent stops are at least 37 ΔE apart.

Segments in a stacked bar sit flush against each other. They used to be
separated by a 2px gap, which looked tidy on large segments and erased small
ones outright — a series one pixel tall minus a two-pixel gap is not there at
all. Colour does the separating now, and every segment keeps a 1px floor.

The per-prompt cost donut follows the same three rules: no separating stroke,
slices ordered cheapest to dearest around the ring, and a minimum arc so a
small component is still visible. The floor is paid for proportionally by the
larger slices, so the ring closes exactly; with at most four components the
distortion stays under about a degree per lifted slice, and the tooltip and
the table beside the donut carry the exact figures either way.

Cost components are derived from the pricing table and scaled to sum to the
CLI-reported cost where available; cost views show the "without caching"
counterfactual.

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
- **Project runs when sorted by project** — sort by the Project column and
  the first row of each project gets a rule above it and its name un-muted, so
  a run of one project is separable from the next at a glance. Only under that
  sort: under any other, the rows are not grouped by project and such a rule
  would divide nothing.
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
Edit/Write tool results leave in transcripts. Changes made via Bash (git
operations, scripts, generators) leave no diff in transcripts and aren't
counted. These columns update on ingest/reconcile, not via OTel (its tool
events carry no diffs).

**Subagent edits and the undo history.** A subagent's transcript records its
Edit and Write *calls* but not their results — there is no `toolUseResult` on
those turns, so there is no `structuredPatch` to measure. What Claude Code
does keep is its own undo history:

```
~/.claude/file-history/<session-id>/<hash>@v<N>
```

a complete copy of a file at each checkpoint, where `<hash>` is the first 16
hex digits of the SHA-256 of the file's absolute path. Where two consecutive
versions of a file survive, Claude Lens diffs them and records the change with
`edits.source = 'file-history'`. It only does so for sessions that actually
have unmeasured subagent edits, and only for files that session has no `edits`
row for at all, so nothing is counted twice.

Recovery is best-effort by nature: only the newest version or two of a file is
kept, so the earliest change to a file usually has no earlier version to diff
against, and a run of edits between two checkpoints is recovered as one net
change attributed to the prompt open at the later checkpoint. See ROADMAP.md.

## How big a tool call was

`tool_calls.input_bytes` is the length of the call's input serialised as
compact JSON; `result_bytes` is the length of what came back — the characters
in the `tool_result` block, or `toolUseResult.stdout` + `stderr` where that is
larger, since a Bash result's visible block is only a summary of its output.
Both are **exact, decoded sizes**, not the size of the line in the transcript:
a transcript line carries the result twice over for Bash calls and inflates
everything else with JSON escaping, so line length runs a median of 2.7x and a
mean of 9.3x the real figure. Measuring the result properly costs about 0.4s
on a 450 MB tree, which is the price of the column being usable.

`is_error` is set from the `tool_result` block's own flag or from an `error` /
`isError` on `toolUseResult`. OTel fills `duration_ms` and `error_type` for
the same call; neither source overwrites the other, each only fills what is
still NULL.

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
| 7 | `api_requests.provider` and `.model_raw` — model ids are stored canonically with the original kept alongside, so Bedrock and Vertex ids resolve against the pricing table. Existing rows are normalised in place |
| 8 | Per-request detail: `api_requests` gains `effort`, `speed`, `thinking_tokens`, `stop_reason`, `server_tool_requests`, `service_tier`, `inference_geo`, `context_tokens`, `cost_basis` and `error`; `tool_calls` gains `input_bytes`, `result_bytes`, `is_error`, `duration_ms` and `error_type`; `sessions` gains `git_branch`, `cli_version`, `entrypoint`, `permission_mode`, `transcript_path`, `first_ts` and `last_ts`; `prompts` gains `kind`. New tables `agents` (one row per subagent launch, with the requested and resolved model) and `session_events` (context readings, compactions, model/effort/speed switches). Transcript rows stop being insert-or-ignore: a re-parse may raise an existing transcript row but never lower it, and never touches an OTel row. Clears `ingest_state` to force that re-parse, and strips the `agent-` filename prefix from `agent_name` so subagent rows join `agents` |

## Known limitations

- Headless `claude -p` prompts that predate OTel enablement show
  "(prompt text unavailable)"; live sessions carry text via OTel.
- Format handling is derived from transcripts written by CLI 2.1.168 and
  later. Much older layouts may still parse — nothing assumes a version — but
  they have not been tested against real data.
- A brand-new session may show project `?` until the next ingest maps its
  session to a working directory.
- Claude Chat conversations are not collectable: they live server-side, and
  the desktop app keeps no local per-conversation or token record. Its
  `plan-usage-history.json` *is* read, but only as an account-wide rate-limit
  gauge — it carries no per-product or per-conversation breakdown, so it can
  never say what Chat itself cost.
- Remote collection needs a POSIX remote reachable with key-based SSH;
  Windows remotes have to go through a shared folder and `--extra-dir`.
- Remote rows are only as fresh as the last fetch — the live OTel receiver
  covers this machine, not the others.
- A remote parked by backoff stays stale until its retry window opens; run
  `--remote HOST` explicitly (or check `--remote-status`) if that is a
  surprise.
- Costs for backfilled rows are estimates (`~` prefix); live rows use the
  CLI's own figure. Keep `pricing.py` current as models change, and put local
  corrections — a region's Bedrock rate card, a deployment-ARN mapping — in
  `pricing.local.json` rather than editing the table. Any model with no rate
  is named on the page and counted as $0.00 rather than guessed at.
- Bedrock rates here default to Anthropic list prices and are unverified
  against a live account; Provisioned Throughput and batch billing cannot be
  estimated per token at all. Claude Sonnet 5 is the one entry where the
  partner card is stated separately (`pricing.PARTNER_PRICES`), because its
  $2/$10 is a first-party rate.
- The data-residency premium is a single 1.1x, applied to input and output
  alike, for every model that supports pinning. No per-model figure is
  published; the number is stated in `pricing.py` rather than buried in a
  formula so it is one line to correct.
