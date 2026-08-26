"""Aggregate metrics.db into a self-contained static dashboard.html.

One row per user prompt (injected harness turns folded into their parent
prompt), with per-model token/cost breakdowns, tool-call counts, file-edit
stats, and subagent attribution. All filtering/summarizing happens client-side
in the template. `collect()` is shared with digest.py.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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


def collect(con):
    """Aggregate the DB into per-prompt rows + current-window stats."""
    UNPRICED.clear()
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
    slug_display = {}
    for sid, proj, cwd, label, title in con.execute(
            "SELECT session_id, project, cwd, source_label, title FROM sessions"):
        if title:
            session_title[sid] = title
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
        }

    for pid, sid, project, ts, text, injected in con.execute(
            "SELECT prompt_id, session_id, project, ts, text, injected FROM prompts"):
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
    recent = []

    providers = defaultdict(int)
    for (pid, sid, ts, model, inp, out, cr, cw, c5, c1, cost, dur,
         qsrc, agent, provider) in con.execute(
            """SELECT prompt_id, session_id, ts, model, input_tokens,
                      output_tokens, cache_read_tokens, cache_create_tokens,
                      cache_5m_tokens, cache_1h_tokens, cost_usd, duration_ms,
                      query_source, agent_name, provider
               FROM api_requests WHERE prompt_id IS NOT NULL"""):
        if provider:
            providers[provider] += 1
        r = bucket(pid)
        if r["project"] == "?" and sid in session_project:
            r["project"] = session_project[sid] or "?"
        m = r["models"][model]
        m["in"] += inp or 0
        m["out"] += out or 0
        m["cr"] += cr or 0
        m["cw"] += cw or 0
        m["calls"] += 1
        r["api_calls"] += 1
        unsplit = max((cw or 0) - (c5 or 0) - (c1 or 0), 0)
        p = pricing.lookup(model, ts, provider)
        if p is None:
            note_unpriced(model, (inp or 0) + (out or 0) + (cr or 0) + (cw or 0),
                          uncosted=cost is None, provider=provider)
        if cost is None:
            cost = pricing.estimate_cost(model, inp or 0, out or 0, cr or 0,
                                         c5 or 0, c1 or 0, unsplit, ts,
                                         provider=provider)
            if cost is None:
                cost = 0.0
            r["est"] = True
        m["cost"] += cost
        r["cost"] += cost
        # Cost components from the pricing table; when the CLI reported an
        # authoritative total, scale the split so components sum to it.
        if p:
            pi, po = p
            comp = [
                (cr or 0) * pi * pricing.CACHE_READ_MULT / 1e6,
                ((c5 or 0) * pricing.CACHE_WRITE_5M_MULT
                 + ((c1 or 0) + unsplit) * pricing.CACHE_WRITE_1H_MULT) * pi / 1e6,
                (out or 0) * po / 1e6,
                (inp or 0) * pi / 1e6,
            ]
            est_total = sum(comp)
            if est_total > 0 and cost > 0:
                f = cost / est_total
                comp = [c * f for c in comp]
            for i2 in range(4):
                r["comp"][i2] += comp[i2]
            r["alt"] += (((cr or 0) + (cw or 0) + (inp or 0)) * pi
                         + (out or 0) * po) / 1e6
        if agent:
            r["agents"].add(agent)
            r["agent_out"] += out or 0
        if ts and ts > r["last_ts"]:
            r["last_ts"] = ts
        if ts and not r["ts"]:
            r["ts"] = ts
        dt = parse_ts(ts)
        if dt and dt >= window_cutoff:
            recent.append((dt, out or 0, cost))

    for tuid, pid, name, agent, detail in con.execute(
            "SELECT tool_use_id, prompt_id, tool_name, agent_name, detail "
            "FROM tool_calls WHERE prompt_id IS NOT NULL"):
        r = bucket(pid)
        display = f"Skill:{detail}" if (name == "Skill" and detail) else (name or "?")
        r["tools"][display] += 1
        if agent:
            r["agents"].add(agent)

    for pid, path, add, rem, chars, agent in con.execute(
            """SELECT prompt_id, file_path, lines_added, lines_removed,
                      chars_added, agent_name
               FROM edits WHERE prompt_id IS NOT NULL"""):
        r = bucket(pid)
        f = r["files"][path or "?"]
        f[0] += add or 0
        f[1] += rem or 0
        r["chars"] += chars or 0
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

    out_rows = []
    for r in rows.values():
        if not r["api_calls"] and not r["tools"]:
            continue
        wall = None
        t0, t1 = parse_ts(r["ts"]), parse_ts(r["last_ts"])
        if t0 and t1 and t1 >= t0:
            wall = round((t1 - t0).total_seconds())
        models = [
            {"model": k, **v} for k, v in
            sorted(r["models"].items(), key=lambda kv: -kv[1]["out"])
        ]
        file_list = sorted(
            ([p, a, d] for p, (a, d) in r["files"].items()),
            key=lambda x: -(x[1] + x[2]))[:40]
        project = slug_display.get(r["project"],
                                   session_project.get(r["session"],
                                                       r["project"] or "?"))
        kind = session_kind.get(r["session"])
        if kind is None:      # prompt whose session never made it into the DB
            kind = (COWORK_KIND
                    if project.startswith(sources.COWORK_LABEL + "/")
                    else CODE_KIND)
        row = {
            "id": r["id"],
            "ts": r["ts"],
            "project": project,
            "kind": kind,
            "text": r["text"],
            "models": models,
            "tools": sorted(r["tools"].items(), key=lambda kv: -kv[1]),
            "agents": sorted(r["agents"]),
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
    return out_rows, compute_window(recent)


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


def build(con=None, max_rows=DEFAULT_MAX_ROWS, redact=False, cfg=None):
    """Render dashboard.html (and refresh index.html).

    max_rows caps how many prompts are embedded, newest first - the payload is
    re-parsed by the browser on every auto-refresh, so it cannot grow without
    limit. redact blanks prompt text, which makes the file safe to hand to
    someone who should see the numbers but not the conversations.
    """
    own = con is None
    if own:
        con = db.connect()
    out_rows, window = collect(con)
    total = len(out_rows)
    truncated = 0
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
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": window,
        "rows": out_rows,
        "total_rows": total,
        "truncated": truncated,
        "redacted": bool(redact),
        "plan": plan_usage(cfg) if subscription else None,
        "providers": dict(PROVIDERS),
        "subscription": subscription,
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
    if receiver_running():
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
                         "without disclosing what was typed")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _args = parse_args()
    print(json.dumps(build(max_rows=_args.max_rows,
                           redact=_args.no_prompt_text), indent=2))
