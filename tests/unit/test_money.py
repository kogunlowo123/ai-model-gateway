"""Money is integers, and the test that says why measures the alternative."""

from __future__ import annotations

import pytest

from amg.money import (
    MICRO_CENTS_PER_DOLLAR,
    cost_of,
    dollars_per_million_to_price,
    format_micro_cents,
)

pytestmark = pytest.mark.unit


class TestPriceParsing:
    def test_a_quoted_price_becomes_micro_cents_per_1k(self):
        # $0.15 per million tokens is $0.00015 per thousand, which is 0.015
        # cents, which is 15,000 micro-cents. Written out because the chain of
        # unit conversions is exactly where this kind of code goes wrong.
        assert dollars_per_million_to_price("0.15") == 15_000

    def test_a_whole_dollar_price(self):
        assert dollars_per_million_to_price("3.00") == 300_000

    def test_an_integer_price_needs_no_point(self):
        assert dollars_per_million_to_price("12") == 1_200_000

    @pytest.mark.parametrize("value", ["", "-1.0", "1.0e-3", "abc", "+2", "1.2.3"])
    def test_anything_that_is_not_a_plain_decimal_is_refused(self, value):
        with pytest.raises(ValueError, match="plain non-negative decimal"):
            dollars_per_million_to_price(value)

    def test_more_precision_than_a_micro_cent_is_refused_rather_than_rounded(self):
        # Silently rounding a price is how a gateway's arithmetic stops matching
        # its provider's invoice, so it raises instead.
        with pytest.raises(ValueError, match="more precision"):
            dollars_per_million_to_price("0.123456789")


class TestCost:
    def test_rounds_up_never_down(self):
        # One token at 15 micro-cents per 1,000 costs 0.015 micro-cents, which
        # must not become 0: a gateway that rounds small requests down to
        # nothing under-bills every single one of them.
        assert cost_of(1, 15) == 1

    def test_an_exact_multiple_is_exact(self):
        assert cost_of(1_000, 15) == 15

    def test_zero_tokens_cost_nothing(self):
        assert cost_of(0, 300) == 0

    @pytest.mark.parametrize(("tokens", "price"), [(-1, 10), (10, -1)])
    def test_negative_inputs_are_refused(self, tokens, price):
        with pytest.raises(ValueError, match="cannot be negative"):
            cost_of(tokens, price)


class TestFloatDollarsDoNotAddUp:
    """The measurement behind the whole module, not an assertion about it."""

    def test_summing_a_hundred_thousand_float_costs_drifts_from_the_integer_sum(self):
        price = dollars_per_million_to_price("0.60")
        per_request = cost_of(437, price)

        exact = per_request * 100_000

        # The same arithmetic a gateway would do if it held dollars as floats.
        as_dollars = per_request / MICRO_CENTS_PER_DOLLAR
        drifted = 0.0
        for _ in range(100_000):
            drifted += as_dollars

        integer_dollars = exact / MICRO_CENTS_PER_DOLLAR
        # The drift is small per request and real in aggregate. Asserting it is
        # non-zero is the point: this is the failure the module exists to avoid,
        # and it is invisible to any test that adds a handful of numbers.
        assert drifted != integer_dollars
        assert abs(drifted - integer_dollars) > 0

    def test_the_integer_sum_is_exact_by_construction(self):
        price = dollars_per_million_to_price("0.60")
        per_request = cost_of(437, price)
        assert sum([per_request] * 100_000) == per_request * 100_000


class TestFormatting:
    def test_renders_exactly_without_ever_making_a_float(self):
        assert format_micro_cents(MICRO_CENTS_PER_DOLLAR) == "$1.00000000"

    def test_sub_cent_amounts_keep_every_digit(self):
        assert format_micro_cents(1) == "$0.00000001"

    def test_negative_amounts_carry_their_sign(self):
        assert format_micro_cents(-MICRO_CENTS_PER_DOLLAR) == "-$1.00000000"
