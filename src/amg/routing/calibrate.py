"""Choosing the fitted router's thresholds, against a budget rather than a guess.

A router that sends "easy" requests to a cheap model needs to know where easy
stops. That boundary is not a property of the estimator -- it is a business
decision about how much you are willing to spend -- so it is calibrated to a
**spend budget** and not tuned to whatever maximises a score.

The budget is expressed as a multiple of what the null baseline spends: "you may
spend up to 3x what always-cheapest costs". Stating it that way makes every
comparison in the report a comparison at a matched price, which is the only way
two policies' quality numbers can be read against each other. A router quoted at
an unstated operating point is a router quoting the number that flattered it.

**Calibration happens on the fitting workload**, deliberately, because that is
what a real deployment does: you have last month's logs and you set your
thresholds from them. The experiment then measures what those thresholds are
worth on traffic they have never seen. Calibrating on the measurement workload
would be the mistake this repository exists to demonstrate, so
:mod:`amg.evaluate.experiment` refuses to do it.

The search is exact rather than iterative. Sorting the workload by estimator
score makes any threshold pair a pair of split points in that order, so prefix
sums over per-task cost and correctness turn each candidate into two subtractions
and the whole grid is evaluated without re-running the upstreams.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from amg.errors import RefusalError
from amg.routing.estimator import Estimator
from amg.routing.policies import SHARE_SCALE
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_PRICE
from amg.workload.corpus import Corpus
from amg.workload.tasks import Task, parse_answer

#: How many split points the search considers, per threshold. 200 over a
#: workload of a few thousand puts the grid finer than the sampling noise on the
#: quantity being maximised, so a finer grid would be fitting to the corpus.
GRID: Final[int] = 200

#: The default budget: three times what always-cheapest spends. Between the
#: cheapest policy (1x by definition) and the always-best one, which is roughly
#: 30x, so the router has somewhere useful to sit.
DEFAULT_BUDGET_MULTIPLE: Final[int] = 3


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The calibrated boundaries, and what they were calibrated against."""

    high: int
    low: int
    budget_multiple: int
    calibrated_on: str
    expected_cost: int
    expected_correct: int
    total: int

    @property
    def expected_accuracy(self) -> float:
        """Correct share on the calibration workload. Optimistic by construction."""
        return self.expected_correct / self.total if self.total else 0.0


def _outcomes(corpus: Corpus, upstreams: dict[str, Upstream]) -> dict[str, list[tuple[int, int]]]:
    """Per upstream, the (cost, correct) of answering each task once.

    One call per task per upstream, which is the counterfactual table the whole
    search runs on. It is affordable precisely because the upstreams are pure
    functions: there is no network here, and asking "what would flagship have
    done" is a hash and some arithmetic.
    """
    table: dict[str, list[tuple[int, int]]] = {}
    for name in BY_PRICE:
        upstream = upstreams[name]
        rows: list[tuple[int, int]] = []
        for task in corpus:
            attempt = upstream.attempt(task)
            correct = (
                attempt.ok
                and attempt.response is not None
                and parse_answer(attempt.response) is not None
                and task.is_correct(attempt.response)
            )
            rows.append((attempt.cost_micro_cents, 1 if correct else 0))
        table[name] = rows
    return table


