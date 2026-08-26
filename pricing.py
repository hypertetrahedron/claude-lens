"""Model pricing for cost estimation of backfilled (JSONL) rows.

OTel rows carry an authoritative cost_usd computed by the Claude Code CLI;
this table is only used for historical rows where no cost was recorded.

Prices are USD per million tokens: (input, output).
Cache read bills at 0.1x input; cache writes at 1.25x (5m TTL) / 2x (1h TTL).

Model ids are matched by longest prefix, so date-suffixed ids
(claude-haiku-4-5-20251001) and decorated ids (claude-opus-5[1m]) resolve to
their base entry. The 1M-context models carry no long-context premium, so the
"[1m]" decoration does not change the rate.

Providers
---------
Claude Code can reach the same models through the Anthropic API, through
Amazon Bedrock (CLAUDE_CODE_USE_BEDROCK) or through Vertex AI
(CLAUDE_CODE_USE_VERTEX), and each decorates the id differently:

    anthropic  claude-opus-4-5-20251101
    bedrock    us.anthropic.claude-opus-4-5-20251101-v1:0
               arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic...
    vertex     claude-opus-4-5@20251101

canonical_model() strips the decoration back to the Anthropic-form id and
reports which provider it came from, so one model is one entry in the table
whichever route it took. Without it every Bedrock id missed this table
entirely and its requests were costed at $0.00 - silently, in live mode.

Rates are held per provider. The Bedrock and Vertex tables start as copies of
the Anthropic list price, which is where they have historically sat. That is
an assumption, not a measurement: Bedrock rates also vary by region, batch
inference is discounted, and Provisioned Throughput bills per model-unit-hour
rather than per token, where a per-token estimate means nothing at all.
Override the rates - and map any id this file cannot recognise - in
pricing.local.json; pricing.example.json documents the shape.
"""
import json as _json
import os as _os
import re as _re

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
# end date fall back to the standard PRICES entry. Anthropic-only: a promotion
# on the first-party API says nothing about a marketplace's rate card.
INTRO_PRICES = {
    "claude-sonnet-5": ("2026-09-01", (2.0, 10.0)),
}

# Fast mode (Claude Opus 5 / Opus 4.8) bills at 10.0/50.0, but the transcript
# records only the model id — there is no speed field to key off — so fast-mode
# requests are estimated here at the standard rate.

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

ANTHROPIC, BEDROCK, VERTEX = "anthropic", "bedrock", "vertex"

# Per-provider rate tables. Bedrock and Vertex begin as copies of the
# Anthropic list price so a user on those routes gets a plausible figure
# rather than $0.00, and an exact one once their own rates are pasted into
# pricing.local.json.
PROVIDER_PRICES = {
    ANTHROPIC: PRICES,
    BEDROCK: dict(PRICES),
    VERTEX: dict(PRICES),
}

# Cache-creation tokens whose TTL the transcript did not record are billed at
# this multiplier. Claude Code uses 1h caching against the Anthropic API; other
# providers may not offer that tier, so the assumption is stated per provider
# and can be overridden rather than being buried inside a formula.
UNSPLIT_CACHE_MULT = {
    ANTHROPIC: CACHE_WRITE_1H_MULT,
    BEDROCK: CACHE_WRITE_1H_MULT,
    VERTEX: CACHE_WRITE_1H_MULT,
}

OVERRIDE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "pricing.local.json")

# Raw model id -> canonical id, for ids carrying no model name at all: a
# Bedrock application inference profile or provisioned-model ARN names a
# deployment, not a model.
MODEL_ALIASES = {}

_BY_PREFIX_BY_PROVIDER = {}


def _rebuild_indexes():
    # Longest first, so claude-opus-4-5 wins over a shorter overlapping prefix.
    for provider, table in PROVIDER_PRICES.items():
        _BY_PREFIX_BY_PROVIDER[provider] = sorted(
            table.items(), key=lambda kv: -len(kv[0]))


