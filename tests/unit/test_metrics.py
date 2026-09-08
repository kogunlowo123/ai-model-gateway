"""Intervals, paired tests, and dominance.

The paired tests are the ones worth reading. Counterfactual replay produces
matched samples -- the same request under every policy -- and treating them as
independent throws away most of the power, so these pin that the paired
machinery actually behaves like a paired test.
"""

from __future__ import annotations

import pytest

from amg.evaluate.metrics import (
    Point,
    dominated,
    mcnemar,
    paired_bootstrap,
    quantile,
    wilson,
)

pytestmark = pytest.mark.unit


class TestWilson:
    def test_zero_out_of_n_is_not_zero_percent(self):
        # The most common way a report overstates itself: publishing a point
        # estimate from a sample that cannot support it.
        interval = wilson(0, 140)
        assert interval.point == 0.0
        assert interval.low == 0.0
        assert 0.0 < interval.high < 0.05

    def test_all_out_of_n_snaps_its_upper_bound(self):
        interval = wilson(140, 140)
        assert interval.high == 1.0

    def test_a_wider_sample_gives_a_narrower_interval(self):
        narrow = wilson(500, 1_000)
        wide = wilson(50, 100)
        assert (narrow.high - narrow.low) < (wide.high - wide.low)

    def test_an_empty_sample_is_an_empty_interval(self):
        assert wilson(0, 0).total == 0

    @pytest.mark.parametrize(("successes", "total"), [(5, 4), (-1, 10)])
    def test_impossible_counts_are_refused(self, successes, total):
        with pytest.raises(ValueError, match="not a proportion"):
            wilson(successes, total)


class TestMcNemar:
    def test_it_ignores_the_requests_both_policies_agreed_on(self):
        # The whole point of a paired test: a thousand ties carry no information
        # about which policy is better, and including them is what makes an
        # unpaired comparison so much weaker.
        first = [True] * 1_000 + [True, False]
        second = [True] * 1_000 + [False, True]
        result = mcnemar(first, second)
        assert result.ties == 1_000
        assert result.discordant == 2

    def test_a_consistent_winner_is_significant(self):
        first = [True] * 40 + [False] * 60
        second = [False] * 100
        result = mcnemar(first, second)
        assert result.wins == 40
        assert result.losses == 0
        assert result.significant

    def test_total_agreement_is_not_evidence_of_anything(self):
        result = mcnemar([True] * 50, [True] * 50)
        assert result.p_value == 1.0
        assert not result.significant

    def test_a_two_one_split_is_not_significant(self):
        first = [True, True, False]
        second = [False, False, True]
        assert not mcnemar(first, second).significant

    def test_mismatched_lengths_are_refused(self):
        with pytest.raises(ValueError, match="same length"):
            mcnemar([True], [True, False])

    def test_the_difference_is_signed_towards_the_first_policy(self):
        assert mcnemar([True, False], [False, False]).difference > 0
        assert mcnemar([False, False], [True, False]).difference < 0


class TestPairedBootstrap:
    def test_an_interval_straddling_zero_means_no_measured_difference(self):
        low, high = paired_bootstrap([1, -1] * 500)
        assert low < 0 < high

    def test_a_consistent_difference_excludes_zero(self):
        low, high = paired_bootstrap([100] * 500)
        assert low > 0
        assert high >= low

    def test_it_is_identical_on_every_run(self):
        # Seeded from a digest rather than from `random`, so a reported interval
        # does not move when some other part of the program draws a number.
        differences = [3, -1, 7, 0, -2] * 40
        assert paired_bootstrap(differences) == paired_bootstrap(differences)

    def test_an_empty_sample_is_an_empty_interval(self):
        assert paired_bootstrap([]) == (0.0, 0.0)


class TestDominance:
    def test_a_policy_that_costs_more_and_answers_worse_is_dominated(self):
        points = [
            Point("good", cost_per_request=10.0, correctness=0.9),
            Point("bad", cost_per_request=20.0, correctness=0.8),
        ]
        assert dominated(points)["bad"] == ["good"]
        assert dominated(points)["good"] == []

    def test_a_policy_on_the_frontier_is_present_with_an_empty_list(self):
        # Not absent: a reader must be able to tell "not dominated" from "not
        # measured".
        points = [
            Point("cheap", cost_per_request=1.0, correctness=0.5),
            Point("dear", cost_per_request=100.0, correctness=0.99),
        ]
        result = dominated(points)
        assert result == {"cheap": [], "dear": []}

    def test_equal_cost_and_better_quality_dominates(self):
        points = [
            Point("a", cost_per_request=10.0, correctness=0.9),
            Point("b", cost_per_request=10.0, correctness=0.8),
        ]
        assert dominated(points)["b"] == ["a"]

    def test_identical_policies_do_not_dominate_each_other(self):
        points = [
            Point("a", cost_per_request=10.0, correctness=0.9),
            Point("b", cost_per_request=10.0, correctness=0.9),
        ]
        assert dominated(points) == {"a": [], "b": []}


class TestQuantile:
    def test_it_never_invents_a_value(self):
        values = [1, 2, 3, 100]
        assert quantile(values, 0.95) in values

    def test_an_empty_sample_is_zero(self):
        assert quantile([], 0.5) == 0

    def test_the_median_of_an_odd_sample(self):
        assert quantile([5, 1, 3], 0.5) == 3
