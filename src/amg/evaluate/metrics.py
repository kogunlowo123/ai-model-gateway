"""Intervals, paired tests, and the cost-quality frontier.

**Every comparison here is paired, and that is the point.** Counterfactual
replay serves the *same* request under every policy, so the samples are matched:
task 4,281 was answered by `cheapest` and by `fitted` and by everything else,
and the only thing that differed is the routing. Treating those as two
independent samples and comparing overlapping confidence intervals throws away
most of the information and most of the power -- two policies that differ on
three hundred requests out of two thousand can easily have overlapping marginal
intervals while disagreeing systematically.

So correctness is compared with **McNemar's exact test**, which looks only at
the requests where two policies disagreed, and cost with a **paired bootstrap**
over per-request differences. This is the mechanism earning a better test: it is
worth having replay be exact partly because it lets the statistics be sharper.

Everything is standard library. The bootstrap is seeded and integer-driven, so a
reported interval is the same on every machine and every run.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: 95% two-sided. Named rather than repeated so that changing it changes every
#: interval in the report at once.
Z: Final[float] = 1.959963984540054

#: Resamples for the paired bootstrap. Two thousand puts the Monte Carlo error
#: on a percentile interval well below the sampling error it is estimating, and
#: keeps the whole evaluation inside a few seconds.
BOOTSTRAP_RESAMPLES: Final[int] = 2_000

#: The conventional significance level. Named because it is a convention
#: rather than a property of anything, and a reader should be able to see
#: that it was chosen rather than derived.
ALPHA: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class Interval:
    """A rate and its 95% Wilson interval."""

    point: float
    low: float
    high: float
    total: int

    def __str__(self) -> str:
        return f"{self.point:.2%} [{self.low:.2%}, {self.high:.2%}]"


def wilson(successes: int, total: int) -> Interval:
    """The Wilson score interval for *successes* out of *total*.

    Wilson rather than the normal approximation because the normal one is wrong
    exactly where a routing report needs to be right: near zero and near one, on
    samples of a couple of thousand. At 0 successes the normal interval is
    [0, 0], which claims certainty from evidence that cannot support it.
    """
    if total < 0:
        raise ValueError("a total cannot be negative")
    if not 0 <= successes <= total:
        raise ValueError(f"{successes} successes out of {total} is not a proportion")
    if total == 0:
        return Interval(0.0, 0.0, 0.0, 0)
    proportion = successes / total
    denominator = 1 + Z**2 / total
    centre = (proportion + Z**2 / (2 * total)) / denominator
    spread = (
        Z * math.sqrt(proportion * (1 - proportion) / total + Z**2 / (4 * total**2)) / denominator
    )
    low, high = centre - spread, centre + spread
    # Snap the exact endpoints. Without this, zero successes reports a lower
    # bound of about 3e-18 and every reader wonders what it means.
    if successes == 0:
        low = 0.0
    if successes == total:
        high = 1.0
    return Interval(proportion, max(0.0, low), min(1.0, high), total)


@dataclass(frozen=True, slots=True)
class Paired:
    """The result of comparing two policies on the same requests.

    Attributes:
        wins: Requests the first policy got right and the second did not.
        losses: Requests the second got right and the first did not.
        ties: Requests they agreed on, which McNemar's test discards.
        p_value: Two-sided exact binomial p-value on the discordant pairs.
    """

    wins: int
    losses: int
    ties: int
    p_value: float

    @property
    def discordant(self) -> int:
        """How many requests the two policies actually disagreed about."""
        return self.wins + self.losses

    @property
    def difference(self) -> float:
        """First policy's correctness minus the second's, as a proportion."""
        total = self.discordant + self.ties
        return (self.wins - self.losses) / total if total else 0.0

    @property
    def significant(self) -> bool:
        """At the conventional 5%. A convention, and labelled as one."""
        return self.p_value < ALPHA


def mcnemar(first: Sequence[bool], second: Sequence[bool]) -> Paired:
    """Exact McNemar's test over matched correctness outcomes.

    The exact binomial form rather than the chi-squared approximation with a
    continuity correction: the approximation misbehaves when the discordant
    count is small, which is precisely the case where two good policies are
    being separated, and the exact computation costs nothing at these counts.

    Args:
        first: Whether each request was answered correctly by policy A.
        second: The same requests under policy B, in the same order.
    """
    if len(first) != len(second):
        raise ValueError("paired samples must be the same length")
    wins = sum(1 for a, b in zip(first, second, strict=True) if a and not b)
    losses = sum(1 for a, b in zip(first, second, strict=True) if b and not a)
    ties = len(first) - wins - losses
    discordant = wins + losses
    if discordant == 0:
        return Paired(wins=0, losses=0, ties=ties, p_value=1.0)

    # Two-sided exact binomial at p = 1/2: sum the probability of every outcome
    # at least as extreme as the one observed.
    extreme = min(wins, losses)
    tail = sum(math.comb(discordant, k) for k in range(extreme + 1))
    p_value = min(1.0, 2 * tail / (2**discordant))
    return Paired(wins=wins, losses=losses, ties=ties, p_value=p_value)


def paired_bootstrap(
    differences: Sequence[int], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: str = "amg"
) -> tuple[float, float]:
    """A 95% percentile interval for the mean of per-request differences.

    Used for cost, where the per-request difference between two policies is an
    integer count of micro-cents and its distribution is nothing like normal --
    most requests cost the same under both policies and a few differ by thirty
    times. A t-interval on that would be a statement about a distribution the
    data does not have.

    The resampling indices come from a seeded BLAKE2b stream rather than from
    ``random``, so the interval is identical on every machine and does not move
    when some other part of the program draws a number.
    """
    count = len(differences)
    if count == 0:
        return (0.0, 0.0)
    means: list[float] = []
    for resample in range(resamples):
        total = 0
        # One digest per resample, expanded into indices, rather than one digest
        # per index: the same stream, a fraction of the hashing.
        stream = hashlib.blake2b(f"{seed}:{resample}".encode(), digest_size=64).digest()
        position = 0
        for _ in range(count):
            if position + 8 > len(stream):
                stream = hashlib.blake2b(stream, digest_size=64).digest()
                position = 0
            index = int.from_bytes(stream[position : position + 8], "big") % count
            position += 8
            total += differences[index]
        means.append(total / count)
    means.sort()
    low = means[max(0, round(0.025 * resamples) - 1)]
    high = means[min(resamples - 1, round(0.975 * resamples) - 1)]
    return (low, high)


@dataclass(frozen=True, slots=True)
class Point:
    """One policy's position on the cost-quality plane."""

    policy: str
    cost_per_request: float
    correctness: float


