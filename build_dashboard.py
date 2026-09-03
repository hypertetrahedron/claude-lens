"""Aggregate metrics.db into a self-contained static dashboard.html.

One row per user prompt (injected harness turns folded into their parent
prompt), with per-model token/cost breakdowns, tool-call counts, file-edit
stats, and subagent attribution. All filtering/summarizing happens client-side
in the template. `collect()` is shared with digest.py.
"""
import argparse
import heapq
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import conversations
import db
import pricing
import report_index
import sources

# Product a row came from, used by the dashboard's product selector.
CODE_KIND = "code"
COWORK_KIND = "cowork"

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE, "template.html")
OUTPUT = os.path.join(BASE, "dashboard.html")

MAX_TEXT = 400
WINDOW_HOURS = 5          # Anthropic rate-limit window length
WINDOW_LOOKBACK_H = 36    # how far back to scan when locating the current window

# The payload is parsed by the browser on every load and every auto-refresh,
# and each prompt costs roughly a kilobyte. Newest rows are kept; the UI says
# so when anything was dropped, so a shrinking "All" view is never a mystery.
DEFAULT_MAX_ROWS = 8000

# How many of the newest prompts get a conversation page written for them.
# Each page is a few tens of kilobytes and is only rewritten when its
# transcript moved, so this is a disk budget rather than a payload one.
DEFAULT_CONVERSATIONS = 300

# ccusage-style billing blocks: a block opens on the first request after the
# previous one closed and runs for five hours. Only the recent past is worth
# computing - a block from last spring cannot be spent - so the scan is
# bounded, and the burn rate for the open block is measured over half an hour.
BLOCK_HOURS = 5
BLOCK_DAYS = 30
BURN_WINDOW_MIN = 30

# Cache-miss classification. A request is a miss when most of its input was
# written to the cache rather than read from it, and the request before it did
# read from the cache - i.e. there was a warm cache and this request missed it.
# The prompt cache lives 60 minutes for the main conversation on the Anthropic
# API and 5 minutes everywhere else, which is what makes an idle gap a cause.
MISS_SHARE = 0.5
MAIN_TTL_MIN = 60
OTHER_TTL_MIN = 5

# session_events kinds that can explain a cache miss, in the order they are
# checked; "compact" is the only one that is not also visible on the requests.
MISS_EVENT_KINDS = ("compact", "model_switch", "effort_switch", "speed_switch")

# The context series carries one point per main-conversation request, so a
# long history is a lot of points. Past the cap whole sessions are dropped,
# oldest first, and the page says so.
CTX_CAP = 200000

# Anthropic's published figure for what Claude Code costs per active developer
# per day, so a user can tell "expensive" from "ordinary" without guessing.
BASELINE = {"usd_per_active_day": 13, "p90": 30,
            "source": "https://code.claude.com/docs/en/costs"}

# Everything collect() works out that is not a per-prompt row: the context
# series, per-session and per-prompt cache-miss figures, billing blocks, tool
# error rates, the tool-use system prompt overhead and the cost basis. Held
# here rather than returned so collect()'s two-value contract - which digest.py
# and the tests rely on - does not change. build() reads it straight after.
EXTRAS = {}

# Models seen during the last collect() that pricing.py has no entry for,
# as {model: {"rows", "uncosted_rows", "tokens"}}. A new model launch lands
# here: backfilled rows fall back to $0.00 and OTel rows keep their reported
# total but lose the cost-composition split, so both under-report silently
# unless we say something. Populated by collect(), reported by warn_unpriced().
UNPRICED = {}

# Rows whose cost was replaced by a CLI-reported figure during the last
# collect(); reported by build() so a run says how much of it is exact.
REPRICED = {"rows": 0}

# Provider -> request count from the last collect(). Bedrock and Vertex users
# have no Claude subscription, so the plan gauges and the 5h rate-limit block
# describe nothing for them and the dashboard hides those tiles.
PROVIDERS = {}


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def note_unpriced(model, tokens, uncosted, provider=None):
    e = UNPRICED.setdefault(model or "?",
                            {"rows": 0, "uncosted_rows": 0, "tokens": 0,
                             "provider": provider})
    e["rows"] += 1
    e["uncosted_rows"] += int(uncosted)
    e["tokens"] += tokens
    if provider and not e.get("provider"):
        e["provider"] = provider


def warn_unpriced(stream=sys.stderr):
    """Print a warning for unpriced models that actually billed tokens.

    Zero-token entries are placeholders rather than real models (Claude Code
    writes '<synthetic>' rows for harness-generated turns), so they are ignored
    to keep the warning free of false positives.
    """
    real = {m: e for m, e in UNPRICED.items() if e["tokens"] > 0}
    if not real:
        return real
    # ASCII only: this goes to a console that may be cp1252 (Windows default).
    print(f"WARNING: no pricing entry for {len(real)} model(s) in pricing.py.",
          file=stream)
    for m, e in sorted(real.items(), key=lambda kv: -kv[1]["tokens"]):
        note = (f", {e['uncosted_rows']:,} counted as $0.00"
                if e["uncosted_rows"] else ", cost breakdown omitted")
        print(f"  {m:<28} {e['rows']:>6,} rows, "
              f"{e['tokens'] / 1e6:.1f}M tokens{note}", file=stream)
    print("  Add them to PRICES; the next build reprices all history.",
          file=stream)
    return real


def resolve_map(con):
    """prompt_id -> canonical prompt_id (folding injected turns)."""
    m = {}
    for pid, canon, injected in con.execute(
            "SELECT prompt_id, canonical_id, injected FROM prompts"):
        m[pid] = canon if (injected and canon) else pid
    return m


def compute_window(recent):
    """ccusage-style 5h blocks: a block starts at the floored hour of the first
    request after the previous block ends. Returns the block containing now,
    or None if idle. `recent` = [(dt, out_tokens, cost), ...]."""
    if not recent:
        return None
    recent.sort(key=lambda x: x[0])
    now = datetime.now(timezone.utc)
    block_start = block_end = None
    stats = None
    for dt, out, cost in recent:
        if block_end is None or dt >= block_end:
            block_start = dt.replace(minute=0, second=0, microsecond=0)
            block_end = block_start + timedelta(hours=WINDOW_HOURS)
            stats = {"out": 0, "cost": 0.0, "requests": 0}
        stats["out"] += out
        stats["cost"] += cost
        stats["requests"] += 1
    if block_end and now < block_end:
        return {
            "start": block_start.isoformat(timespec="seconds"),
            "end": block_end.isoformat(timespec="seconds"),
            **{k: (round(v, 4) if k == "cost" else v) for k, v in stats.items()},
        }
    return None


def _since_clause(sql, since, args=()):
    """`sql` with a ts lower bound appended, and the parameter tuple for it."""
    if not since:
        return sql, tuple(args)
    return sql + " AND ts >= ?", tuple(args) + (since,)


