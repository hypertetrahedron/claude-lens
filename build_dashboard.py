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
from urllib.request import pathname2url

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

# What the main conversation and a subagent call themselves - db.py owns this
# vocabulary (jsonl_ingest.py needs it too, to seed a session's model/effort/
# speed from its own past rows) and this module just uses it. A transcript
# writes "main"/"subagent"; the CLI's own telemetry writes "repl_main_thread"
# for the same requests and "agent:builtin:<type>" for the same subagents, and
# an OTel row wins every conflict, so a request both sources saw ends up
# labelled the telemetry way. Filtering on "main" alone therefore kept only
# the requests OTel never saw - on a live-monitored machine under a tenth of
# the conversation - and the context curve drawn from what was left jumped
# days at a time and invented idle-gap cache misses across the holes.
# Anything not named here (sdk, away_summary, prompt_suggestion, the search
# and title-generation helpers) is a genuine side thread against its own
# context and stays out.
MAIN_QUERY_SOURCES = db.MAIN_QUERY_SOURCES
MAIN_QS_SQL = db.MAIN_QS_SQL
SUBAGENT_QS_SQL = db.SUBAGENT_QS_SQL
is_subagent_qs = db.is_subagent_qs
# A sentinel for "no prompt seen yet in this session" that no prompt id can
# equal - None cannot be used, because a request with a NULL prompt_id is a
# real case and must not read as the start of a turn.
_NO_PROMPT = object()

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

# Rows whose cost was re-derived from a CLI-reported session total during the
# last collect(). The total is exact; each row holds its share of it, scaled
# by that row's own estimate, so these rows stay flagged estimated. Surfaced
# in the payload's notices and in build()'s result so a run says how much of
# it came from the CLI's figure rather than from the rate table.
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


def note_unpriced(model, tokens, uncosted, provider=None, calls=1):
    """Record `calls` unpriced requests for `model` (one GROUP BY group).

    `calls` is the request count of the group being folded in, not the
    number of times this function was called - collect() calls it once per
    (model, provider, ...) group, so without this the stderr warning and the
    payload's unpriced[].rows undercount every model with more than one
    request.
    """
    e = UNPRICED.setdefault(model or "?",
                            {"rows": 0, "uncosted_rows": 0, "tokens": 0,
                             "provider": provider})
    e["rows"] += calls
    e["uncosted_rows"] += calls if uncosted else 0
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


