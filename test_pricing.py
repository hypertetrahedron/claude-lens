"""Tests for pricing.py: rates, cache multipliers, fast mode, retirement.

Run with `python test_pricing.py`. Stdlib only, like everything else here.

These are arithmetic tests against a published rate card, and that is the
point: every number the dashboard shows for a backfilled row comes out of this
table, and a wrong multiplier is invisible - it produces a plausible figure.
The per-MTok assertions below are written the way the pricing page states
them ("cache reads at $0.25/MTok") so a rate change is a one-line diff here.

pricing.py holds module-level tables and a memo keyed on them, so every test
that mutates a table restores it and calls _rebuild_indexes() afterwards -
otherwise the memo answers a later test from a table that no longer exists.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pricing

MTOK = 1_000_000


class TableState(unittest.TestCase):
    """Restores every mutable pricing table after each test."""

    def setUp(self):
        self._saved = {
            "PRICES": dict(pricing.PRICES),
            "INTRO_PRICES": dict(pricing.INTRO_PRICES),
            "FAST_PRICES": dict(pricing.FAST_PRICES),
            "CACHE_READ_MULT_BY_MODEL": dict(pricing.CACHE_READ_MULT_BY_MODEL),
            "UNSPLIT_CACHE_MULT": dict(pricing.UNSPLIT_CACHE_MULT),
            "MODEL_ALIASES": dict(pricing.MODEL_ALIASES),
            "PROVIDER_PRICES": {k: dict(v)
                                for k, v in pricing.PROVIDER_PRICES.items()},
        }

    def tearDown(self):
        pricing.PRICES.clear()
        pricing.PRICES.update(self._saved["PRICES"])
        for name in ("INTRO_PRICES", "FAST_PRICES", "CACHE_READ_MULT_BY_MODEL",
                     "UNSPLIT_CACHE_MULT", "MODEL_ALIASES"):
            table = getattr(pricing, name)
            table.clear()
            table.update(self._saved[name])
        pricing.PROVIDER_PRICES.clear()
        for provider, table in self._saved["PROVIDER_PRICES"].items():
            pricing.PROVIDER_PRICES[provider] = table
        pricing._rebuild_indexes()


class CacheReadRates(TableState):
    """Cache reads are not 0.1x input on every model any more."""

    def cache_read_per_mtok(self, model, **kw):
        return pricing.estimate_cost(model, cache_read=MTOK, **kw)

    def test_fable_5_1_reads_cache_at_a_quarter_dollar(self):
        self.assertAlmostEqual(
            self.cache_read_per_mtok("claude-fable-5-1"), 0.25, places=9,
            msg="Claude Fable 5.1 cache reads are $0.25/MTok, not $1.00")

    def test_mythos_5_1_matches_fable_5_1(self):
        self.assertAlmostEqual(
            self.cache_read_per_mtok("claude-mythos-5-1"), 0.25, places=9)

    def test_fable_5_still_reads_at_the_usual_tenth(self):
        """The discount is per model: fable-5 and fable-5-1 differ 4x."""
        self.assertAlmostEqual(
            self.cache_read_per_mtok("claude-fable-5"), 1.00, places=9)

    def test_every_other_model_is_a_tenth(self):
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
                      "claude-haiku-4-5", "claude-mythos-5"):
            rate = pricing.resolve(model)
            self.assertEqual(rate.cache_read_mult, 0.1, model)

    def test_the_multiplier_follows_the_prefix_scan(self):
        """Decorated and dated ids get the model's multiplier, not the default."""
        for raw in ("claude-fable-5-1[1m]", "claude-fable-5-1-20260401",
                    "claude-mythos-5-1[1m]"):
            self.assertEqual(pricing.resolve(raw).cache_read_mult, 0.025, raw)

    def test_cache_writes_scale_off_the_same_input_rate(self):
        """A 1h cache write is 2x input; nothing model-specific about it."""
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-fable-5-1", cache_1h=MTOK),
            20.0, places=9)


class SonnetFiveIsPermanentlyCheap(TableState):
    """The 1 Sept 2026 rise was cancelled; $2/$10 is the standing rate."""

    def test_two_and_ten_on_any_date(self):
        for ts in (None, "2026-01-01T00:00:00Z", "2026-08-31T23:59:59Z",
                   "2026-09-01T00:00:00Z", "2027-06-01T00:00:00Z"):
            self.assertEqual(pricing.lookup("claude-sonnet-5", ts=ts),
                             (2.0, 10.0), repr(ts))

    def test_no_promotion_is_live(self):
        self.assertEqual(pricing.INTRO_PRICES, {},
                         "a live promotion needs an end date and a test")

    def test_the_marketplaces_keep_their_own_card(self):
        for provider in ("bedrock", "vertex"):
            self.assertEqual(
                pricing.lookup("claude-sonnet-5", provider=provider),
                (3.0, 15.0), provider)


