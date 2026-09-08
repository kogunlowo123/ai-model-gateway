"""Policies, features and the fitted estimator.

The theme running through this file is the boundary: a routing policy may see
the prompt text and nothing else. Several tests exist only to hold that line,
because it is the kind of thing a refactor erodes without anybody noticing until
the numbers look suspiciously good.
"""

from __future__ import annotations

import inspect

import pytest

from amg.errors import ConfigError, RefusalError
from amg.routing import features, policies
from amg.routing.calibrate import calibrate
from amg.routing.estimator import WEIGHT_SCALE, Estimator, fit
from amg.upstream.simulated import BY_PRICE, CATALOGUE
from amg.workload.build import PLANS, generate

pytestmark = pytest.mark.unit


class TestFeatures:
    def test_the_vector_matches_the_declared_layout(self):
        vector = features.extract("What is 47 * 83?")
        assert len(vector) == len(features.FEATURE_NAMES)

    def test_every_feature_is_an_integer(self):
        # Routing is a threshold comparison, and a float comparison can resolve
        # differently on two machines whose libm differs in the last place.
        assert all(isinstance(value, int) for value in features.extract("a 1 {x}"))

    def test_counts_are_clipped_so_one_outlier_cannot_dominate(self):
        vector = features.describe("x" * 5_000)
        assert vector["chars"] == features.CLIP

    def test_the_longest_number_is_a_length_not_a_value(self):
        assert features.describe("12345 and 7")["longest_number"] == 5

    def test_an_empty_prompt_produces_zeros_rather_than_raising(self):
        assert features.extract("") == (0,) * len(features.FEATURE_NAMES)

    def test_extraction_takes_only_a_string(self):
        # The signature is the enforcement. A policy cannot reach a task's
        # difficulty or answer because neither is on the parameter it is given.
        parameters = inspect.signature(features.extract).parameters
        assert list(parameters) == ["prompt"]
        assert parameters["prompt"].annotation == "str"


class TestBaselinePolicies:
    def test_cheapest_always_picks_the_cheapest(self, cheapest):
        assert cheapest.decide("anything at all").first == BY_PRICE[0]

    def test_best_always_picks_the_dearest(self):
        assert policies.Best().decide("anything").first == BY_PRICE[-1]

    def test_cascade_has_a_second_rung_and_cheapest_does_not(self, cheapest, cascade):
        assert len(cascade.decide("x").ladder) == 2
        assert len(cheapest.decide("x").ladder) == 1

    def test_a_decision_must_name_an_upstream(self):
        with pytest.raises(ConfigError, match="at least one upstream"):
            policies.Decision(ladder=(), reason="empty")

    def test_every_policy_explains_itself(self, cheapest, cascade, blend):
        for policy in (cheapest, policies.Best(), cascade, blend):
            assert policy.decide("what is 2 + 2?").reason


class TestBlend:
    def test_the_split_is_stable_for_the_same_prompt(self, blend):
        assert blend.decide("stable?").first == blend.decide("stable?").first

    def test_shares_roughly_partition_the_traffic(self, tiny):
        blend = policies.Blend(to_cheap=5_000, to_middle=2_500)
        picks = [blend.decide(task.prompt).first for task in tiny]
        # Sixty samples, so this is a sanity check on the partition rather than
        # a claim about the exact proportions.
        assert set(picks) <= set(BY_PRICE)
        assert picks.count(BY_PRICE[0]) > 0

    def test_shares_that_sum_past_the_scale_are_refused(self):
        with pytest.raises(ConfigError, match="sum to more than"):
            policies.Blend(to_cheap=8_000, to_middle=8_000)


class TestFitted:
    def test_it_routes_easy_prompts_cheaper_than_hard_ones(self, fitted):
        easy = fitted.decide("What is 12 + 7? Reply with JSON.")
        hard = fitted.decide(
            'What is 9481726 * 3810594? Reply with JSON only, as {"answer": <value>}.'
        )
        assert BY_PRICE.index(easy.first) <= BY_PRICE.index(hard.first)

    def test_thresholds_the_wrong_way_round_are_refused(self, fitted):
        with pytest.raises(ConfigError, match="below the low one"):
            policies.Fitted(estimator=fitted.estimator, high=0, low=10)

    def test_an_unconverged_estimator_cannot_be_routed_on(self, fitted):
        from dataclasses import replace

        with pytest.raises(ConfigError, match="did not converge"):
            policies.Fitted(estimator=replace(fitted.estimator, converged=False), high=10, low=0)

    def test_the_score_is_a_pure_integer_function_of_the_prompt(self, fitted):
        first = fitted.estimator.score("a prompt")
        second = fitted.estimator.score("a prompt")
        assert first == second
        assert isinstance(first, int)


