"""Model pricing for cost estimation of backfilled (JSONL) rows.

OTel rows carry an authoritative cost_usd computed by the Claude Code CLI;
this table is only used for historical rows where no cost was recorded.

Prices are USD per million tokens: (input, output).
Cache read bills at 0.1x input; cache writes at 1.25x (5m TTL) / 2x (1h TTL).
"""

PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-3-5": (0.8, 4.0),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0


def lookup(model: str):
    """Resolve a model id (possibly date-suffixed or decorated like 'x[1m]') to prices."""
    if not model:
        return None
    m = model.split("[")[0].strip()
    if m in PRICES:
        return PRICES[m]
    # date-suffixed ids like claude-haiku-4-5-20251001
    for known, p in PRICES.items():
        if m.startswith(known):
            return p
    return None


def estimate_cost(model, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_5m=0, cache_1h=0, cache_unsplit=0):
    """Estimated USD cost. cache_unsplit is cache-creation tokens with unknown TTL
    (billed here at the 1h rate, since Claude Code uses 1h-TTL caching)."""
    p = lookup(model)
    if p is None:
        return None
    inp, out = p
    return (
        input_tokens * inp
        + output_tokens * out
        + cache_read * inp * CACHE_READ_MULT
        + cache_5m * inp * CACHE_WRITE_5M_MULT
        + (cache_1h + cache_unsplit) * inp * CACHE_WRITE_1H_MULT
    ) / 1_000_000