class IntroPricing(TableState):
    """The promotion mechanism, exercised with a model that does not exist."""

    MODEL = "claude-promo-9"

    def setUp(self):
        TableState.setUp(self)
        pricing.PRICES[self.MODEL] = (4.0, 20.0)
        pricing.PROVIDER_PRICES[pricing.ANTHROPIC][self.MODEL] = (4.0, 20.0)
        pricing.PROVIDER_PRICES[pricing.BEDROCK][self.MODEL] = (4.0, 20.0)
        pricing.INTRO_PRICES[self.MODEL] = ("2026-09-01", (1.0, 5.0))
        pricing._rebuild_indexes()

    def test_promo_rate_before_the_end_date(self):
        self.assertEqual(pricing.lookup(self.MODEL, ts="2026-08-31T23:00:00Z"),
                         (1.0, 5.0))

    def test_list_rate_from_the_end_date(self):
        """The date is exclusive: the first instant of it is full price."""
        self.assertEqual(pricing.lookup(self.MODEL, ts="2026-09-01T00:00:00Z"),
                         (4.0, 20.0))

    def test_no_timestamp_means_list_rate(self):
        self.assertEqual(pricing.lookup(self.MODEL), (4.0, 20.0))

    def test_promotions_are_first_party_only(self):
        self.assertEqual(
            pricing.lookup(self.MODEL, ts="2026-08-01", provider="bedrock"),
            (4.0, 20.0), "a marketplace has its own rate card")

    def test_dated_ids_inherit_the_promotion(self):
        self.assertEqual(
            pricing.lookup(self.MODEL + "-20260701", ts="2026-08-01"),
            (1.0, 5.0))


class FastMode(TableState):
    """speed="fast" is 2x list, on two models, on the first party only."""

    def test_opus_5_doubles(self):
        self.assertEqual(pricing.lookup("claude-opus-5", speed="fast"),
                         (10.0, 50.0))
        self.assertEqual(pricing.lookup("claude-opus-5"), (5.0, 25.0))

    def test_opus_4_8_doubles(self):
        self.assertEqual(pricing.lookup("claude-opus-4-8", speed="fast"),
                         (10.0, 50.0))

    def test_no_other_model_has_a_fast_tier(self):
        for model in ("claude-opus-4-7", "claude-sonnet-5", "claude-fable-5-1",
                      "claude-haiku-4-5"):
            standard = pricing.lookup(model)
            self.assertEqual(pricing.lookup(model, speed="fast"), standard,
                             model + " has no fast mode to bill for")
            self.assertIsNone(pricing.resolve(model).fast_inp, model)

    def test_fast_mode_is_first_party_only(self):
        """Fast mode is not offered on Bedrock, Vertex or Foundry."""
        for provider in ("bedrock", "vertex"):
            self.assertEqual(
                pricing.lookup("claude-opus-5", speed="fast", provider=provider),
                (5.0, 25.0), provider)
            self.assertIsNone(
                pricing.resolve("claude-opus-5", provider=provider).fast_inp)

    def test_bedrock_decorated_id_stays_at_standard(self):
        raw = "us.anthropic.claude-opus-5-20260101-v1:0"
        self.assertEqual(pricing.lookup(raw, speed="fast"), (5.0, 25.0))

    def test_cache_multipliers_apply_on_top_of_the_fast_rate(self):
        """A fast-mode cache read is 0.1x the *fast* input rate."""
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-5", cache_read=MTOK,
                                  speed="fast"),
            1.0, places=9)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-5", cache_read=MTOK), 0.5,
            places=9)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-5", cache_1h=MTOK, speed="fast"),
            20.0, places=9)

    def test_one_rate_can_bill_a_mixed_batch(self):
        """cost_at(speed=...) is why a Rate carries the fast price too."""
        rate = pricing.resolve("claude-opus-5")
        self.assertAlmostEqual(pricing.cost_at(rate, output_tokens=MTOK),
                               25.0, places=9)
        self.assertAlmostEqual(
            pricing.cost_at(rate, output_tokens=MTOK, speed="fast"),
            50.0, places=9)

    def test_billing_fast_twice_is_not_four_times(self):
        rate = pricing.resolve("claude-opus-5", speed="fast")
        self.assertAlmostEqual(
            pricing.cost_at(rate, output_tokens=MTOK, speed="fast"),
            50.0, places=9)

    def test_speed_on_a_model_without_one_is_a_no_op(self):
        rate = pricing.resolve("claude-haiku-4-5")
        self.assertAlmostEqual(
            pricing.cost_at(rate, output_tokens=MTOK, speed="fast"),
            5.0, places=9)


