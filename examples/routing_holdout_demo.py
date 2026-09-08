#!/usr/bin/env python
"""Fit a router, then measure it somewhere it has never been.

This is the first finding, small enough to run in under a minute:

    **The router's quality generalises. Its budget does not.**

The estimator keeps most of its advantage on traffic it was not fitted on --
that part behaves the way people expect a fitted model to behave. The
*thresholds* do not. They were chosen to hit a spend target on one difficulty
mix, and a threshold is a decision boundary, not a budget: hand the same
boundary a harder mix and it escalates more of it, so the realised spend runs
well past the target nobody re-checked.

The demo also runs the spend-matched null, which is the whole reason the first
number means anything. A router that spends three times as much as the cheapest
policy should be more correct than the cheapest policy; the question is whether
it is more correct than *spending three times as much at random*. That is what
`blend` is: the same money, routed by a coin.

Run it::

    uv run python examples/routing_holdout_demo.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amg.evaluate.metrics import mcnemar
from amg.replay import Replay, run
from amg.routing.calibrate import calibrate, calibrate_blend
from amg.routing.estimator import fit
from amg.routing.policies import build
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_NAME, CATALOGUE
from amg.workload.build import PLANS, generate
from amg.workload.corpus import Corpus

#: Small enough to be an example, large enough that the intervals do not
#: swallow the effect. The shipped experiment uses 4,800 and 2,400.
SIZE = 900

#: What the thresholds were calibrated to spend, as a multiple of always-cheapest.
BUDGET = 3


def _upstreams() -> dict[str, Upstream]:
    """The catalogue, typed as the protocol.

    ``dict`` is invariant in its value type, so ``dict[str, SimulatedUpstream]``
    is not a ``dict[str, Upstream]`` however obviously one is a kind of the
    other. Naming it once here is cheaper than annotating every call site.
    """
    return dict(BY_NAME)


def correctness(replay: Replay, corpus: Corpus) -> list[bool]:
    """Per-request correctness, in corpus order, for a paired test."""
    return [
        record.response is not None and task.is_correct(record.response)
        for record, task in zip(replay.records, corpus, strict=True)
    ]


def main() -> int:
    upstreams = _upstreams()

    # Three workloads. The second is disjoint from the first by construction --
    # generated with the first one's prompts excluded, not filtered afterwards,
    # because filtering removes samples unevenly across difficulties and the
    # difficulty mix is the thing being varied.
    fit_plan = replace(PLANS["fit"], size=SIZE)
    fit_corpus = generate(fit_plan).corpus
    exclude = frozenset(task.prompt for task in fit_corpus)
    control = generate(replace(PLANS["measure"], size=SIZE, name="control"), exclude=exclude).corpus
    shifted = generate(
        replace(PLANS["shift-90"], size=SIZE, name="shift-90"), exclude=exclude
    ).corpus

    print(f"fitting on {len(fit_corpus)} tasks, {fit_corpus.hard_share:.0%} hard")
    # Fitted against the *cheapest* upstream: the question the router actually
    # asks is "will the cheap model get this right", not "how hard is this".
    estimator = fit(fit_corpus, BY_NAME[CATALOGUE[0].name])
    print(f"  converged in {estimator.iterations} Newton steps, target {estimator.target}")

    thresholds = calibrate(fit_corpus, estimator, upstreams, budget_multiple=BUDGET)
    print(f"  thresholds high={thresholds.high} low={thresholds.low}, budget {BUDGET}x")

    fitted = build("fitted", estimator=estimator, thresholds=(thresholds.high, thresholds.low))
    # The null's shares are sized ONCE, here, against the fitted router's spend
    # on the workload it was fitted on. That is what a real deployment does:
    # both are set from the same month of logs and neither is retuned. It also
    # means the spend match is exact here and drifts off this distribution,
    # which is why the last column below exists.
    shares = calibrate_blend(fit_corpus, upstreams, run(fit_corpus, fitted, upstreams).total_cost)
    null = build("blend", shares=shares)
    print(
        f"  spend-matched null routes {shares[0] / 100:.0f}% cheap, {shares[1] / 100:.0f}% middle"
    )

    print(
        f"\n{'workload':<12}{'hard':>6}{'fitted':>10}{'null':>9}{'diff':>9}"
        f"{'p':>10}{'vs cheapest':>13}{'vs null':>10}"
    )
    for label, corpus in (
        ("fit", fit_corpus),
        ("control", control),
        ("shift-90", shifted),
    ):
        cheapest_spend = run(corpus, build("cheapest"), upstreams).total_cost
        subject = run(corpus, fitted, upstreams)
        against = run(corpus, null, upstreams)
        left, right = correctness(subject, corpus), correctness(against, corpus)
        paired = mcnemar(left, right)
        print(
            f"{label:<12}{corpus.hard_share:>5.0%}"
            f"{sum(left) / len(left):>10.2%}{sum(right) / len(right):>9.2%}"
            f"{(sum(left) - sum(right)) / len(left) * 100:>+8.2f}p"
            f"{paired.p_value:>10.1e}"
            f"{subject.total_cost / cheapest_spend:>12.2f}x"
            f"{subject.total_cost / against.total_cost:>9.2f}x"
        )

    print(
        "\nRead the last column before reading the difference column.\n"
        "\n"
        "On `fit` and `control` the null spends what the fitted router spends,\n"
        "so the difference between them is the estimator and nothing else. On\n"
        "`shift-90` the match has drifted, and part of that difference is simply\n"
        "the extra money -- which is the honest way to report it and the reason\n"
        "the ratio is printed rather than assumed.\n"
        "\n"
        f"The 'vs cheapest' column is the finding. Every row used the same\n"
        f"thresholds, calibrated once to {BUDGET}x on the fitting workload. Nothing\n"
        "re-checked them, and nothing would have: a threshold is a decision\n"
        "boundary, and a harder mix pushes more requests over it.\n"
        "\n"
        "The full experiment, with intervals and four shift levels, is\n"
        "`python tasks.py evaluate` and reports/evaluation.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