class TestBuild:
    def test_a_fitted_policy_with_nothing_fitted_is_refused_not_downgraded(self):
        # Silently falling back to always-cheapest produces a gateway that is
        # healthy, cheap, and answering worse, with no symptom at all.
        with pytest.raises(ConfigError, match="needs an estimator"):
            policies.build("fitted")

    def test_a_blend_with_no_shares_is_refused(self):
        with pytest.raises(ConfigError, match="needs traffic shares"):
            policies.build("blend")

    def test_an_unknown_policy_names_the_known_ones(self):
        # The remedy, not the message: an error that says what went wrong but
        # not what to do next makes the reader go and read the source.
        with pytest.raises(ConfigError) as caught:
            policies.build("clairvoyant")
        assert "Known policies" in (caught.value.remedy or "")

    def test_every_declared_policy_can_be_built(self, fitted, calibrated_shares):
        for name in policies.POLICY_NAMES:
            built = policies.build(
                name,
                estimator=fitted.estimator,
                thresholds=(fitted.high, fitted.low),
                shares=calibrated_shares,
            )
            assert built.name == name


class TestEstimator:
    def test_it_converges_and_says_how(self, disjoint_pair, upstreams):
        fit_corpus, _ = disjoint_pair
        estimator = fit(fit_corpus, upstreams[CATALOGUE[0].name])
        assert estimator.converged
        assert 0 < estimator.iterations < 100

    def test_it_records_the_workload_it_was_fitted_to(self, disjoint_pair, upstreams):
        fit_corpus, _ = disjoint_pair
        estimator = fit(fit_corpus, upstreams[CATALOGUE[0].name])
        assert estimator.fitted_on == fit_corpus.digest()

    def test_weights_are_integers_in_fixed_point(self, fitted):
        assert all(isinstance(weight, int) for weight in fitted.estimator.weights)
        assert WEIGHT_SCALE == 1 << 20

    def test_a_round_trip_through_disk_preserves_every_decision(self, fitted, tmp_path):
        path = fitted.estimator.write(tmp_path / "estimator.json")
        loaded = Estimator.load(path)
        prompts = ["short", "What is 918273 * 42?", '{"a": 1} which field?']
        assert [loaded.score(p) for p in prompts] == [fitted.estimator.score(p) for p in prompts]

    def test_a_reordered_feature_list_is_refused_rather_than_remapped(self, fitted, tmp_path):
        import json

        path = tmp_path / "estimator.json"
        document = json.loads(fitted.estimator.to_json())
        document["features"] = list(reversed(document["features"]))
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            Estimator.load(path)
        assert "cannot be remapped" in (caught.value.remedy or "")

    def test_a_missing_estimator_says_how_to_make_one(self, tmp_path):
        with pytest.raises(ConfigError) as caught:
            Estimator.load(tmp_path / "absent.json")
        assert "amg fit" in (caught.value.remedy or "")

    def test_a_corpus_the_cheapest_model_answers_uniformly_is_refused(self, upstreams):
        # Nothing to separate means no estimator, and an estimator fitted to a
        # constant would produce thresholds that mean nothing at all.
        from dataclasses import replace

        from amg.upstream.simulated import SCALE

        corpus = generate(PLANS["tiny"]).corpus
        perfect = replace(upstreams[CATALOGUE[0].name], accuracy=(SCALE,) * 5, failure_rate=0)
        with pytest.raises(RefusalError, match="nothing for an estimator"):
            fit(corpus, perfect)


class TestCalibration:
    def test_calibrating_on_a_different_workload_is_refused(self, disjoint_pair, fitted, upstreams):
        _, control = disjoint_pair
        with pytest.raises(RefusalError, match="fitted on a different workload"):
            calibrate(control, fitted.estimator, upstreams)

    def test_a_budget_below_the_cheapest_policy_cannot_be_met(
        self, disjoint_pair, fitted, upstreams
    ):
        fit_corpus, _ = disjoint_pair
        with pytest.raises(RefusalError, match="cannot be met"):
            calibrate(fit_corpus, fitted.estimator, upstreams, budget_multiple=0)

    def test_a_larger_budget_never_buys_less(self, disjoint_pair, fitted, upstreams):
        fit_corpus, _ = disjoint_pair
        lean = calibrate(fit_corpus, fitted.estimator, upstreams, budget_multiple=2)
        rich = calibrate(fit_corpus, fitted.estimator, upstreams, budget_multiple=6)
        assert rich.expected_correct >= lean.expected_correct

    def test_the_thresholds_carry_the_workload_they_belong_to(
        self, disjoint_pair, fitted, upstreams
    ):
        fit_corpus, _ = disjoint_pair
        thresholds = calibrate(fit_corpus, fitted.estimator, upstreams)
        assert thresholds.calibrated_on == fit_corpus.digest()