class DataResidency(TableState):
    """inference_geo="us" carries a 1.1x surcharge on 4.6-and-later models."""

    def test_premium_applies_to_input_and_output(self):
        inp, out = pricing.lookup("claude-opus-4-6", inference_geo="us")
        self.assertAlmostEqual(inp, 5.5, places=9)
        self.assertAlmostEqual(out, 27.5, places=9)

    def test_older_models_cannot_pin_and_are_not_surcharged(self):
        for model in ("claude-sonnet-4-5", "claude-haiku-4-5",
                      "claude-opus-4-5"):
            self.assertEqual(pricing.lookup(model, inference_geo="us"),
                             pricing.lookup(model), model)

    def test_premium_is_first_party_only(self):
        self.assertEqual(
            pricing.lookup("claude-opus-5", inference_geo="us",
                           provider="bedrock"),
            (5.0, 25.0))

    def test_it_compounds_with_fast_mode(self):
        inp, out = pricing.lookup("claude-opus-5", speed="fast",
                                  inference_geo="us")
        self.assertAlmostEqual(inp, 11.0, places=9)
        self.assertAlmostEqual(out, 55.0, places=9)

    def test_any_other_geo_is_ignored(self):
        for geo in (None, "", "eu", "global"):
            self.assertEqual(pricing.lookup("claude-opus-5", inference_geo=geo),
                             (5.0, 25.0), repr(geo))


class PrefixResolution(TableState):
    """Dated and decorated ids resolve to the base entry, longest prefix wins."""

    def test_dated_ids(self):
        cases = {
            "claude-opus-5-20260601": (5.0, 25.0),
            "claude-fable-5-1-20260401": (10.0, 50.0),
            "claude-haiku-4-5-20251001": (1.0, 5.0),
            "claude-opus-4-20250514": (15.0, 75.0),
            "claude-sonnet-4-20250514": (3.0, 15.0),
        }
        for raw, want in cases.items():
            self.assertEqual(pricing.lookup(raw), want, raw)

    def test_one_million_context_decoration(self):
        """[1m] is a context decoration, not a long-context premium."""
        for base in ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5",
                     "claude-mythos-5-1"):
            self.assertEqual(pricing.lookup(base + "[1m]"),
                             pricing.lookup(base), base)

    def test_point_one_does_not_lose_to_its_own_prefix(self):
        """claude-fable-5 is a prefix of claude-fable-5-1; longest must win."""
        self.assertEqual(pricing.resolve("claude-fable-5").cache_read_mult, 0.1)
        self.assertEqual(pricing.resolve("claude-fable-5-1").cache_read_mult,
                         0.025)
        self.assertEqual(pricing.resolve("claude-mythos-5").cache_read_mult, 0.1)
        self.assertEqual(pricing.resolve("claude-mythos-5-1").cache_read_mult,
                         0.025)

    def test_unknown_models_are_none_not_zero(self):
        self.assertIsNone(pricing.resolve("claude-nonesuch-7"))
        self.assertIsNone(pricing.lookup(""))
        self.assertIsNone(pricing.estimate_cost(None, input_tokens=1000))


class Retirement(TableState):
    def test_retired_models_keep_their_rates(self):
        """Old transcripts still need costing; retirement is an annotation."""
        for model in pricing.RETIRED:
            self.assertIsNotNone(pricing.lookup(model), model)

    def test_status(self):
        cases = {
            "claude-opus-5": "active",
            "claude-fable-5-1[1m]": "active",
            "claude-sonnet-4-6": "active",
            "claude-opus-4-1-20250805": "retired",
            "claude-opus-4-20250514": "retired",
            "claude-sonnet-4-20250514": "retired",
            "claude-3-7-sonnet-20250219": "retired",
            "claude-3-5-haiku-20241022": "retired",
            "claude-3-haiku-20240307": "retired",
            "claude-nonesuch-7": "unknown",
            "<synthetic>": "unknown",
            "": "unknown",
        }
        for model, want in cases.items():
            self.assertEqual(pricing.status(model), want, model)

    def test_retirement_dates(self):
        cases = {
            "claude-opus-4-1": "2026-08-05",
            "claude-opus-4-20250514": "2026-06-15",
            "claude-sonnet-4-20250514": "2026-06-15",
            "claude-3-7-sonnet": "2026-02-19",
            "claude-3-5-haiku": "2026-02-19",
            "claude-3-haiku": "2026-04-20",
        }
        for model, want in cases.items():
            self.assertEqual(pricing.retired_on(model), want, model)

    def test_a_date_free_retirement_is_still_retired(self):
        self.assertEqual(pricing.status("claude-3-opus"), "retired")
        self.assertIsNone(pricing.retired_on("claude-3-opus"))

    def test_an_unknown_model_has_no_date(self):
        self.assertIsNone(pricing.retired_on("claude-nonesuch-7"))


