"""Model pricing for cost estimation of backfilled (JSONL) rows.

OTel rows carry an authoritative cost_usd computed by the Claude Code CLI;
this table is only used for historical rows where no cost was recorded.

Prices are USD per million tokens: (input, output).
Cache read bills at 0.1x input; cache writes at 1.25x (5m TTL) / 2x (1h TTL).

Model ids are matched by longest prefix, so date-suffixed ids
(claude-haiku-4-5-20251001) and decorated ids (claude-opus-5[1m]) resolve to
their base entry. The 1M-context models carry no long-context premium, so the
"[1m]" decoration does not change the rate.
"""

PRICES = {
    # Current models
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Legacy / deprecated, still resolvable for historical transcripts
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-opus-4-2025": (15.0, 75.0),      # claude-opus-4-20250514
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-sonnet-4-2025": (3.0, 15.0),     # claude-sonnet-4-20250514
    # Retired, but old transcripts on disk may still reference them
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-haiku-3-5": (0.8, 4.0),
    "claude-3-haiku": (0.25, 1.25),
}

# Time-limited promotional rates: model -> (end_date_exclusive, (input, output)).
# Applied only when a request timestamp is supplied; requests at or after the
# end date fall back to the standard PRICES entry.
INTRO_PRICES = {
    "claude-sonnet-5": ("2026-09-01", (2.0, 10.0)),
}

# Fast mode (Claude Opus 5 / Opus 4.8) bills at 10.0/50.0, but the transcript
# records only the model id — there is no speed field to key off — so fast-mode
# requests are estimated here at the standard rate.

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# Longest first, so claude-opus-4-5 wins over any shorter overlapping prefix.
_BY_PREFIX = sorted(PRICES.items(), key=lambda kv: -len(kv[0]))


def lookup(model, ts=None):
    """Resolve a model id to (input, output) prices per million tokens.

    ts is an optional ISO-8601 request timestamp; when given, promotional
    pricing in effect at that time is applied. Returns None if unknown.
    """
    if not model:
        return None
    m = model.split("[")[0].strip()
    base = None
    for known, p in _BY_PREFIX:
        if m == known or m.startswith(known):
            base = known
            break
    if base is None:
        return None
    intro = INTRO_PRICES.get(base)
    if intro and ts and str(ts) < intro[0]:
        return intro[1]
    return PRICES[base]


def estimate_cost(model, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_5m=0, cache_1h=0, cache_unsplit=0, ts=None):
    """Estimated USD cost. cache_unsplit is cache-creation tokens with unknown TTL
    (billed here at the 1h rate, since Claude Code uses 1h-TTL caching)."""
    p = lookup(model, ts)
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
