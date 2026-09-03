"""Model pricing for cost estimation of backfilled (JSONL) rows.

OTel rows carry an authoritative cost_usd computed by the Claude Code CLI;
this table is only used for historical rows where no cost was recorded.

Prices are USD per million tokens: (input, output).
Cache reads bill at 0.1x input for most models - but not all, which is what
CACHE_READ_MULT_BY_MODEL is for. Cache writes bill at 1.25x (5m TTL) / 2x
(1h TTL) of whatever input rate applies.

Model ids are matched by longest prefix, so date-suffixed ids
(claude-haiku-4-5-20251001) and decorated ids (claude-opus-5[1m]) resolve to
their base entry. The 1M-context models carry no long-context premium, so the
"[1m]" decoration does not change the rate.

What can move a rate off the base table, in the order resolve() applies it:

  promotion     INTRO_PRICES, a rate that expires on a date. First-party only.
  fast mode     FAST_PRICES, Claude Opus 5 / Opus 4.8 at 2x list. First-party
                only - fast mode does not exist on Bedrock, Vertex or Foundry.
  data residency  GEO_PREMIUM_MULT, a 1.1x surcharge for pinning inference to
                a region (inference_geo="us") on 4.6-and-later models.

Cache multipliers are applied to whichever input rate came out of that, so a
fast-mode cache read costs 0.1x the *fast* input rate.

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
from collections import namedtuple as _namedtuple
from functools import lru_cache as _lru_cache

PRICES = {
    # Current models
    "claude-fable-5": (10.0, 50.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
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

# Models whose first-party price is not the marketplace price. The Bedrock and
# Vertex tables are otherwise copies of PRICES; these are the entries where
# that copy would be wrong. Claude Sonnet 5's $2/$10 is an Anthropic-API rate
# (it started as a promotion and was made permanent); the partner rate cards
# stayed at the $3/$15 the model launched with.
PARTNER_PRICES = {
    "claude-sonnet-5": (3.0, 15.0),
}

# Time-limited promotional rates: model -> (end_date_exclusive, (input, output)).
# Applied only when a request timestamp is supplied; requests at or after the
# end date fall back to the standard PRICES entry. Anthropic-only: a promotion
# on the first-party API says nothing about a marketplace's rate card.
#
# Empty today. Claude Sonnet 5's $2/$10 lived here until the 1 September 2026
# rise was cancelled and the rate became permanent
# (platform.claude.com/docs/en/about-claude/pricing); it now sits in PRICES,
# with PARTNER_PRICES keeping Bedrock and Vertex on their own card. The
# mechanism is kept for the next promotion:
#
#     INTRO_PRICES = {"claude-something-6": ("2027-01-01", (1.0, 5.0))}
INTRO_PRICES = {}

# Fast mode: the same model served at up to 2.5x the output tokens per second,
# billed at 2x list. Claude Opus 5 and Opus 4.8 only, and first-party only -
# it is not offered on Bedrock, Vertex or Foundry, so a "fast" speed recorded
# against one of those is a transcript oddity, not a rate change.
#
# The transcript does record the speed used: message.usage.speed is "standard"
# or "fast", stored on api_requests.speed from schema v8. Pass it to resolve()
# or cost_at() and fast-mode requests are billed at the fast rate instead of
# being under-counted at half price.
FAST_PRICES = {
    "claude-opus-5": (10.0, 50.0),
    "claude-opus-4-8": (10.0, 50.0),
}

# Pinning inference to a geography (inference_geo="us") carries a surcharge on
# the models that support it - Opus 4.6 / Sonnet 4.6 and later. Applied to the
# input and output rate alike, which is the simple reading of a "1.1x" premium;
# no per-model figure is published, so this is one number, stated here rather
# than buried in a formula. First-party only.
GEO_PREMIUM_MULT = 1.1
GEO_PREMIUM_MODELS = frozenset((
    "claude-fable-5", "claude-fable-5-1", "claude-mythos-5", "claude-mythos-5-1",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6",
))

# Retirement dates, so the dashboard can say "this model is gone" rather than
# leaving a reader to wonder why a line stops. Keyed on the base table key, and
# resolved by the same longest-prefix scan as the rate. A None value means
# retired on a date this file does not record - the Claude 3 generation, which
# predates any transcript that is likely to still be on disk.
RETIRED = {
    "claude-opus-4-1": "2026-08-05",
    "claude-opus-4-0": "2026-06-15",
    "claude-opus-4-2025": "2026-06-15",
    "claude-sonnet-4-0": "2026-06-15",
    "claude-sonnet-4-2025": "2026-06-15",
    "claude-3-7-sonnet": "2026-02-19",
    "claude-3-5-haiku": "2026-02-19",
    "claude-haiku-3-5": "2026-02-19",
    "claude-3-haiku": "2026-04-20",
    "claude-3-5-sonnet": None,
    "claude-3-sonnet": None,
    "claude-3-opus": None,
}

# Tokens the tool-use system prompt costs on every request, by model, from
# Anthropic's published per-model figures. Claude Code sends a tool definition
# block with each request whether or not a tool is called, so this is a floor
# on the input of every turn; the dashboard multiplies it by the request count
# to show what the harness itself costs. Prefix-matched like everything else.
TOOL_PROMPT_TOKENS = {
    "claude-opus-5": 286,
    "claude-opus-4-8": 290,
    "claude-opus-4-7": 675,
    "claude-opus-4-6": 497,
    "claude-opus-4-5": 496,
    "claude-sonnet-5": 354,
    "claude-sonnet-4-6": 497,
    "claude-sonnet-4-5": 496,
    "claude-haiku-4-5": 496,
}
TOOL_PROMPT_DEFAULT = 400

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# Cache reads bill at CACHE_READ_MULT x input for every model except the few
# that price them differently. Keyed on the *base* table key, so it is resolved
# by the same longest-prefix scan as the rate itself and costs one dict lookup
# once per (model, provider) - not once per request.
#
# Claude Fable 5.1 and Claude Mythos 5.1 read cache at $0.25/MTok against a
# $10/MTok input rate: 0.025x, a quarter of the usual discount rate. On a
# cache-heavy agent that is the difference between a right answer and one four
# times too large, which is why this is a table and not a constant.
CACHE_READ_MULT_BY_MODEL = {
    "claude-fable-5-1": 0.025,
    "claude-mythos-5-1": 0.025,
}

# A resolved rate: what one request costs per token, everything a caller needs
# in a single tuple so the table is consulted once per distinct model.
# fast_inp/fast_out are the same model's fast-mode rate, or None where it has
# none, so a caller holding one Rate for a batch can still bill the fast
# requests in that batch correctly - see cost_at(speed=...).
Rate = _namedtuple("Rate", "inp out cache_read_mult fast_inp fast_out")
Rate.__new__.__defaults__ = (None, None)

ANTHROPIC, BEDROCK, VERTEX = "anthropic", "bedrock", "vertex"

# Per-provider rate tables. Bedrock and Vertex begin as copies of the
# Anthropic list price so a user on those routes gets a plausible figure
# rather than $0.00, and an exact one once their own rates are pasted into
# pricing.local.json. PARTNER_PRICES names the entries where the copy is
# known to be wrong.
PROVIDER_PRICES = {
    ANTHROPIC: PRICES,
    BEDROCK: dict(PRICES, **PARTNER_PRICES),
    VERTEX: dict(PRICES, **PARTNER_PRICES),
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
_TOOL_PROMPT_BY_PREFIX = []


def _rebuild_indexes():
    # Longest first, so claude-opus-4-5 wins over a shorter overlapping prefix.
    for provider, table in PROVIDER_PRICES.items():
        _BY_PREFIX_BY_PROVIDER[provider] = sorted(
            table.items(), key=lambda kv: -len(kv[0]))
    _TOOL_PROMPT_BY_PREFIX[:] = sorted(
        TOOL_PROMPT_TOKENS.items(), key=lambda kv: -len(kv[0]))
    # The memo below answers from these indexes and from MODEL_ALIASES, both
    # of which just moved. (Not yet defined during module import.)
    memo = globals().get("_resolve_base")
    if memo is not None:
        memo.cache_clear()


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
    for model, pair in (raw.get("fast_prices") or {}).items():
        try:
            FAST_PRICES[str(model)] = (float(pair[0]), float(pair[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    for model, mult in (raw.get("cache_read_mult") or {}).items():
        try:
            CACHE_READ_MULT_BY_MODEL[str(model)] = float(mult)
        except (TypeError, ValueError):
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


@_lru_cache(maxsize=1024)
def _resolve_base(model, provider):
    """(table key, provider) for a raw model id, or None if unknown.

    This is the expensive half of a price lookup - canonicalisation plus a
    linear scan of the prefix table - and it does not depend on the request
    timestamp, speed or geography, so it is memoized. A build costs one scan
    per distinct (model, provider) pair instead of two per API request.
    _rebuild_indexes() clears it whenever the tables or aliases change.
    """
    canon, detected = canonical_model(model)
    if canon is None:
        return None
    provider = provider or detected or ANTHROPIC
    index = (_BY_PREFIX_BY_PROVIDER.get(provider)
             or _BY_PREFIX_BY_PROVIDER[ANTHROPIC])
    m = canon.split("[")[0].strip()
    for known, _p in index:
        if m == known or m.startswith(known):
            return known, provider
    return None


def resolve(model, ts=None, provider=None, speed=None, inference_geo=None):
    """Rate per million tokens for one request's circumstances, or None.

    ts       ISO-8601 request timestamp; when given, promotional pricing in
             effect at that time is applied (INTRO_PRICES, first-party only).
    speed    "fast" bills Claude Opus 5 / Opus 4.8 at FAST_PRICES - 2x list -
             on the Anthropic provider. Anything else, including None, is the
             standard rate. The returned Rate also carries the fast rate in
             fast_inp/fast_out so one Rate can bill a mixed batch.
    inference_geo  "us" adds the GEO_PREMIUM_MULT residency surcharge on the
             models that support pinning (4.6 and later), first-party only.

    Only the timestamp/speed/geography part is recomputed per call - the table
    lookup itself is memoized - so callers may hold on to a Rate for a whole
    batch of requests at one price.
    """
    if not model:
        return None
    found = _resolve_base(model, provider)
    if found is None:
        return None
    base, provider = found
    first_party = provider == ANTHROPIC

    intro = INTRO_PRICES.get(base)
    if intro and ts and first_party and str(ts) < intro[0]:
        inp, out = intro[1]
    else:
        table = PROVIDER_PRICES.get(provider) or PROVIDER_PRICES[ANTHROPIC]
        inp, out = table[base]

    fast = FAST_PRICES.get(base) if first_party else None
    if fast and speed == "fast":
        inp, out = fast

    if (inference_geo == "us" and first_party
            and base in GEO_PREMIUM_MODELS):
        inp, out = inp * GEO_PREMIUM_MULT, out * GEO_PREMIUM_MULT
        if fast:
            fast = (fast[0] * GEO_PREMIUM_MULT, fast[1] * GEO_PREMIUM_MULT)

    return Rate(inp, out, CACHE_READ_MULT_BY_MODEL.get(base, CACHE_READ_MULT),
                fast[0] if fast else None, fast[1] if fast else None)


def status(model):
    """"active", "retired" or "unknown" for a model id from any provider.

    "unknown" means this file has no entry at all - an unrecognised id or an
    opaque deployment ARN - which is also the case where cost is reported as
    unpriced rather than estimated.
    """
    found = _resolve_base(model, None)
    if found is None:
        return "unknown"
    return "retired" if found[0] in RETIRED else "active"


def retired_on(model):
    """Retirement date as YYYY-MM-DD, or None (still served, or date unknown).

    None is deliberately ambiguous between the two; status() is what
    distinguishes them.
    """
    found = _resolve_base(model, None)
    return None if found is None else RETIRED.get(found[0])


def tool_prompt_tokens(model):
    """Tool-use system prompt tokens charged on every request for this model.

    Longest-prefix match over TOOL_PROMPT_TOKENS, falling back to
    TOOL_PROMPT_DEFAULT (400) - an order-of-magnitude figure for a model with
    no published number, which is what the dashboard's overhead estimate is
    worth anyway.
    """
    canon, _ = canonical_model(model)
    if not canon:
        return TOOL_PROMPT_DEFAULT
    m = canon.split("[")[0].strip()
    for known, n in _TOOL_PROMPT_BY_PREFIX:
        if m == known or m.startswith(known):
            return n
    return TOOL_PROMPT_DEFAULT


def lookup(model, ts=None, provider=None, speed=None, inference_geo=None):
    """(input, output) prices per million tokens, or None. See resolve()."""
    r = resolve(model, ts, provider, speed, inference_geo)
    return None if r is None else (r.inp, r.out)


def estimate_cost(model, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_5m=0, cache_1h=0, cache_unsplit=0, ts=None,
                  provider=None, speed=None, inference_geo=None):
    """Estimated USD cost.

    cache_unsplit is cache-creation tokens with an unrecorded TTL, billed at
    the provider's UNSPLIT_CACHE_MULT (1h by default, which is what Claude
    Code uses against the Anthropic API).
    """
    rate = resolve(model, ts, provider, speed, inference_geo)
    if rate is None:
        return None
    return cost_at(rate, input_tokens, output_tokens, cache_read,
                   cache_5m, cache_1h, cache_unsplit, provider)


def cost_at(rate, input_tokens=0, output_tokens=0, cache_read=0,
            cache_5m=0, cache_1h=0, cache_unsplit=0, provider=None,
            speed=None):
    """estimate_cost() against an already-resolved Rate.

    Split out so a caller aggregating many requests at one price resolves once
    and bills many times; estimate_cost() is this plus the resolve.

    speed="fast" switches to the Rate's fast-mode price where it has one, so a
    batch resolved once at the standard rate can still bill its fast requests
    correctly. It is a no-op on a model with no fast tier, and on a Rate that
    resolve() already returned at the fast rate.
    """
    inp, out = rate.inp, rate.out
    if speed == "fast" and rate.fast_inp is not None:
        inp, out = rate.fast_inp, rate.fast_out
    unsplit_mult = UNSPLIT_CACHE_MULT.get(provider or ANTHROPIC,
                                          CACHE_WRITE_1H_MULT)
    return (
        input_tokens * inp
        + output_tokens * out
        + cache_read * inp * rate.cache_read_mult
        + cache_5m * inp * CACHE_WRITE_5M_MULT
        + cache_1h * inp * CACHE_WRITE_1H_MULT
        + cache_unsplit * inp * unsplit_mult
    ) / 1_000_000