def cache_write_cost(rate, c5, c1, unsplit, provider):
    """USD for one request's cache-creation tokens alone."""
    mult = pricing.UNSPLIT_CACHE_MULT.get(provider or pricing.ANTHROPIC,
                                          pricing.CACHE_WRITE_1H_MULT)
    return (c5 * pricing.CACHE_WRITE_5M_MULT
            + c1 * pricing.CACHE_WRITE_1H_MULT
            + unsplit * mult) * rate.inp / 1e6


def cache_scan(con, since=None):
    """One ordered pass over main-conversation requests.

    Two things come out of it that a GROUP BY cannot produce, because both
    depend on what the *previous* request in the same session did:

    - the context series the dashboard plots (one point per request, carrying
      the measured `context_tokens` rather than anything inferred);
    - whether each request missed the prompt cache, and why.

    A miss is a request whose input was mostly written to the cache while the
    request before it was reading from one: the cache was warm and this
    request did not use it. The cause is the first explanation that fits -
    the model, effort or speed changed under the session, a compaction landed
    between the two, or the session simply sat idle past the cache's TTL.
    "unknown" is left as itself rather than guessed at.

    Returns (per_prompt, per_session, ctx) where per_prompt maps a prompt id
    to [misses, miss_cost] and ctx maps a session id to a list of
    [t, ctx, cr, cw, model, miss, cause, event] points.

    Subagent requests are excluded: they run against their own context, so
    mixing them into the session's series would draw a sawtooth that never
    happened.
    """
    events = defaultdict(list)
    for sid, ts, kind in con.execute(
            "SELECT session_id, ts, kind FROM session_events "
            "WHERE kind IN (?,?,?,?) ORDER BY session_id, ts",
            MISS_EVENT_KINDS):
        if sid:
            events[sid].append((ts or "", kind))

    sql, args = _since_clause(
        """SELECT session_id, prompt_id, ts, model, provider, effort, speed,
                  inference_geo, COALESCE(cache_read_tokens, 0),
                  COALESCE(cache_create_tokens, 0),
                  COALESCE(cache_5m_tokens, 0), COALESCE(cache_1h_tokens, 0),
                  COALESCE(context_tokens, 0)
             FROM api_requests
            WHERE session_id IS NOT NULL
              AND (query_source IS NULL OR query_source = 'main')""", since)
    sql += " ORDER BY session_id, ts"

    resolve = pricing.resolve
    per_prompt = defaultdict(lambda: [0, 0.0])
    per_session = {}
    ctx = {}
    cur = prev = None
    evs, ei, points, stats = [], 0, [], None

    for (sid, pid, ts, model, provider, effort, speed, geo, cr, cw, c5, c1,
         ctx_tokens) in con.execute(sql, args):
        if sid != cur:
            cur, prev = sid, None
            evs, ei = events.get(sid) or [], 0
            points = ctx.setdefault(sid, [])
            stats = per_session.setdefault(sid, {
                "misses": 0, "miss_cost": 0.0, "peak_ctx": 0,
                "compactions": sum(1 for _, k in evs if k == "compact"),
                "switches": sum(1 for _, k in evs if k == "model_switch")})
        # Events that fell between the previous request and this one. The
        # pointer only moves forward, so the whole session costs one pass.
        seen = []
        while ei < len(evs) and evs[ei][0] <= (ts or ""):
            seen.append(evs[ei][1])
            ei += 1
        dt = parse_ts(ts)
        if ctx_tokens > stats["peak_ctx"]:
            stats["peak_ctx"] = ctx_tokens
        miss, cause = 0, None
        if prev is not None and cw > MISS_SHARE * (cr + cw) and prev[0] > 0:
            miss = 1
            ttl = (MAIN_TTL_MIN
                   if provider in (None, pricing.ANTHROPIC) else OTHER_TTL_MIN)
            gap = ((dt - prev[4]).total_seconds() / 60.0
                   if (dt and prev[4]) else None)
            if model != prev[1]:
                cause = "model_switch"
            elif effort != prev[2]:
                cause = "effort_switch"
            elif speed != prev[3]:
                cause = "speed_switch"
            elif "compact" in seen:
                cause = "compact"
            elif gap is not None and gap > ttl:
                cause = "idle_gap"
            else:
                cause = "unknown"
            rate = resolve(model, ts, provider, speed, geo)
            if rate is not None:
                cost = cache_write_cost(rate, c5, c1, max(cw - c5 - c1, 0),
                                        provider)
                stats["miss_cost"] += cost
                if pid:
                    per_prompt[pid][1] += cost
            stats["misses"] += 1
            if pid:
                per_prompt[pid][0] += 1
        points.append([int(dt.timestamp()) if dt else None, ctx_tokens, cr, cw,
                       model, miss, cause, seen[0] if seen else None])
        prev = (cr, model, effort, speed, dt)
    return per_prompt, per_session, ctx