def dominated(points: Sequence[Point]) -> dict[str, list[str]]:
    """Which policies are dominated, and by what.

    A policy is dominated when another costs no more **and** is at least as
    correct, with at least one of those strict. Dominance is the only comparison
    on this plane that needs no exchange rate between dollars and correctness --
    and an exchange rate is exactly the thing a routing report usually smuggles
    in by quoting "quality per dollar" as if that ratio were meaningful across
    policies at different price points.

    Returns:
        Policy name to the list of policies that dominate it, empty when none
        do. A policy on the frontier maps to an empty list rather than being
        absent, so a reader can tell "not dominated" from "not measured".
    """
    result: dict[str, list[str]] = {point.policy: [] for point in points}
    for candidate in points:
        for other in points:
            if other.policy == candidate.policy:
                continue
            cheaper_or_equal = other.cost_per_request <= candidate.cost_per_request
            better_or_equal = other.correctness >= candidate.correctness
            strictly = (
                other.cost_per_request < candidate.cost_per_request
                or other.correctness > candidate.correctness
            )
            if cheaper_or_equal and better_or_equal and strictly:
                result[candidate.policy].append(other.policy)
    return result


def quantile(values: Sequence[int], share: float) -> int:
    """The *share* quantile by nearest rank, never interpolated.

    Interpolating invents a value nothing produced. At the tail -- the only
    place anybody reads a latency quantile -- the invented value lands in the
    gap between the body of the distribution and its slow branch, which is the
    one region where being wrong actually misleads.
    """
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * len(ordered)) - 1))
    return ordered[index]