def load_overrides(path=OVERRIDE_PATH):
    """Merge pricing.local.json over the built-in tables. Absent file = no-op."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = _json.load(f)
    except (OSError, ValueError):
        _rebuild_indexes()
        return {}
    if not isinstance(raw, dict):
        _rebuild_indexes()
        return {}
    for provider, table in (raw.get("prices") or {}).items():
        target = PROVIDER_PRICES.setdefault(provider, {})
        for model, pair in (table or {}).items():
            try:
                target[str(model)] = (float(pair[0]), float(pair[1]))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
    for provider, mult in (raw.get("unsplit_cache_multiplier") or {}).items():
        try:
            UNSPLIT_CACHE_MULT[provider] = float(mult)
        except (TypeError, ValueError):
            continue
    MODEL_ALIASES.update({str(k): str(v)
                          for k, v in (raw.get("model_aliases") or {}).items()})
    _rebuild_indexes()
    return raw


_rebuild_indexes()
load_overrides()

# Kept for callers that predate provider awareness.
_BY_PREFIX = _BY_PREFIX_BY_PROVIDER[ANTHROPIC]

_BEDROCK_ARN = _re.compile(r"^arn:aws[a-z-]*:bedrock:", _re.I)
_BEDROCK_REGION_PREFIXES = ("us.", "eu.", "apac.", "global.", "us-gov.")
_BEDROCK_VERSION_SUFFIX = _re.compile(r"-v\d+:\d+$")


def canonical_model(raw):
    """(canonical_id, provider) for a model id from any provider.

    canonical_id is the Anthropic-form id these tables are keyed on, or None
    when the id names a deployment rather than a model - a Bedrock application
    inference profile or provisioned-model ARN. Those return None so they are
    reported as unpriced instead of guessed at; MODEL_ALIASES is how an
    operator resolves them.

    provider is None when nothing in the id identifies one, which keeps
    "<synthetic>" and anything unrecognised out of the provider tally.
    """
    if not raw:
        return None, None
    text = str(raw).strip()
    body = text
    provider = ANTHROPIC

    if _BEDROCK_ARN.match(body):
        provider = BEDROCK
        body = body.rsplit("/", 1)[-1]
    for prefix in _BEDROCK_REGION_PREFIXES:
        if body.startswith(prefix):
            provider = BEDROCK
            body = body[len(prefix):]
            break
    if body.startswith("anthropic."):
        provider = BEDROCK
        body = body[len("anthropic."):]
    if provider == BEDROCK:
        body = _BEDROCK_VERSION_SUFFIX.sub("", body)
    if "@" in body:
        if provider == ANTHROPIC:
            provider = VERTEX
        body = body.replace("@", "-")

    # An operator-supplied mapping decides the model name, but never the
    # provider: an aliased Bedrock deployment is still billed at Bedrock
    # rates, so the provider has to come from the original id. Either the raw
    # id or the decoration-stripped form can be used as the key.
    alias = MODEL_ALIASES.get(text) or MODEL_ALIASES.get(body)
    if alias:
        body = alias.replace("@", "-")

    if not body.startswith("claude-"):
        # an opaque ARN, "<synthetic>", or something we have never seen
        return None, (provider if provider != ANTHROPIC else None)
    return body, provider


def lookup(model, ts=None, provider=None):
    """Resolve a model id to (input, output) prices per million tokens.

    ts is an optional ISO-8601 request timestamp; when given, promotional
    pricing in effect at that time is applied. provider selects the rate
    table, defaulting to whatever the id itself implies. Ids still carrying
    provider decoration are canonicalised here, so a caller that never stored
    a provider still resolves correctly. Returns None if unknown.
    """
    if not model:
        return None
    canon, detected = canonical_model(model)
    if canon is None:
        return None
    provider = provider or detected or ANTHROPIC
    table = PROVIDER_PRICES.get(provider) or PROVIDER_PRICES[ANTHROPIC]
    index = (_BY_PREFIX_BY_PROVIDER.get(provider)
             or _BY_PREFIX_BY_PROVIDER[ANTHROPIC])
    m = canon.split("[")[0].strip()
    base = None
    for known, _p in index:
        if m == known or m.startswith(known):
            base = known
            break
    if base is None:
        return None
    intro = INTRO_PRICES.get(base)
    if intro and ts and str(ts) < intro[0] and provider == ANTHROPIC:
        return intro[1]
    return table[base]


def estimate_cost(model, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_5m=0, cache_1h=0, cache_unsplit=0, ts=None,
                  provider=None):
    """Estimated USD cost.

    cache_unsplit is cache-creation tokens with an unrecorded TTL, billed at
    the provider's UNSPLIT_CACHE_MULT (1h by default, which is what Claude
    Code uses against the Anthropic API).
    """
    p = lookup(model, ts, provider)
    if p is None:
        return None
    inp, out = p
    unsplit_mult = UNSPLIT_CACHE_MULT.get(provider or ANTHROPIC,
                                          CACHE_WRITE_1H_MULT)
    return (
        input_tokens * inp
        + output_tokens * out
        + cache_read * inp * CACHE_READ_MULT
        + cache_5m * inp * CACHE_WRITE_5M_MULT
        + cache_1h * inp * CACHE_WRITE_1H_MULT
        + cache_unsplit * inp * unsplit_mult
    ) / 1_000_000