class ToolPromptOverhead(TableState):
    def test_published_figures(self):
        cases = {
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
        for model, want in cases.items():
            self.assertEqual(pricing.tool_prompt_tokens(model), want, model)

    def test_prefix_and_decoration(self):
        self.assertEqual(pricing.tool_prompt_tokens("claude-opus-5-20260601"), 286)
        self.assertEqual(pricing.tool_prompt_tokens("claude-opus-5[1m]"), 286)
        self.assertEqual(
            pricing.tool_prompt_tokens("us.anthropic.claude-sonnet-5-20260101-v1:0"),
            354)

    def test_default_for_anything_else(self):
        for model in ("claude-fable-5-1", "claude-mythos-5-1", "claude-3-opus",
                      "claude-nonesuch-7", "<synthetic>", "", None):
            self.assertEqual(pricing.tool_prompt_tokens(model), 400, repr(model))


class Overrides(TableState):
    """pricing.local.json merges over the built-in tables; nothing is replaced."""

    def setUp(self):
        TableState.setUp(self)
        self.tmp = tempfile.mkdtemp(prefix="claude-lens-pricing-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        TableState.tearDown(self)

    def write(self, obj):
        path = os.path.join(self.tmp, "pricing.local.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_absent_file_is_a_no_op(self):
        pricing.load_overrides(os.path.join(self.tmp, "nope.json"))
        self.assertEqual(pricing.lookup("claude-opus-5"), (5.0, 25.0))

    def test_malformed_file_is_a_no_op(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(pricing.load_overrides(path), {})
        self.assertEqual(pricing.lookup("claude-opus-5"), (5.0, 25.0))

    def test_prices_are_per_provider(self):
        pricing.load_overrides(self.write(
            {"prices": {"bedrock": {"claude-opus-5": [9.0, 9.0]}}}))
        self.assertEqual(pricing.lookup("claude-opus-5", provider="bedrock"),
                         (9.0, 9.0))
        self.assertEqual(pricing.lookup("claude-opus-5", provider="anthropic"),
                         (5.0, 25.0), "an override for one provider must not leak")

    def test_cache_read_mult_override(self):
        pricing.load_overrides(self.write(
            {"cache_read_mult": {"claude-opus-5": 0.05}}))
        self.assertEqual(pricing.resolve("claude-opus-5").cache_read_mult, 0.05)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-5", cache_read=MTOK), 0.25,
            places=9)

    def test_fast_prices_override(self):
        pricing.load_overrides(self.write(
            {"fast_prices": {"claude-sonnet-5": [4.0, 20.0]}}))
        self.assertEqual(pricing.lookup("claude-sonnet-5", speed="fast"),
                         (4.0, 20.0))
        self.assertEqual(pricing.lookup("claude-sonnet-5"), (2.0, 10.0))

    def test_unsplit_cache_multiplier_override(self):
        pricing.load_overrides(self.write(
            {"unsplit_cache_multiplier": {"bedrock": 1.25}}))
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-haiku-4-5", cache_unsplit=MTOK,
                                  provider="bedrock"),
            1.25, places=9)

    def test_model_alias_override(self):
        arn = "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"
        self.assertIsNone(pricing.lookup(arn))
        pricing.load_overrides(self.write({"model_aliases": {arn: "claude-opus-5"}}))
        self.assertEqual(pricing.lookup(arn), (5.0, 25.0))

    def test_garbage_entries_are_skipped_not_fatal(self):
        pricing.load_overrides(self.write({
            "prices": {"anthropic": {"claude-opus-5": "not-a-pair"}},
            "cache_read_mult": {"claude-opus-5": "nope"},
            "fast_prices": {"claude-opus-5": [1.0]},
        }))
        self.assertEqual(pricing.lookup("claude-opus-5"), (5.0, 25.0))
        self.assertEqual(pricing.resolve("claude-opus-5").cache_read_mult, 0.1)
        self.assertEqual(pricing.lookup("claude-opus-5", speed="fast"),
                         (10.0, 50.0))

    def test_the_memo_is_cleared_when_a_table_moves(self):
        """A stale memo answers from a table that no longer exists."""
        self.assertEqual(pricing.lookup("claude-opus-5"), (5.0, 25.0))
        pricing.PROVIDER_PRICES["anthropic"]["claude-opus-5-turbo"] = (1.0, 2.0)
        pricing._rebuild_indexes()
        self.assertEqual(pricing.lookup("claude-opus-5-turbo"), (1.0, 2.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