def compute_blocks(con, now=None, days=BLOCK_DAYS):
    """ccusage-style 5h billing blocks over the recent past, newest last.

    Same rule as compute_window(): a block opens at the floored hour of the
    first request after the previous block closed, and runs BLOCK_HOURS. The
    open block also gets a burn rate - tokens and dollars per minute over the
    last BURN_WINDOW_MIN - and a projection of what it will have cost by the
    time it closes, which is the number that tells you to slow down while
    there is still something to slow down for.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_day = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")
    burn_since = now - timedelta(minutes=BURN_WINDOW_MIN)
    cost_at = pricing.cost_at
    # A month of a heavy history is tens of thousands of requests, and all but
    # a handful share one of a dozen (model, provider, speed, region, day)
    # combinations - so the Rate is looked up once per combination rather than
    # once per request. The day is in the key because promotional pricing
    # turns over at a date boundary.
    rates = {}

    blocks = []
    end = None
    burn_tokens = burn_usd = 0.0
    for (ts, model, provider, speed, geo, inp, out, cr, cw, c5, c1,
         cost) in con.execute(
            """SELECT ts, model, provider, speed, inference_geo,
                      COALESCE(input_tokens, 0), COALESCE(output_tokens, 0),
                      COALESCE(cache_read_tokens, 0),
                      COALESCE(cache_create_tokens, 0),
                      COALESCE(cache_5m_tokens, 0),
                      COALESCE(cache_1h_tokens, 0), cost_usd
                 FROM api_requests WHERE ts >= ? ORDER BY ts""",
            (cutoff_day,)):
        dt = parse_ts(ts)
        if not dt or dt < cutoff:
            continue
        if cost is None:
            key = (model, provider, speed, geo, ts[:10] if ts else None)
            try:
                rate = rates[key]
            except KeyError:
                rate = rates[key] = pricing.resolve(model, ts, provider, speed,
                                                    geo)
            cost = 0.0 if rate is None else cost_at(
                rate, inp, out, cr, c5, c1, max(cw - c5 - c1, 0), provider)
        if end is None or dt >= end:
            start = dt.replace(minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=BLOCK_HOURS)
            blocks.append({"start": start, "end": end, "calls": 0,
                           "cost": 0.0, "out": 0, "inp": 0, "cr": 0, "cw": 0,
                           "models": set()})
        b = blocks[-1]
        b["calls"] += 1
        b["cost"] += cost
        b["out"] += out
        b["inp"] += inp + cr + cw
        b["cr"] += cr
        b["cw"] += cw
        if model:
            b["models"].add(model)
        # The burn rate only ever looks at the last half hour, so only that
        # much is kept - the alternative held every request of the month.
        if dt >= burn_since:
            burn_tokens += out + inp + cr + cw
            burn_usd += cost

    burn = None
    for b in blocks:
        b["active"] = now < b["end"]
    if blocks and blocks[-1]["active"]:
        b = blocks[-1]
        left = max((b["end"] - now).total_seconds() / 60.0, 0.0)
        burn = {
            "window_min": BURN_WINDOW_MIN,
            "minutes_left": round(left, 1),
            "tokens_per_min": round(burn_tokens / BURN_WINDOW_MIN, 1),
            "usd_per_min": round(burn_usd / BURN_WINDOW_MIN, 6),
            "projected_cost": round(
                b["cost"] + burn_usd / BURN_WINDOW_MIN * left, 4),
        }
    return blocks, burn


def tool_errors(con, since=None):
    """Per-tool call/error counts, plus whatever the API itself refused."""
    sql, args = _since_clause(
        "SELECT tool_name, COUNT(*), SUM(COALESCE(is_error, 0)) "
        "FROM tool_calls WHERE 1=1", since)
    tools = []
    for name, calls, errs in con.execute(sql + " GROUP BY tool_name", args):
        errs = errs or 0
        if calls:
            tools.append([name or "?", calls, errs, round(errs / calls, 4)])
    tools.sort(key=lambda t: (-t[2], -t[1], t[0]))
    sql, args = _since_clause(
        "SELECT error, COUNT(*) FROM api_requests WHERE error IS NOT NULL",
        since)
    kinds = [[str(e), n] for e, n in con.execute(sql + " GROUP BY error", args)]
    kinds.sort(key=lambda k: -k[1])
    return {"tools": tools, "api": sum(k[1] for k in kinds),
            "api_kinds": kinds[:10]}


def tool_overhead(con, since=None):
    """The tool-use system prompt, which every request pays for again.

    Claude Code re-sends its tool definitions on every request; the published
    per-model token counts live in pricing.TOOL_PROMPT_TOKENS. Charged at the
    uncached input rate, which over-states it whenever the block was cached -
    it usually is - so this is an upper bound and is labelled as one.
    """
    sql, args = _since_clause(
        "SELECT model, COUNT(*), MIN(ts), provider FROM api_requests "
        "WHERE model IS NOT NULL", since)
    by_model, tokens, usd = [], 0, 0.0
    for model, n, ts, provider in con.execute(
            sql + " GROUP BY model, provider", args):
        per = pricing.tool_prompt_tokens(model)
        t = per * n
        rate = pricing.resolve(model, ts, provider)
        c = 0.0 if rate is None else t * rate.inp / 1e6
        by_model.append([model, n, per, t, round(c, 4)])
        tokens += t
        usd += c
    by_model.sort(key=lambda m: -m[3])
    return {"by_model": by_model, "tokens": tokens, "usd": round(usd, 4)}


def cost_basis(con, since=None):
    """"list", "contracted", "mixed" or None over the rows that declare one."""
    sql, args = _since_clause(
        "SELECT cost_basis, COUNT(*) FROM api_requests "
        "WHERE cost_basis IS NOT NULL", since)
    seen = {b: n for b, n in con.execute(sql + " GROUP BY cost_basis", args)}
    if not seen:
        return None
    if len(seen) == 1:
        return next(iter(seen))
    return "mixed"


def collect(con, since=None):
    """Aggregate the DB into per-prompt rows + current-window stats.

    `since` is an ISO timestamp lower bound applied to every table this reads,
    so a caller that only wants a fortnight (digest.py) does not pay for a
    year. It is a *prefilter*, not a window: a prompt whose own timestamp
    falls before `since` loses the requests that came after it, so callers
    pass a margin and do their own windowing on the rows that come back.

    Everything that is not a per-prompt row - the context series, cache-miss
    classification, billing blocks, error rates, overhead - lands in EXTRAS.
    """
    UNPRICED.clear()
    EXTRAS.clear()
    # Display name for a project: the basename of the session's working
    # directory (portable), falling back to the transcript-folder slug.
    #
    # Sessions from anywhere but the primary ~/.claude carry a source_label
    # (a remote host name, or a second .claude* directory). That label is
    # prepended here because cwd basenames collide freely across machines -
    # every box has a `src` or a `web` - and because the origin is worth
    # seeing. Stored project slugs are already qualified by the ingester, so
    # slug_display keys stay unique per source.
    session_project = {}
    session_kind = {}
    session_title = {}
    session_meta = {}
    slug_display = {}
    for sid, proj, cwd, label, title, first_ts, last_ts in con.execute(
            "SELECT session_id, project, cwd, source_label, title, "
            "first_ts, last_ts FROM sessions"):
        if title:
            session_title[sid] = title
        session_meta[sid] = {"first_ts": first_ts, "last_ts": last_ts}
        label = label or ""
        base = os.path.basename((cwd or "").rstrip("\\/"))
        disp = f"{label}/{base}" if (label and base) else (base or proj)
        session_project[sid] = disp
        # Which Claude product produced this row. Recorded explicitly rather
        # than inferred from the name prefix in the UI, so a local folder that
        # happens to be called "cowork" can't be mistaken for the desktop app.
        session_kind[sid] = COWORK_KIND if label == sources.COWORK_LABEL else CODE_KIND
        if proj and disp:
            slug_display[proj] = disp
    canon = resolve_map(con)

    rows = {}

    def new_row(pid):
        return {
            "id": pid, "ts": "", "project": "?", "session": None,
            "text": "", "injected": 0,
            "models": defaultdict(lambda: {"in": 0, "out": 0, "cr": 0, "cw": 0,
                                           "cost": 0.0, "calls": 0}),
            "tools": defaultdict(int), "agents": set(),
            "api_calls": 0, "cost": 0.0, "est": False, "last_ts": "",
            "files": defaultdict(lambda: [0, 0]), "chars": 0, "agent_out": 0,
            "comp": [0.0, 0.0, 0.0, 0.0],  # $: cache_read, cache_write, output, uncached_in
            "alt": 0.0,                    # counterfactual: same traffic, no caching
            # v2 payload fields
            "effort_calls": defaultdict(int), "thinking": 0, "fast_calls": 0,
            "errors": 0, "max_tokens_stops": 0, "web_searches": 0,
            "peak_ctx": 0, "tool_bytes": defaultdict(int),
        }

    prompt_sql, prompt_args = _since_clause(
        "SELECT prompt_id, session_id, project, ts, text, injected "
        "FROM prompts WHERE 1=1", since)
    for pid, sid, project, ts, text, injected in con.execute(prompt_sql,
                                                             prompt_args):
        target = canon.get(pid, pid)
        if target != pid:
            continue  # injected + folded; its usage lands on the canonical row
        r = new_row(pid)
        r.update(ts=ts or "", last_ts=ts or "", session=sid,
                 project=project or session_project.get(sid) or "?",
                 text=(text or "")[:MAX_TEXT], injected=injected)
        rows[pid] = r

    def bucket(pid):
        target = canon.get(pid, pid)
        if target not in rows:
            r = new_row(target)
            r["text"] = "(prompt text unavailable)"
            rows[target] = r
        return rows[target]

    window_cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_LOOKBACK_H)
    # A date one day either side of the cutoff, for a string prefilter that no
    # UTC offset (max +/-14h) can make wrong. It keeps the 5h-window scan off
    # every request ever recorded, and off parse_ts entirely.
    cutoff_day = (window_cutoff - timedelta(days=1)).strftime("%Y-%m-%d")
    recent = []

    resolve = pricing.resolve
    cost_at = pricing.cost_at
    w5m, w1h = pricing.CACHE_WRITE_5M_MULT, pricing.CACHE_WRITE_1H_MULT

    # API usage is aggregated in SQLite, not in Python. Everything a prompt row
    # needs from api_requests is a sum over (prompt, session, model, provider,
    # agent, priced-or-not, day), and there are two orders of magnitude fewer of
    # those than there are requests - 1.1k groups for 38k requests on the
    # benchmark tree. The day is in the key so promotional pricing, which turns
    # over at a date boundary, still applies exactly.
    orphan_ts = {}
    providers = defaultdict(int)
    req_sql, req_args = _since_clause(
        """SELECT prompt_id, session_id, model, provider, agent_name,
                  cost_usd IS NULL, effort, speed, inference_geo, COUNT(*),
                  SUM(COALESCE(input_tokens, 0)),
                  SUM(COALESCE(output_tokens, 0)),
                  SUM(COALESCE(cache_read_tokens, 0)),
                  SUM(COALESCE(cache_create_tokens, 0)),
                  SUM(COALESCE(cache_5m_tokens, 0)),
                  SUM(COALESCE(cache_1h_tokens, 0)),
                  SUM(MAX(COALESCE(cache_create_tokens, 0)
                          - COALESCE(cache_5m_tokens, 0)
                          - COALESCE(cache_1h_tokens, 0), 0)),
                  SUM(cost_usd), MIN(ts), MAX(ts),
                  SUM(COALESCE(thinking_tokens, 0)),
                  SUM(COALESCE(server_tool_requests, 0)),
                  SUM(CASE WHEN stop_reason = 'max_tokens' THEN 1 ELSE 0 END),
                  MAX(COALESCE(context_tokens, 0))
           FROM api_requests WHERE prompt_id IS NOT NULL""", since)
    # effort/speed/inference_geo join the key because they move the price:
    # fast mode is billed at 2x and a pinned US region at 1.1x, so a group
    # that mixed them would be costed at whichever one the row happened to
    # carry. They are near-constant within a session, so the group count
    # barely moves.
    for (pid, sid, model, provider, agent, nocost, effort, speed, geo, calls,
         inp, out, cr, cw, c5, c1, unsplit, cost, ts_min, ts_max, thinking,
         server_tools, max_stops, peak_ctx) in con.execute(
            req_sql + """
               GROUP BY prompt_id, session_id, model, provider, agent_name,
                        cost_usd IS NULL, effort, speed, inference_geo,
                        substr(ts, 1, 10)""", req_args):
        if provider:
            providers[provider] += calls
        r = bucket(pid)
        if r["project"] == "?" and sid in session_project:
            r["project"] = session_project[sid] or "?"
        m = r["models"][model]
        m["in"] += inp
        m["out"] += out
        m["cr"] += cr
        m["cw"] += cw
        m["calls"] += calls
        r["api_calls"] += calls
        if effort:
            r["effort_calls"][effort] += calls
        if speed == "fast":
            r["fast_calls"] += calls
        r["thinking"] += thinking or 0
        r["web_searches"] += server_tools or 0
        r["max_tokens_stops"] += max_stops or 0
        if (peak_ctx or 0) > r["peak_ctx"]:
            r["peak_ctx"] = peak_ctx or 0
        rate = resolve(model, ts_min, provider, speed, geo)
        if rate is None:
            note_unpriced(model, inp + out + cr + cw,
                          uncosted=bool(nocost), provider=provider)
        if nocost:
            cost = 0.0 if rate is None else cost_at(
                rate, inp, out, cr, c5, c1, unsplit, provider)
            r["est"] = True
        m["cost"] += cost
        r["cost"] += cost
        # Cost components from the pricing table; when the CLI reported an
        # authoritative total, scale the split so components sum to it.
        if rate is not None:
            pi, po = rate.inp, rate.out
            c_read = cr * pi * rate.cache_read_mult / 1e6
            c_write = (c5 * w5m + (c1 + unsplit) * w1h) * pi / 1e6
            c_out = out * po / 1e6
            c_in = inp * pi / 1e6
            est_total = sum((c_read, c_write, c_out, c_in))
            if est_total > 0 and cost > 0:
                f = cost / est_total
                c_read *= f
                c_write *= f
                c_out *= f
                c_in *= f
            rc = r["comp"]
            rc[0] += c_read
            rc[1] += c_write
            rc[2] += c_out
            rc[3] += c_in
            r["alt"] += ((cr + cw + inp) * pi + out * po) / 1e6
        if agent:
            r["agents"].add(agent)
            r["agent_out"] += out
        if ts_max and ts_max > r["last_ts"]:
            r["last_ts"] = ts_max
        if ts_min:
            prev = orphan_ts.get(r["id"])
            if prev is None or ts_min < prev:
                orphan_ts[r["id"]] = ts_min

    # A prompt whose own row never reached the DB takes its start time from its
    # earliest request.
    for rid, t in orphan_ts.items():
        r = rows[rid]
        if not r["ts"]:
            r["ts"] = t

    # The 5h rate-limit block needs individual request times, but only for the
    # last day and a half, so it is its own small query rather than a field on
    # every row of the one above.
    for ts, model, provider, speed, geo, out, cost, inp, cr, cw, c5, c1 in \
            con.execute(
            """SELECT ts, model, provider, speed, inference_geo,
                      COALESCE(output_tokens, 0), cost_usd,
                      COALESCE(input_tokens, 0), COALESCE(cache_read_tokens, 0),
                      COALESCE(cache_create_tokens, 0),
                      COALESCE(cache_5m_tokens, 0), COALESCE(cache_1h_tokens, 0)
               FROM api_requests
               WHERE prompt_id IS NOT NULL AND ts >= ?""", (cutoff_day,)):
        dt = parse_ts(ts)
        if not dt or dt < window_cutoff:
            continue
        if cost is None:
            unsplit = cw - c5 - c1
            if unsplit < 0:
                unsplit = 0
            rate = resolve(model, ts, provider, speed, geo)
            cost = 0.0 if rate is None else cost_at(
                rate, inp, out, cr, c5, c1, unsplit, provider)
        recent.append((dt, out, cost))

    tool_sql, tool_args = _since_clause(
        "SELECT prompt_id, tool_name, detail, agent_name, COUNT(*), "
        "SUM(COALESCE(result_bytes, 0)), SUM(COALESCE(is_error, 0)) "
        "FROM tool_calls WHERE prompt_id IS NOT NULL", since)
    for pid, name, detail, agent, n, rbytes, errs in con.execute(
            tool_sql + " GROUP BY prompt_id, tool_name, detail, agent_name",
            tool_args):
        r = bucket(pid)
        display = f"Skill:{detail}" if (name == "Skill" and detail) else (name or "?")
        r["tools"][display] += n
        r["tool_bytes"][display] += rbytes or 0
        r["errors"] += errs or 0
        if agent:
            r["agents"].add(agent)

    edit_sql, edit_args = _since_clause(
        """SELECT prompt_id, file_path, SUM(COALESCE(lines_added, 0)),
                  SUM(COALESCE(lines_removed, 0)),
                  SUM(COALESCE(chars_added, 0)), agent_name
           FROM edits WHERE prompt_id IS NOT NULL""", since)
    for pid, path, add, rem, chars, agent in con.execute(
            edit_sql + " GROUP BY prompt_id, file_path, agent_name",
            edit_args):
        r = bucket(pid)
        f = r["files"][path or "?"]
        f[0] += add
        f[1] += rem
        r["chars"] += chars
        if agent:
            r["agents"].add(agent)

    # A CLI-reported session cost is exact, but only for runs that finished
    # and reported one. Spending it when it covers fewer runs than the session
    # actually has would silently under-report, so it is applied only where
    # the run count matches the prompts we found - otherwise the estimate,
    # which at least covers everything, stands.
    emitted = defaultdict(list)
    for r in rows.values():
        if (r["api_calls"] or r["tools"]) and r["session"]:
            emitted[r["session"]].append(r)
    repriced = 0
    for sid, auth_cost, runs in con.execute(
            "SELECT session_id, cost_usd, runs FROM run_cost"):
        group = emitted.get(sid) or []
        est_total = sum(g["cost"] for g in group)
        if not group or not auth_cost or runs != len(group) or est_total <= 0:
            continue
        factor = auth_cost / est_total
        for g in group:
            g["cost"] *= factor
            g["comp"] = [c * factor for c in g["comp"]]
            g["est"] = False
        repriced += len(group)
    REPRICED["rows"] = repriced

    # Subagent identity: `agents.agent_id` is what api_requests.agent_name and
    # tool_calls.agent_name carry, so the opaque id a row collected can be
    # traded for the type that was launched and the model it actually ran on.
    agent_meta = {}
    for aid, kind, resolved, requested in con.execute(
            "SELECT agent_id, subagent_type, resolved_model, requested_model "
            "FROM agents"):
        agent_meta[aid] = (kind, resolved or requested)

    per_prompt_cache, per_session_cache, ctx_series = cache_scan(con, since)

    out_rows = []
    for r in rows.values():
        if not r["api_calls"] and not r["tools"]:
            continue
        wall = None
        t0, t1 = parse_ts(r["ts"]), parse_ts(r["last_ts"])
        if t0 and t1 and t1 >= t0:
            wall = round((t1 - t0).total_seconds())
        models = [
            dict(v, model=k, cost=round(v["cost"], 6)) for k, v in
            sorted(r["models"].items(), key=lambda kv: (-kv[1]["out"], kv[0]))
        ]
        file_list = heapq.nlargest(
            40, ([p, a, d] for p, (a, d) in r["files"].items()),
            key=lambda x: (x[1] + x[2], x[0]))
        project = slug_display.get(r["project"],
                                   session_project.get(r["session"],
                                                       r["project"] or "?"))
        kind = session_kind.get(r["session"])
        if kind is None:      # prompt whose session never made it into the DB
            kind = (COWORK_KIND
                    if project.startswith(sources.COWORK_LABEL + "/")
                    else CODE_KIND)
        tools = sorted(r["tools"].items(), key=lambda kv: (-kv[1], kv[0]))
        # What a tool cost, as far as anything can say. A tool's result enters
        # the context once and is then re-read on every later request of the
        # turn, so the turn's whole input-side bill (cache read + cache write +
        # uncached input) is shared out in proportion to result bytes. It is a
        # share-out, not a measurement: the totals are exact, the split is an
        # attribution, and a tool whose result was never recorded falls back to
        # its share of the call count.
        input_cost = r["comp"][0] + r["comp"][1] + r["comp"][3]
        total_bytes = sum(r["tool_bytes"].values())
        total_calls = sum(n for _, n in tools)
        tool_attrib = []
        for name, n in tools:
            rb = r["tool_bytes"].get(name, 0)
            if total_bytes:
                share = rb / total_bytes
            else:
                share = (n / total_calls) if total_calls else 0.0
            tool_attrib.append([name, n, rb, round(input_cost * share, 6)])
        misses, miss_cost = per_prompt_cache.get(r["id"], (0, 0.0))
        agent_ids = sorted(r["agents"])
        efforts = r["effort_calls"]
        row = {
            "id": r["id"],
            "ts": r["ts"],
            "project": project,
            "kind": kind,
            "text": r["text"],
            "models": models,
            "tools": tools,
            "agents": agent_ids,
            "agent_info": [[a, agent_meta.get(a, (None, None))[0],
                            agent_meta.get(a, (None, None))[1]]
                           for a in agent_ids],
            "session": r["session"],
            "conv": None,
            "effort": (max(efforts.items(), key=lambda kv: (kv[1], kv[0]))[0]
                       if efforts else None),
            "thinking": r["thinking"],
            "fast_calls": r["fast_calls"],
            "errors": r["errors"],
            "max_tokens_stops": r["max_tokens_stops"],
            "web_searches": r["web_searches"],
            "peak_ctx": r["peak_ctx"],
            "misses": misses,
            "cache_miss_cost": round(miss_cost, 6),
            "tool_attrib": tool_attrib,
            "api_calls": r["api_calls"],
            "cost": round(r["cost"], 4),
            "est": r["est"],
            "wall_s": wall,
            "out": sum(m["out"] for m in models),
            "inp": sum(m["in"] + m["cr"] + m["cw"] for m in models),
            "cr": sum(m["cr"] for m in models),
            "agent_out": r["agent_out"],
            "files": len(r["files"]),
            "ladd": sum(a for a, _ in r["files"].values()),
            "lrem": sum(d for _, d in r["files"].values()),
            "chars": r["chars"],
            "file_list": file_list,
            "comp": [round(c, 4) for c in r["comp"]],
            "alt": round(r["alt"], 4),
        }
        title = session_title.get(r["session"])
        if title and title != project:
            row["title"] = title
        out_rows.append(row)
    out_rows.sort(key=lambda r: r["ts"], reverse=True)
    warn_unpriced()
    PROVIDERS.clear()
    PROVIDERS.update(providers)

    blocks, burn = compute_blocks(con)
    subagent_ttl = {}
    for sid, c5, c1 in con.execute(
            "SELECT session_id, SUM(COALESCE(cache_5m_tokens, 0)), "
            "SUM(COALESCE(cache_1h_tokens, 0)) FROM api_requests "
            "WHERE query_source = 'subagent' GROUP BY session_id"):
        if (c5 or 0) or (c1 or 0):
            subagent_ttl[sid] = "5m" if (c5 or 0) >= (c1 or 0) else "1h"
    EXTRAS.update({
        "ctx": ctx_series,
        "session_cache": per_session_cache,
        "session_meta": session_meta,
        "session_title": session_title,
        "subagent_ttl": subagent_ttl,
        "blocks": blocks,
        "burn": burn,
        "errors": tool_errors(con, since),
        "overhead": tool_overhead(con, since),
        "cost_basis": cost_basis(con, since),
    })
    return out_rows, compute_window(recent)


def session_rows(out_rows, redact=False):
    """One row per session, aggregated from the prompt rows being embedded.

    Built from the rows rather than from SQL so a session's cost is exactly
    the sum of the costs the page shows for it, repricing and all - a session
    total that disagreed with its own prompts would be worse than none.
    """
    cache = EXTRAS.get("session_cache") or {}
    meta = EXTRAS.get("session_meta") or {}
    titles = EXTRAS.get("session_title") or {}
    ttl = EXTRAS.get("subagent_ttl") or {}
    acc = {}
    for r in out_rows:
        sid = r.get("session")
        if not sid:
            continue
        s = acc.get(sid)
        if s is None:
            s = acc[sid] = {
                "id": sid, "title": titles.get(sid), "project": r["project"],
                "kind": r["kind"], "start": r["ts"], "end": r["ts"],
                "prompts": 0, "calls": 0, "cost": 0.0, "est": False,
                "out": 0, "inp": 0, "cr": 0, "cw": 0, "uncached": 0,
                "models": set(), "effort": defaultdict(int), "peak_ctx": 0,
                "first_ts": r["ts"], "first_text": r["text"],
            }
        s["prompts"] += 1
        s["calls"] += r["api_calls"]
        s["cost"] += r["cost"]
        s["est"] = s["est"] or r["est"]
        s["out"] += r["out"]
        s["inp"] += r["inp"]
        s["cr"] += r["cr"]
        for m in r["models"]:
            s["cw"] += m["cw"]
            s["uncached"] += m["in"]
            if m["model"]:
                s["models"].add(m["model"])
        if r["effort"]:
            s["effort"][r["effort"]] += r["api_calls"] or 1
        if r["peak_ctx"] > s["peak_ctx"]:
            s["peak_ctx"] = r["peak_ctx"]
        if r["ts"] and r["ts"] < s["first_ts"]:
            s["first_ts"], s["first_text"] = r["ts"], r["text"]
        if r["ts"]:
            s["start"] = min(s["start"] or r["ts"], r["ts"])
            s["end"] = max(s["end"] or r["ts"], r["ts"])
    out = []
    for sid, s in acc.items():
        c = cache.get(sid) or {}
        m = meta.get(sid) or {}
        denom = s["cr"] + s["cw"] + s["uncached"]
        out.append({
            "id": sid,
            "title": s["title"],
            "project": s["project"],
            "kind": s["kind"],
            "start": m.get("first_ts") or s["start"],
            "end": m.get("last_ts") or s["end"],
            "prompts": s["prompts"],
            "calls": s["calls"],
            "cost": round(s["cost"], 4),
            "est": s["est"],
            "out": s["out"],
            "inp": s["inp"],
            "cr": s["cr"],
            "cw": s["cw"],
            "hit": round(s["cr"] / denom, 4) if denom else 0.0,
            "models": sorted(s["models"]),
            "switches": c.get("switches", 0),
            "effort": (max(s["effort"].items(), key=lambda kv: (kv[1], kv[0]))[0]
                       if s["effort"] else None),
            "compactions": c.get("compactions", 0),
            "peak_ctx": max(s["peak_ctx"], c.get("peak_ctx", 0)),
            "misses": c.get("misses", 0),
            "miss_cost": round(c.get("miss_cost", 0.0), 6),
            "subagent_ttl": ttl.get(sid),
            "first_prompt_text": "" if redact else (s["first_text"] or "")[:400],
        })
    out.sort(key=lambda s: (s["start"] or ""), reverse=True)
    return out


def ctx_points(keep_sessions):
    """The context series for `keep_sessions`, capped at CTX_CAP points.

    Past the cap whole sessions go, oldest first: half a session's context
    curve is a misleading picture, where a missing session is an honest one
    as long as the page says how many went. Returns (points, dropped).
    """
    series = EXTRAS.get("ctx") or {}
    order = []
    for sid in keep_sessions:
        pts = series.get(sid)
        if pts:
            order.append((pts[0][0] or 0, sid, pts))
    # Newest session first. The key is explicit because a tie on the start
    # time would otherwise fall through to comparing the point lists, which
    # can hold a None and would raise rather than sort.
    order.sort(key=lambda o: (o[0], o[1]), reverse=True)
    kept, total, dropped = [], 0, 0
    for _, sid, pts in order:
        if total + len(pts) > CTX_CAP:
            dropped += 1
            continue
        total += len(pts)
        for p in pts:
            kept.append([sid] + p)
    kept.sort(key=lambda p: (p[0], p[1] or 0))
    return kept, dropped


# ---------------------------------------------------------------------------
# Payload compaction.
#
# The browser re-parses this payload on every load and every auto-refresh, and
# the receiver rewrites the file about once a minute, so its size is a running
# cost rather than a one-off. Three things dominate it and none of them carry
# information: the key names, repeated once per row; the strings (project,
# model id, tool, agent, file path) that repeat across thousands of rows; and
# four fields that are exact sums of the per-model breakdown sitting next to
# them. So rows go out column-oriented, against a shared string table, with the
# derived fields dropped - and template.html puts the rows back together in one
# pass before any other code sees them.
# ---------------------------------------------------------------------------
COLUMNS = ("ts", "project", "kind", "text", "models", "tools", "agents",
           "cost", "est", "wall_s", "agent_out", "files", "ladd", "lrem",
           "chars", "file_list", "comp", "alt", "title",
           # v2
           "session", "conv", "effort", "thinking", "fast_calls", "errors",
           "max_tokens_stops", "web_searches", "peak_ctx", "misses",
           "cache_miss_cost", "tool_attrib")

# Column order of the `sessions` and `ctx` tables, which go out in the same
# column-oriented form as the rows and against the same string table. The
# template's rehydrate() turns each back into an array of objects using
# exactly these names, so adding one here is enough to make it visible.
SESSION_COLUMNS = ("id", "title", "project", "kind", "start", "end",
                   "prompts", "calls", "cost", "est", "out", "inp", "cr",
                   "cw", "hit", "models", "switches", "effort", "compactions",
                   "peak_ctx", "misses", "miss_cost", "subagent_ttl",
                   "first_prompt_text")

# What one point of the context series means once rehydrated. The wire form
# is smaller than this (see compact_ctx); the template puts it back.
CTX_COLUMNS = ("session", "t", "ctx", "cr", "cw", "model", "miss", "cause",
               "event")

BLOCK_COLUMNS = ("start", "end", "calls", "cost", "out", "inp", "cr", "cw",
                 "models", "active")


class Strings:
    """A shared string table: every table in the payload indexes into it."""

    def __init__(self):
        self.out = []
        self._seen = {}

    def __call__(self, v):
        if v is None:
            return None
        i = self._seen.get(v)
        if i is None:
            i = self._seen[v] = len(self.out)
            self.out.append(v)
        return i


def compact(out_rows, strings=None):
    """(columns, string table) for a list of collect() rows."""
    sid = strings if strings is not None else Strings()

    cols = {k: [] for k in COLUMNS}
    for r in out_rows:
        cols["session"].append(sid(r.get("session")))
        cols["conv"].append(r.get("conv"))
        cols["effort"].append(sid(r.get("effort")))
        cols["thinking"].append(r.get("thinking", 0))
        cols["fast_calls"].append(r.get("fast_calls", 0))
        cols["errors"].append(r.get("errors", 0))
        cols["max_tokens_stops"].append(r.get("max_tokens_stops", 0))
        cols["web_searches"].append(r.get("web_searches", 0))
        cols["peak_ctx"].append(r.get("peak_ctx", 0))
        cols["misses"].append(r.get("misses", 0))
        cols["cache_miss_cost"].append(r.get("cache_miss_cost", 0))
        cols["tool_attrib"].append([[sid(t[0]), t[1], t[2], t[3]]
                                    for t in r.get("tool_attrib", ())])
        cols["ts"].append(r["ts"])
        cols["project"].append(sid(r["project"]))
        cols["kind"].append(sid(r["kind"]))
        cols["text"].append(r["text"])
        cols["models"].append([[sid(m["model"]), m["in"], m["out"], m["cr"],
                                m["cw"], m["cost"], m["calls"]]
                               for m in r["models"]])
        cols["tools"].append([[sid(t), n] for t, n in r["tools"]])
        # [agent id, subagent_type, resolved model] - the id on its own is
        # opaque, and the detail view has no other way to reach the type.
        cols["agents"].append([[sid(a[0]), sid(a[1]), sid(a[2])]
                               for a in r.get("agent_info")
                               or [[a, None, None] for a in r["agents"]]])
        cols["cost"].append(r["cost"])
        cols["est"].append(1 if r["est"] else 0)
        cols["wall_s"].append(r["wall_s"])
        cols["agent_out"].append(r["agent_out"])
        cols["files"].append(r["files"])
        cols["ladd"].append(r["ladd"])
        cols["lrem"].append(r["lrem"])
        cols["chars"].append(r["chars"])
        cols["file_list"].append([[sid(f[0]), f[1], f[2]] for f in r["file_list"]])
        cols["comp"].append(r["comp"])
        cols["alt"].append(r["alt"])
        cols["title"].append(r.get("title"))
    if not any(cols["title"]):
        cols["title"] = 0          # nothing to carry; the reader treats it as absent
    if not any(cols["conv"]):
        cols["conv"] = 0
    return cols, sid.out


def compact_table(records, columns, strings, str_cols=(), list_str_cols=()):
    """A list of dicts as {n, cols} against the shared string table.

    Same trick the prompt rows use: the key names go out once instead of once
    per record, and anything repeated (a project, a model id) goes out as an
    index. `str_cols` are interned scalars; `list_str_cols` are lists of them.
    """
    cols = {k: [] for k in columns}
    for rec in records:
        for k in columns:
            v = rec.get(k)
            if k in str_cols:
                v = strings(v)
            elif k in list_str_cols:
                v = [strings(x) for x in (v or ())]
            elif isinstance(v, bool):
                v = 1 if v else 0
            cols[k].append(v)
    return {"n": len(records), "cols": cols}


def compact_ctx(points, strings):
    """The context series, encoded for size. See CTX_COLUMNS for what it means.

    One point per API request is by far the largest thing in the payload - at
    thirty times a heavy year's history it was two thirds of the file - and
    almost all of it was repetition. Three encodings, all undone by the
    template's rehydrateCtx() before anything else sees the series:

    - `t` is delta-encoded against `t0`. Consecutive requests are seconds
      apart, so a four-digit delta replaces a ten-digit epoch.
    - `session` and `model` are run-length encoded as [first index, string]
      pairs: the series is ordered by session and a session rarely changes
      model, so a few hundred pairs replace a value on every point.
    - `miss`, `cause` and `event` are sparse: a list of indices, and of
      [index, string] pairs. They are empty on well over 90% of points.

    The result is a fifth of the size and rehydrates to exactly the same
    array of objects it would have without any of this.
    """
    n = len(points)
    t0 = next((p[1] for p in points if p[1] is not None), 0)
    cols = {"t": [], "ctx": [], "cr": [], "cw": []}
    runs = {"session": [], "model": []}
    sparse = {"miss": [], "cause": [], "event": []}
    prev_t = t0
    last = object()
    last_session, last_model = last, last
    for i, (sid_, t, ctx, cr, cw, model, miss, cause, event) in enumerate(points):
        # A request whose timestamp would not parse keeps the previous point's
        # time rather than inventing one; the series is a curve, not a clock.
        t = prev_t if t is None else t
        cols["t"].append(t - prev_t)
        prev_t = t
        cols["ctx"].append(ctx)
        cols["cr"].append(cr)
        cols["cw"].append(cw)
        if sid_ != last_session:
            runs["session"].append([i, strings(sid_)])
            last_session = sid_
        if model != last_model:
            runs["model"].append([i, strings(model)])
            last_model = model
        if miss:
            sparse["miss"].append(i)
        if cause:
            sparse["cause"].append([i, strings(cause)])
        if event:
            sparse["event"].append([i, strings(event)])
    return {"n": n, "t0": t0, "cols": cols, "runs": runs, "sparse": sparse}


RECEIVER_ADDR = ("127.0.0.1", 4318)


def receiver_running(addr=RECEIVER_ADDR, timeout=0.2):
    """Is a live receiver holding the OTel port?

    Worth knowing at build time: a receiver started before this code was
    edited will not pick the change up, and says so in its own log where
    nobody looks. Mentioning it here puts the note in front of whoever just
    ran the build.
    """
    import socket
    try:
        with socket.create_connection(addr, timeout):
            return True
    except OSError:
        return False


def plan_usage(cfg=None):
    """Account-wide rate-limit gauges, bucketed by UTC day for the chart.

    Claude Desktop samples the plan's 5-hour and 7-day limits every five
    minutes. Per day we keep the peak of each - a limit you touched at noon
    still shaped your day even if you were idle by evening. Returns None when
    the desktop app is not installed.
    """
    cfg = cfg or sources.SourceConfig.load()
    samples = sources.plan_usage_samples(cfg.plan_usage_paths)
    if not samples:
        return None
    days = {}
    for epoch, fh, sd in samples:
        day = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
        cur = days.setdefault(day, [0, 0])
        cur[0] = max(cur[0], fh)
        cur[1] = max(cur[1], sd)
    last = samples[-1]
    return {
        "days": days,
        "latest": {
            "ts": datetime.fromtimestamp(last[0], timezone.utc)
                          .isoformat(timespec="seconds"),
            "fh": last[1], "sd": last[2],
        },
        "samples": len(samples),
    }


def unpriced_models():
    """Unpriced models that actually billed tokens (placeholders excluded)."""
    return {m: e for m, e in UNPRICED.items() if e["tokens"] > 0}


def insights_report():
    """Path to Claude Code's own /insights report, if the CLI wrote one.

    It answers a different question than this dashboard does - the CLI's view
    of the account - and it is easy to forget it exists, so the page links it.
    """
    path = os.path.join(
        os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR")
                           or os.path.join("~", ".claude")),
        "usage-data", "report.html")
    return path if os.path.exists(path) else None


def build(con=None, max_rows=DEFAULT_MAX_ROWS, redact=False, cfg=None,
          conversations_n=DEFAULT_CONVERSATIONS, check_receiver=True,
          db_path=None):
    """Render dashboard.html (and refresh index.html).

    max_rows caps how many prompts are embedded, newest first - the payload is
    re-parsed by the browser on every auto-refresh, so it cannot grow without
    limit. redact blanks prompt text, which makes the file safe to hand to
    someone who should see the numbers but not the conversations.

    conversations_n writes a per-prompt conversation page for that many of the
    newest prompts (0 disables; forced off by redact, since a page is nothing
    but prompt text). check_receiver=False skips the self-probe - the receiver
    passes it, because the receiver is the process holding the port.
    """
    own = con is None
    if own:
        resolved = db.resolve_path(db_path)
        con = db.connect() if resolved == db.DB_PATH else db.connect(resolved)
    out_rows, window = collect(con)
    total = len(out_rows)
    truncated = 0
    notices = []
    if max_rows and total > max_rows:
        out_rows = out_rows[:max_rows]      # already newest-first
        truncated = total - max_rows
    if redact:
        for r in out_rows:
            r["text"] = ""
    # A subscription concept (plan gauges, the 5h rate-limit block) only means
    # something if some traffic actually went through the Anthropic API. Rows
    # predating provider tracking carry no provider and are treated as
    # first-party, so nothing changes for an existing install.
    third_party = {p for p in PROVIDERS if p != pricing.ANTHROPIC}
    subscription = bool(PROVIDERS.get(pricing.ANTHROPIC)) or not third_party

    out_dir = os.path.dirname(os.path.abspath(OUTPUT))
    conv = conversations.write_pages(
        con, out_dir, out_rows,
        limit=0 if redact else (conversations_n or 0), redact=redact)
    if conv.get("skipped_missing"):
        notices.append(
            f"{conv['skipped_missing']} conversation page(s) were not written: "
            "their transcript is no longer on disk.")

    sessions = session_rows(out_rows, redact)
    points, ctx_dropped = ctx_points([s["id"] for s in sessions])
    if ctx_dropped:
        notices.append(
            f"The context series is capped at {CTX_CAP:,} points; the "
            f"{ctx_dropped} oldest session(s) were left out of it.")

    strings = Strings()
    cols, string_table = compact(out_rows, strings)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": window,
        "n_rows": len(out_rows),
        "total_rows": total,
        "truncated": truncated,
        "redacted": bool(redact),
        "plan": plan_usage(cfg) if subscription else None,
        "providers": dict(PROVIDERS),
        "subscription": subscription,
        "cols": cols,
        "sessions": compact_table(
            sessions, SESSION_COLUMNS, strings,
            str_cols=("id", "title", "project", "kind", "effort",
                      "subagent_ttl"),
            list_str_cols=("models",)),
        "ctx": compact_ctx(points, strings),
        "ctx_truncated": bool(ctx_dropped),
        "blocks": compact_table(
            [dict(b, start=b["start"].isoformat(timespec="seconds"),
                  end=b["end"].isoformat(timespec="seconds"),
                  models=sorted(b["models"]))
             for b in (EXTRAS.get("blocks") or [])],
            BLOCK_COLUMNS, strings, list_str_cols=("models",)),
        "burn": EXTRAS.get("burn"),
        "errors": EXTRAS.get("errors") or {"tools": [], "api": 0,
                                           "api_kinds": []},
        "baseline": dict(BASELINE),
        "overhead": EXTRAS.get("overhead") or {"by_model": [], "tokens": 0,
                                               "usd": 0.0},
        "insights_report": insights_report(),
        "cost_basis": EXTRAS.get("cost_basis"),
        "conversations": conv.get("count", 0),
        "notices": notices,
        "strings": string_table,
        "unpriced": [
            {"model": m, "rows": e["rows"], "tokens": e["tokens"],
             "provider": e.get("provider")}
            for m, e in sorted(unpriced_models().items(),
                               key=lambda kv: -kv[1]["tokens"])[:6]
        ],
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/null",
                        json.dumps(payload, separators=(",", ":")))
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUTPUT)
    if own:
        con.close()
    # The landing page is rebuilt alongside the dashboard so a single
    # bookmark always reaches every report, however many digests pile up.
    index = report_index.build()
    result = {"rows": len(out_rows), "output": OUTPUT, "index": index}
    if truncated:
        result["truncated"] = truncated
        print(f"NOTE: embedded the newest {len(out_rows):,} of {total:,} "
              f"prompts (--max-rows to change).", file=sys.stderr)
    if redact:
        result["redacted"] = True
    if REPRICED["rows"]:
        result["repriced_rows"] = REPRICED["rows"]
    if conv.get("count"):
        result["conversations"] = conv["count"]
        result["conversations_dir"] = conv["dir"]
    if check_receiver and receiver_running():
        result["receiver_running"] = True
        print("NOTE: a receiver is running on 127.0.0.1:4318. It rebuilds "
              "dashboard.html on its own; restart it to pick up code changes.",
              file=sys.stderr)
    unpriced = unpriced_models()
    if unpriced:
        result["unpriced_models"] = unpriced
    if PROVIDERS:
        result["providers"] = dict(PROVIDERS)
    return result


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Render metrics.db into a self-contained dashboard.html.")
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                    metavar="N",
                    help=f"most prompts to embed, newest first "
                         f"(default {DEFAULT_MAX_ROWS}; 0 = no limit)")
    ap.add_argument("--no-prompt-text", action="store_true",
                    help="blank prompt text, so the file can be shared "
                         "without disclosing what was typed; also turns off "
                         "conversation pages, which are prompt text")
    ap.add_argument("--conversations", type=int,
                    default=DEFAULT_CONVERSATIONS, metavar="N",
                    help=f"write a conversation page for the newest N prompts "
                         f"(default {DEFAULT_CONVERSATIONS}; 0 = none)")
    ap.add_argument("--db", metavar="PATH", default=None,
                    help="metrics database to read (default: $CLAUDE_LENS_DB, "
                         "the \"db\" key in sources.json, then metrics.db)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _args = parse_args()
    print(json.dumps(build(max_rows=_args.max_rows,
                           redact=_args.no_prompt_text,
                           conversations_n=_args.conversations,
                           db_path=_args.db), indent=2))
