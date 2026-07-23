"""Aggregate metrics.db into a self-contained static dashboard.html.

One row per user prompt (injected harness turns folded into their parent
prompt), with per-model token/cost breakdowns, tool-call counts, file-edit
stats, and subagent attribution. All filtering/summarizing happens client-side
in the template. `collect()` is shared with digest.py.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db
import pricing

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE, "template.html")
OUTPUT = os.path.join(BASE, "dashboard.html")

MAX_TEXT = 400
WINDOW_HOURS = 5          # Anthropic rate-limit window length
WINDOW_LOOKBACK_H = 36    # how far back to scan when locating the current window


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


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
    # Display name for a project: the basename of the session's working
    # directory (portable), falling back to the transcript-folder slug.
    session_project = {}
    slug_display = {}
    for sid, proj, cwd in con.execute(
            "SELECT session_id, project, cwd FROM sessions"):
        disp = os.path.basename((cwd or "").rstrip("\\/")) or proj
        session_project[sid] = disp
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

    for (pid, sid, ts, model, inp, out, cr, cw, c5, c1, cost, dur,
         qsrc, agent) in con.execute(
            """SELECT prompt_id, session_id, ts, model, input_tokens,
                      output_tokens, cache_read_tokens, cache_create_tokens,
                      cache_5m_tokens, cache_1h_tokens, cost_usd, duration_ms,
                      query_source, agent_name
               FROM api_requests WHERE prompt_id IS NOT NULL"""):
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
        if cost is None:
            cost = pricing.estimate_cost(model, inp or 0, out or 0, cr or 0,
                                         c5 or 0, c1 or 0, unsplit)
            if cost is None:
                cost = 0.0
            r["est"] = True
        m["cost"] += cost
        r["cost"] += cost
        # Cost components from the pricing table; when the CLI reported an
        # authoritative total, scale the split so components sum to it.
        p = pricing.lookup(model)
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

    for tuid, pid, name, agent in con.execute(
            "SELECT tool_use_id, prompt_id, tool_name, agent_name FROM tool_calls "
            "WHERE prompt_id IS NOT NULL"):
        r = bucket(pid)
        r["tools"][name or "?"] += 1
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
        out_rows.append({
            "id": r["id"],
            "ts": r["ts"],
            "project": slug_display.get(r["project"],
                                        session_project.get(r["session"],
                                                            r["project"] or "?")),
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
        })
    out_rows.sort(key=lambda r: r["ts"], reverse=True)
    return out_rows, compute_window(recent)


def build(con=None):
    own = con is None
    if own:
        con = db.connect()
    out_rows, window = collect(con)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": window,
        "rows": out_rows,
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
    return {"rows": len(out_rows), "output": OUTPUT}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