def cache_scan(con, since=None, canon=None):
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

    Returns (per_prompt, per_session, ctx) where per_prompt maps a *canonical*
    prompt id (per `canon`, folding injected turns onto their parent - see
    resolve_map()) to [misses, miss_cost] and ctx maps a session id to a list
    of [t, ctx, cr, cw, model, miss, cause, event, turn, tools, terr] points.

    `turn` is 1 on the first request of each prompt, which is what lets the
    session timeline show where one turn ends and the next begins; it is the
    *canonical* prompt that counts, so an injected turn does not read as a
    new one. `tools` and `terr` are the tool calls, and the failed ones, that
    landed between the previous request and this one - which is exactly the
    set whose results this request is carrying, so the density lane and the
    context curve are measuring the same thing at the same x.

    `canon` defaults to resolve_map(con) when not passed by a caller that
    already has it.

    Subagent requests are excluded: they run against their own context, so
    mixing them into the session's series would draw a sawtooth that never
    happened.
    """
    canon = resolve_map(con) if canon is None else canon
    events = defaultdict(list)
    for sid, ts, kind in con.execute(
            "SELECT session_id, ts, kind FROM session_events "
            "WHERE kind IN (?,?,?,?) ORDER BY session_id, ts",
            MISS_EVENT_KINDS):
        if sid:
            events[sid].append((ts or "", kind))

    # The main conversation's own tool calls. A subagent's are its own work
    # and belong to its bar on the timeline, not to the turn that launched it,
    # so agent_name has to be NULL here for the same reason subagent requests
    # are left out of the series.
    tools = defaultdict(list)
    tsql, targs = _since_clause(
        "SELECT session_id, ts, COALESCE(is_error, 0) FROM tool_calls "
        "WHERE session_id IS NOT NULL AND agent_name IS NULL", since)
    for sid, ts, bad in con.execute(tsql + " ORDER BY session_id, ts", targs):
        tools[sid].append((ts or "", bad))

    sql, args = _since_clause(
        """SELECT session_id, prompt_id, ts, model, provider, effort, speed,
                  inference_geo, COALESCE(cache_read_tokens, 0),
                  COALESCE(cache_create_tokens, 0),
                  COALESCE(cache_5m_tokens, 0), COALESCE(cache_1h_tokens, 0),
                  COALESCE(context_tokens, 0)
             FROM api_requests
            WHERE session_id IS NOT NULL
              AND """ + MAIN_QS_SQL, since)
    sql += " ORDER BY session_id, ts"

    resolve = pricing.resolve
    per_prompt = defaultdict(lambda: [0, 0.0])
    per_session = {}
    ctx = {}
    cur = prev = None
    last_pid = _NO_PROMPT
    evs, ei, points, stats = [], 0, [], None
    tls, ti = [], 0

    for (sid, pid, ts, model, provider, effort, speed, geo, cr, cw, c5, c1,
         ctx_tokens) in con.execute(sql, args):
        if sid != cur:
            cur, prev, last_pid = sid, None, _NO_PROMPT
            evs, ei = events.get(sid) or [], 0
            tls, ti = tools.get(sid) or [], 0
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
        n_tools = n_terr = 0
        while ti < len(tls) and tls[ti][0] <= (ts or ""):
            n_tools += 1
            n_terr += 1 if tls[ti][1] else 0
            ti += 1
        dt = parse_ts(ts)
        # "normal" and "standard" both mean the request was not fast mode;
        # without this a standard->NULL pair (or vice versa) reads as a
        # switch and pre-empts the real cause.
        speed_n = "standard" if speed == "normal" else speed
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
            elif effort is not None and prev[2] is not None and effort != prev[2]:
                cause = "effort_switch"
            elif speed_n is not None and prev[3] is not None and speed_n != prev[3]:
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
                    per_prompt[canon.get(pid, pid)][1] += cost
            stats["misses"] += 1
            if pid:
                per_prompt[canon.get(pid, pid)][0] += 1
        # A request with no prompt id at all cannot start a turn - saying it
        # did would put a boundary on every such request in a row.
        cpid = canon.get(pid, pid) if pid else None
        turn = 1 if (cpid is not None and cpid != last_pid) else 0
        if cpid is not None:
            last_pid = cpid
        points.append([int(dt.timestamp()) if dt else None, ctx_tokens, cr, cw,
                       model, miss, cause, seen[0] if seen else None, turn,
                       n_tools, n_terr])
        prev = (cr, model, effort, speed_n, dt)
    return per_prompt, per_session, ctx


def agent_spans(con, since=None):
    """When each subagent ran, per session: {session_id: [span, ...]}.

    A subagent's requests carry the CLI's agentId in `api_requests.agent_name`,
    so the launch-to-last-request span is a group-by on that column - the
    `agents` table is joined only for the things a request does not know (what
    type of agent it was, what the user asked it for). The span *starts* at
    the agents row's own timestamp where there is one, because that is the
    moment the Agent tool was called; the first request is a second or two
    later and, for an agent whose requests were never ingested, is not there
    at all.

    There is no end timestamp anywhere - a subagent stops when it stops - so
    the last request is the end, and a one-request agent gets a zero-length
    span the timeline widens to a visible minimum rather than a bar of the
    wrong length.

    Cost is priced here rather than read off the rows: transcript-sourced
    subagent requests carry no cost_usd at all, so summing that column
    reported every jsonl-only subagent as free.
    """
    # `since` bounds this half too. It is not only the fallback below that
    # would otherwise leak: an agent launched months before the window would
    # come back through it as a request-less span, and one whose requests are
    # in the window would have its span dragged back to a launch outside it.
    # collect(con, since=...) is a real call shape - digest.py builds the
    # weekly report with it - so the two queries have to agree on the cutoff.
    meta = {}
    meta_sql, meta_args = _since_clause(
        "SELECT agent_id, session_id, ts, subagent_type, "
        "COALESCE(resolved_model, requested_model), description "
        "FROM agents WHERE 1=1", since)
    for aid, sid, ts, typ, model, desc in con.execute(meta_sql, meta_args):
        meta[aid] = (sid, ts, typ, model, desc)

    sql, args = _since_clause(
        """SELECT session_id, agent_name, model, provider, speed,
                  inference_geo, cost_usd IS NULL, COUNT(*),
                  SUM(COALESCE(input_tokens, 0)),
                  SUM(COALESCE(output_tokens, 0)),
                  SUM(COALESCE(cache_read_tokens, 0)),
                  SUM(COALESCE(cache_create_tokens, 0)),
                  SUM(COALESCE(cache_5m_tokens, 0)),
                  SUM(COALESCE(cache_1h_tokens, 0)),
                  SUM(MAX(COALESCE(cache_create_tokens, 0)
                          - COALESCE(cache_5m_tokens, 0)
                          - COALESCE(cache_1h_tokens, 0), 0)),
                  SUM(cost_usd), MIN(ts), MAX(ts)
             FROM api_requests
            WHERE agent_name IS NOT NULL AND session_id IS NOT NULL""", since)
    acc = {}
    for (sid, aid, model, provider, speed, geo, nocost, calls, inp, out, cr,
         cw, c5, c1, unsplit, cost, ts_min, ts_max) in con.execute(
            sql + """ GROUP BY session_id, agent_name, model, provider, speed,
                               inference_geo, cost_usd IS NULL,
                               substr(ts, 1, 10)""", args):
        rate = pricing.resolve(model, ts_min, provider, speed, geo)
        if nocost:
            cost = 0.0 if rate is None else pricing.cost_at(
                rate, inp, out, cr, c5, c1, unsplit, provider)
        a = acc.get((sid, aid))
        if a is None:
            a = acc[(sid, aid)] = {"agent": aid, "t0": ts_min, "t1": ts_max,
                                   "calls": 0, "out": 0, "cost": 0.0,
                                   "model": model, "est": bool(nocost)}
        a["calls"] += calls
        a["out"] += out or 0
        a["cost"] += cost or 0.0
        a["est"] = a["est"] or bool(nocost)
        if ts_min and ts_min < a["t0"]:
            a["t0"] = ts_min
        if ts_max and ts_max > a["t1"]:
            a["t1"] = ts_max

    # Agents the requests never mentioned: launched, but their transcript was
    # not ingested (or never written). They still belong on the timeline -
    # leaving them off would say the turn ran nothing.
    for aid, (sid, ts, typ, model, desc) in meta.items():
        if sid and ts and (sid, aid) not in acc:
            acc[(sid, aid)] = {"agent": aid, "t0": ts, "t1": ts, "calls": 0,
                               "out": 0, "cost": 0.0, "model": model,
                               "est": False}

    out_by_session = defaultdict(list)
    for (sid, aid), a in acc.items():
        sid_m, ts_m, typ, model_m, desc = meta.get(aid, (None,) * 5)
        if ts_m and (a["t0"] is None or ts_m < a["t0"]):
            a["t0"] = ts_m
        t0, t1 = parse_ts(a["t0"]), parse_ts(a["t1"])
        if not t0:
            continue
        out_by_session[sid].append({
            "session": sid,
            "t0": int(t0.timestamp()),
            "t1": int((t1 or t0).timestamp()),
            "type": typ,
            "desc": desc,
            "model": model_m or a["model"],
            "calls": a["calls"],
            "out": a["out"],
            "cost": round(a["cost"], 4),
            "est": 1 if a["est"] else 0,
        })
    for spans in out_by_session.values():
        spans.sort(key=lambda s: (s["t0"], s["t1"]))
    return dict(out_by_session)


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
            # Burn only ever describes the block currently open, so a new
            # block starting resets it - otherwise a block only a few
            # minutes old would still carry requests from the one before it.
            burn_tokens = burn_usd = 0.0
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
        # A block only a few minutes old has not had BURN_WINDOW_MIN of
        # history yet - dividing by the full 30 minutes would under-report
        # its rate, so the divisor is however long the block has actually
        # been open, capped at the window and floored at one minute.
        since_start = (now - b["start"]).total_seconds() / 60.0
        divisor = max(1.0, min(BURN_WINDOW_MIN, since_start))
        burn = {
            "window_min": BURN_WINDOW_MIN,
            "minutes_left": round(left, 1),
            "tokens_per_min": round(burn_tokens / divisor, 1),
            "usd_per_min": round(burn_usd / divisor, 6),
            "projected_cost": round(
                b["cost"] + burn_usd / divisor * left, 4),
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


def session_lookups(con):
    """Per-session display facts as five dicts keyed by session_id:
    (project, kind, title, meta, slug_display).

    Split out of collect() because it is a self-contained read that
    depends on nothing collect() has worked out yet.
    """
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
    return (session_project, session_kind, session_title, session_meta,
            slug_display)


def reprice_from_run_cost(con, emitted):
    """Spend the CLI's own per-session total where it provably covers
    every run, and return how many rows were repriced.

    `emitted` maps session_id to the rows that actually ran something.
    Those rows are mutated in place: cost and composition scale by the
    same factor, and `est` stays set, because what the CLI measured is
    the session and not the prompt.
    """
    repriced = 0
    for sid, auth_cost, runs in con.execute(
            "SELECT session_id, cost_usd, runs FROM run_cost"):
        group = emitted.get(sid) or []
        est_total = sum(g["cost"] for g in group)
        if not group or not auth_cost or runs != len(group) or est_total <= 0:
            continue
        # An unpriced model costs $0.00, so a prompt that ran on one carries no
        # weight in `est_total` and would take none of the session total away
        # from its neighbours: the whole session's spend would be shared out
        # over the priced prompts while that one stayed at a confident $0.00 -
        # a model we know we cannot price, displayed as free. The estimate is
        # incomplete for such a session, so the authoritative total is left
        # unspent and the notice bar names the model instead.
        if any(g["unpriced"] for g in group):
            continue
        factor = auth_cost / est_total
        for g in group:
            g["cost"] *= factor
            g["comp"] = [c * factor for c in g["comp"]]
            # The session total is authoritative; this row's share of it is
            # not. Each prompt gets the total scaled by its own estimate, so
            # the figure on the row is still an estimate - a better one - and
            # clearing `est` here would claim a per-prompt measurement the CLI
            # never made. Only the session sum is exact.
            g["est"] = True
        repriced += len(group)
    return repriced


def agent_identity(con):
    """agent_id -> (subagent type, the model it actually ran on).

    `agents.agent_id` is what api_requests.agent_name and
    tool_calls.agent_name carry, so the opaque id a row collected can be
    traded for the type that was launched and the model it resolved to.
    """
    agent_meta = {}
    for aid, kind, resolved, requested in con.execute(
            "SELECT agent_id, subagent_type, resolved_model, requested_model "
            "FROM agents"):
        agent_meta[aid] = (kind, resolved or requested)
    return agent_meta


def subagent_cache_ttl(con):
    """session_id -> "5m"/"1h", whichever tier this session's subagents
    wrote more cache into. Both query_source spellings count - see
    db.SUBAGENT_QS_SQL, which is why this is not a bare literal.
    """
    subagent_ttl = {}
    for sid, c5, c1 in con.execute(
            "SELECT session_id, SUM(COALESCE(cache_5m_tokens, 0)), "
            "SUM(COALESCE(cache_1h_tokens, 0)) FROM api_requests "
            "WHERE " + SUBAGENT_QS_SQL + " GROUP BY session_id"):
        if (c5 or 0) or (c1 or 0):
            subagent_ttl[sid] = "5m" if (c5 or 0) >= (c1 or 0) else "1h"
    return subagent_ttl


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
    (session_project, session_kind, session_title, session_meta,
     slug_display) = session_lookups(con)
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
            # Internal to collect(): true once any of this prompt's requests
            # ran on a model the rate table does not know, so its $0.00 is an
            # absence rather than a measurement. Never reaches the payload -
            # the notice bar names the models from UNPRICED instead - but the
            # run_cost repricing below has to see it.
            "unpriced": False,
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
                  MAX(CASE WHEN """ + MAIN_QS_SQL + """
                           THEN COALESCE(context_tokens, 0) ELSE 0 END)
           FROM api_requests WHERE prompt_id IS NOT NULL""", since)
    # Peak context is the main conversation's window, not a subagent's: a
    # subagent runs against its own, so its context_tokens is not a reading of
    # the window this prompt was filling. cache_scan() filters the session's
    # figure the same way and the session row takes the max of the two, so
    # without this a session's "Peak context" could report a subagent's window
    # as the main thread's. It is a CASE rather than a WHERE because every
    # other column here does want the subagent's requests - they are this
    # prompt's tokens and this prompt's cost.
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
                          uncosted=bool(nocost), provider=provider,
                          calls=calls)
            r["unpriced"] = True
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
    repriced = reprice_from_run_cost(con, emitted)
    REPRICED["rows"] = repriced

    agent_meta = agent_identity(con)

    per_prompt_cache, per_session_cache, ctx_series = cache_scan(con, since,
                                                                 canon)

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
    subagent_ttl = subagent_cache_ttl(con)
    EXTRAS.update({
        "ctx": ctx_series,
        "agent_spans": agent_spans(con, since),
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
               "event", "turn", "tools", "terr")

# One subagent run: when it started, when its last request landed, and what it
# was. See agent_spans().
AGENT_SPAN_COLUMNS = ("session", "t0", "t1", "type", "desc", "model", "calls",
                      "out", "cost", "est")

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
    - `miss`, `cause`, `event`, `turn` and `terr` are sparse: a list of
      indices, and of [index, string] pairs. They are empty on well over 90%
      of points - `turn` marks one request per prompt out of the dozens a
      prompt makes, and a failed tool call is rarer still. `terr` carries its
      count, so it goes out as [index, n] pairs rather than bare indices.

    `tools` is the one new dense column: most requests carry one or two tool
    results and the run lengths are short, so a plain array of small integers
    beats every encoding worth the code.

    The result is a fifth of the size and rehydrates to exactly the same
    array of objects it would have without any of this.
    """
    n = len(points)
    t0 = next((p[1] for p in points if p[1] is not None), 0)
    cols = {"t": [], "ctx": [], "cr": [], "cw": [], "tools": []}
    runs = {"session": [], "model": []}
    sparse = {"miss": [], "cause": [], "event": [], "turn": [], "terr": []}
    prev_t = t0
    last = object()
    last_session, last_model = last, last
    for i, (sid_, t, ctx, cr, cw, model, miss, cause, event, turn, n_tools,
            n_terr) in enumerate(points):
        # A request whose timestamp would not parse keeps the previous point's
        # time rather than inventing one; the series is a curve, not a clock.
        t = prev_t if t is None else t
        cols["t"].append(t - prev_t)
        prev_t = t
        cols["ctx"].append(ctx)
        cols["cr"].append(cr)
        cols["cw"].append(cw)
        cols["tools"].append(n_tools)
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
        if turn:
            sparse["turn"].append(i)
        if n_terr:
            sparse["terr"].append([i, n_terr])
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
    """file: URL for Claude Code's own /insights report, if the CLI wrote one.

    It answers a different question than this dashboard does - the CLI's view
    of the account - and it is easy to forget it exists, so the page links it.
    A raw filesystem path is not a usable href (and breaks outright on
    Windows' drive-letter form), so this is encoded with pathname2url the
    same way report_index.py's _file_url does it.
    """
    path = os.path.join(sources.primary_dir(), "usage-data", "report.html")
    if not os.path.exists(path):
        return None
    return "file:" + pathname2url(os.path.abspath(path))


def embed_json(payload):
    """Serialise `payload` for pasting inside an HTML <script> element.

    The HTML tokenizer looks for the literal `</script` before any JavaScript
    parsing happens, so a prompt that merely mentions one closes the real
    script element early: the rest of the payload lands on the page as text,
    `DATA` is never assigned, the table renders nothing, and anything after
    the sequence is parsed as markup and runs. Escaping `<` is enough to stop
    it; `>` and `&` go too so no other tokenizer state (`<!--`, an entity)
    can be entered either.

    A bare `<`, `>` or `&` can only ever occur inside a JSON string literal -
    JSON's own grammar has no use for them - so replacing every one with its
    \\uXXXX form is safe wholesale and parses back to exactly the same value.
    U+2028 and U+2029 are already handled by ensure_ascii.
    """
    text = json.dumps(payload, separators=(",", ":"))
    return (text.replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026"))


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
    # Only a connection opened here is ours to close, and only in `finally` -
    # collect() and everything after it can raise (a malformed row, a full
    # disk while writing dashboard.html), and leaking `own`'s connection past
    # that holds a WAL read mark that blocks checkpointing indefinitely. A
    # connection the caller handed us is theirs for the whole of its life;
    # closing it out from under them here would be a different bug.
    try:
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
        # A subscription concept (plan gauges, the 5h rate-limit block) only
        # means something if some traffic actually went through the Anthropic
        # API. Rows predating provider tracking carry no provider and are
        # treated as first-party, so nothing changes for an existing install.
        third_party = {p for p in PROVIDERS if p != pricing.ANTHROPIC}
        subscription = (bool(PROVIDERS.get(pricing.ANTHROPIC))
                        or not third_party)

        out_dir = os.path.dirname(os.path.abspath(OUTPUT))
        conv = conversations.write_pages(
            con, out_dir, out_rows,
            limit=0 if redact else (conversations_n or 0), redact=redact)
        if conv.get("skipped_missing"):
            notices.append(
                f"{conv['skipped_missing']} conversation page(s) were not "
                "written: their transcript is no longer on disk.")
        if conv.get("capped"):
            notices.append(
                f"Conversation pages cover the newest {conversations_n:,} "
                f"prompt(s); the other {conv['capped']:,} have no page. "
                "Rebuild with --conversations to change that.")

        sessions = session_rows(out_rows, redact)
        points, ctx_dropped = ctx_points([s["id"] for s in sessions])
        # Subagent spans ride along with the sessions the page actually
        # lists; spans for a session that is not in the payload have nothing
        # to draw on. Descriptions are prompt text the user typed, so
        # --no-prompt-text drops them for the same reason it drops
        # everything else a prompt said.
        by_session = EXTRAS.get("agent_spans") or {}
        spans = [dict(sp, desc=None) if redact else sp
                 for s in sessions for sp in by_session.get(s["id"], ())]
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
            "agent_spans": compact_table(
                spans, AGENT_SPAN_COLUMNS, strings,
                str_cols=("session", "type", "desc", "model")),
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
            # A file: URL under the home directory, so it carries the account
            # name. --no-prompt-text exists to make the page safe to hand to
            # someone else, and a username is exactly the kind of thing that
            # build is supposed to withhold. The template hides the link when
            # this is null.
            "insights_report": None if redact else insights_report(),
            "cost_basis": EXTRAS.get("cost_basis"),
            "conversations": conv.get("count", 0),
            # How many prompts hold a share of a CLI-reported session total
            # instead of a figure worked out from the rate table. It used to
            # live only in the CLI result, which the receiver - running with
            # no console - throws away, so under normal operation nobody ever
            # learned which half of the page's money came from where. The
            # notice bar says it for the same reason it names unpriced models.
            "repriced": REPRICED["rows"],
            "notices": notices,
            "strings": string_table,
            # Every unpriced model, not a slice of them: the notice bar
            # derives both its count and its list from this array, so
            # dropping the tail made the page understate how many models it
            # was costing at $0.00 and leave the rest unnamed anywhere.
            # Dearest first.
            "unpriced": [
                {"model": m, "rows": e["rows"], "tokens": e["tokens"],
                 "provider": e.get("provider")}
                for m, e in sorted(unpriced_models().items(),
                                   key=lambda kv: -kv[1]["tokens"])
            ],
        }
        with open(TEMPLATE, encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__DATA__*/null", embed_json(payload))
        tmp = OUTPUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, OUTPUT)
    finally:
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