def calibrate(
    corpus: Corpus,
    estimator: Estimator,
    upstreams: dict[str, Upstream],
    *,
    budget_multiple: int = DEFAULT_BUDGET_MULTIPLE,
) -> Thresholds:
    """Find the thresholds that answer the most, within the budget.

    Raises:
        RefusalError: if the estimator was not fitted on this corpus. Calibrating
            on a different workload than the one the estimator was fitted to is
            defensible, but doing it *by accident* is how a router ends up with
            thresholds that belong to a score distribution it will never see, so
            it has to be asked for rather than allowed.
    """
    if budget_multiple < 1:
        raise RefusalError("a budget below the cheapest policy's cost cannot be met")
    if estimator.fitted_on != corpus.digest():
        raise RefusalError(
            "the estimator was fitted on a different workload than this one",
            remedy=(
                "Calibrate on the workload the estimator was fitted to. Thresholds "
                "belong to a score distribution, and this is not that one."
            ),
        )

    tasks = list(corpus)
    order = sorted(range(len(tasks)), key=lambda index: -estimator.score(tasks[index].prompt))
    table = _outcomes(corpus, upstreams)
    cheap, middle, dear = BY_PRICE[0], BY_PRICE[1], BY_PRICE[-1]

    total = len(tasks)
    budget = sum(cost for cost, _ in table[cheap]) * budget_multiple

    # Prefix sums in the score order, so any (i, j) split is O(1) to evaluate.
    def prefix(name: str, field: int) -> list[int]:
        running = [0]
        for index in order:
            running.append(running[-1] + table[name][index][field])
        return running

    cost_cheap, correct_cheap = prefix(cheap, 0), prefix(cheap, 1)
    cost_middle, correct_middle = prefix(middle, 0), prefix(middle, 1)
    cost_dear, correct_dear = prefix(dear, 0), prefix(dear, 1)

    step = max(1, total // GRID)
    splits = sorted({*range(0, total + 1, step), total})

    best: tuple[int, int, int, int] | None = None  # correct, -cost, i, j
    for i in splits:
        for j in splits:
            if j < i:
                continue
            cost = (
                cost_cheap[i]
                + (cost_middle[j] - cost_middle[i])
                + (cost_dear[total] - cost_dear[j])
            )
            if cost > budget:
                continue
            correct = (
                correct_cheap[i]
                + (correct_middle[j] - correct_middle[i])
                + (correct_dear[total] - correct_dear[j])
            )
            candidate = (correct, -cost, i, j)
            if best is None or candidate > best:
                best = candidate

    if best is None:
        raise RefusalError(
            f"no threshold pair fits a budget of {budget_multiple}x the cheapest policy",
            remedy="Raise --budget-multiple, or check the upstream catalogue's prices.",
        )

    correct, negative_cost, i, j = best
    # A split point is a rank; the threshold is the score at that rank. Taken
    # from the sorted scores so the served comparison reproduces the split
    # exactly, including ties.
    scores = [estimator.score(tasks[index].prompt) for index in order]
    high = scores[i - 1] if i > 0 else scores[0] + 1
    low = scores[j - 1] if j > 0 else high
    return Thresholds(
        high=high,
        low=min(low, high),
        budget_multiple=budget_multiple,
        calibrated_on=corpus.digest(),
        expected_cost=-negative_cost,
        expected_correct=correct,
        total=total,
    )


def calibrate_blend(
    corpus: Corpus,
    upstreams: dict[str, Upstream],
    target_cost: int,
) -> tuple[int, int]:
    """Size the blend policy's traffic shares to spend *target_cost*.

    The blend exists to answer "is the estimator worth anything, or is the
    fitted router just spending more money?", which it can only do if the two
    spend the same. So the shares are fitted to the fitted policy's realised
    spend on the same workload, and the resulting comparison is at a matched
    price by construction rather than by luck.

    Searched with the same prefix-sum trick as :func:`calibrate`: the blend
    assigns a tier by hashing the prompt, so sorting the workload by that hash
    turns any pair of shares into a pair of split points, and every candidate
    costs two subtractions.

    Returns:
        ``(to_cheap, to_middle)`` in parts per ten thousand, whichever pair
        lands closest to the target from below where possible.
    """
    tasks = list(corpus)
    if not tasks:
        raise RefusalError("cannot calibrate a blend on an empty workload")

    def draw(task: Task) -> int:
        return (
            int.from_bytes(hashlib.blake2b(task.prompt.encode(), digest_size=8).digest(), "big")
            % SHARE_SCALE
        )

    order = sorted(range(len(tasks)), key=lambda index: draw(tasks[index]))
    table = _outcomes(corpus, upstreams)
    cheap, middle, dear = BY_PRICE[0], BY_PRICE[1], BY_PRICE[-1]

    def prefix(name: str) -> list[int]:
        running = [0]
        for index in order:
            running.append(running[-1] + table[name][index][0])
        return running

    cost_cheap, cost_middle, cost_dear = prefix(cheap), prefix(middle), prefix(dear)
    total = len(tasks)
    step = max(1, total // GRID)
    splits = sorted({*range(0, total + 1, step), total})

    best: tuple[int, int, int] | None = None  # distance, i, j
    for i in splits:
        for j in splits:
            if j < i:
                continue
            cost = (
                cost_cheap[i]
                + (cost_middle[j] - cost_middle[i])
                + (cost_dear[total] - cost_dear[j])
            )
            candidate = (abs(cost - target_cost), i, j)
            if best is None or candidate < best:
                best = candidate

    if best is None:
        raise RefusalError("no share pair could be evaluated on this workload")
    _, i, j = best
    # A split point is a rank in hash order, and the policy compares a draw
    # against a share, so the share is the rank scaled onto the share space.
    # Taking the draw at the boundary rather than the rank keeps ties on the
    # same side in both places.
    to_cheap = draw(tasks[order[i]]) if i < total else SHARE_SCALE
    to_middle = (draw(tasks[order[j]]) if j < total else SHARE_SCALE) - to_cheap
    return (to_cheap, max(0, to_middle))
