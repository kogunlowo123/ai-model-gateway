"""The routing experiment: fit once, report on traffic the fit has never seen.

The structure mirrors what a real deployment does and then asks the question a
real deployment does not.

1. **Fit** the difficulty estimator on one workload, and **calibrate** its
   thresholds on that same workload to a spend budget. This is what everybody
   does: you have last month's logs, and you set your router from them.
2. Size the **blend** policy's traffic shares to the fitted router's realised
   spend on that workload, so the two are compared at a matched price.
3. Measure every policy on a **control** workload -- disjoint from the fitting
   one, drawn from the same distribution. The difference between step 1's
   numbers and this one is the *optimism of fitting*, and it is the number a
   self-reported routing saving is missing.
4. Measure every policy on **shifted** workloads, whose difficulty mix has moved
   and whose thresholds nobody has retuned. This is what happens in production
   in month two.

Three refusals guard the result:

* the control workload must not be the fitting workload, checked by digest;
* the two must not share prompts, checked by set intersection, because a
  partially overlapping holdout reports optimism as generalisation;
* every policy's replay must reproduce, checked by running it twice.

Any of them produces **no numbers at all**. A routing report that quietly
degrades to a contaminated comparison is worse than one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from amg.errors import RefusalError
from amg.evaluate.metrics import (
    Interval,
    Paired,
    Point,
    dominated,
    mcnemar,
    paired_bootstrap,
    wilson,
)
from amg.gateway import GatewayConfig
from amg.replay import Replay, run, verify_determinism
from amg.routing.calibrate import Thresholds, calibrate, calibrate_blend
from amg.routing.estimator import Estimator, fit
from amg.routing.policies import POLICY_NAMES, Policy, build
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_PRICE
from amg.workload.corpus import Corpus

#: The policy every other policy is compared against for correctness. Not
#: `cheapest`: beating always-cheapest only proves that spending more money buys
#: more correctness. `blend` spends the *same* money as the fitted router and
#: chooses at random, so the difference between them is what the estimator is
#: worth.
NULL_POLICY: Final[str] = "blend"

#: The policy under test.
SUBJECT: Final[str] = "fitted"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """One policy on one workload."""

    policy: str
    correctness: Interval
    answered: Interval
    cost_total: int
    cost_per_request: float
    calls: int
    escalations: int
    p95_latency_us: int
    breaker_rejections: int
    digest: str

    @property
    def answered_but_wrong(self) -> float:
        """Requests that returned a well-formed answer that was wrong.

        The cascade illusion, as a number. A gateway that reports "success rate"
        from response validity alone is reporting :attr:`answered`; the share of
        that which is actually right is :attr:`correctness`. The gap is what the
        operator does not know they have.
        """
        return max(0.0, self.answered.point - self.correctness.point)


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    """Every policy, on one workload."""

    workload: str
    hard_share: float
    size: int
    digest: str
    policies: dict[str, PolicyResult]
    versus_null: Paired
    cost_difference_ci: tuple[float, float]
    dominated_by: dict[str, list[str]]

    @property
    def spend_match(self) -> float:
        """Fitted spend as a multiple of the spend-matched null's spend, here.

        **The number that says how much of a row's difference is attribution
        and how much is budget.** The blend's shares are sized once, against
        the fitted router's spend on the *fitting* workload, because that is
        what a real deployment does: both are set from last month's logs and
        neither is retuned afterwards. On that distribution the match is exact,
        and the correctness difference is cleanly attributable to the
        estimator.

        Off it, both policies' costs move and they move differently, so this
        ratio drifts above one. A shifted row's difference is then partly the
        estimator and partly the extra money, and any reading of that row has
        to say so. Recalibrating the blend per workload would pin the ratio at
        one and break something worse: the null would be a different policy on
        every row, and nothing could be compared across rows.
        """
        null = self.policies[NULL_POLICY].cost_total
        return self.policies[SUBJECT].cost_total / null if null else 0.0

    @property
    def budget_multiple(self) -> float:
        """What the fitted router actually spent, as a multiple of cheapest.

        The number the headline turns on. The thresholds were calibrated to a
        budget on the fitting workload; this is what that budget became here.
        """
        cheapest = self.policies["cheapest"].cost_total
        fitted = self.policies[SUBJECT].cost_total
        return fitted / cheapest if cheapest else 0.0


@dataclass(frozen=True, slots=True)
class Experiment:
    """The whole result, ready to be rendered or gated."""

    estimator: Estimator
    thresholds: Thresholds
    blend_shares: tuple[int, int]
    budget_multiple: int
    fit_workload: WorkloadResult
    control: WorkloadResult
    shifts: tuple[WorkloadResult, ...]

    @property
    def optimism_gap(self) -> float:
        """Fitted correctness on the fitting workload minus on the control.

        How much a router evaluated on its own training traffic overstates
        itself. Reported in correctness points.
        """
        return (
            self.fit_workload.policies[SUBJECT].correctness.point
            - self.control.policies[SUBJECT].correctness.point
        )

    @property
    def budget_drift(self) -> float:
        """The worst overspend across the shifted workloads, as a multiple.

        A router calibrated to spend 3x the cheapest policy has no mechanism
        holding it there once traffic moves: the thresholds are fixed, and what
        they cost depends on the mix arriving. This is the largest realised
        multiple across the shift sweep.
        """
        return max((shift.budget_multiple for shift in self.shifts), default=0.0)

    def all_workloads(self) -> tuple[WorkloadResult, ...]:
        """Fitting workload, control, then the shifts in the order given."""
        return (self.fit_workload, self.control, *self.shifts)


def _measure(
    corpus: Corpus,
    name: str,
    policies: dict[str, Policy],
    upstreams: dict[str, Upstream],
    config: GatewayConfig,
) -> WorkloadResult:
    """Run every policy over *corpus* and assemble the comparison."""
    replays: dict[str, Replay] = {}
    correct: dict[str, list[bool]] = {}
    costs: dict[str, list[int]] = {}

    for policy_name in POLICY_NAMES:
        policy = policies[policy_name]
        digest = verify_determinism(corpus, policy, upstreams, config)
        replay = run(corpus, policy, upstreams, config)
        if replay.digest() != digest:
            raise RefusalError(
                f"policy {policy_name!r} produced a third digest on the measured run",
                remedy="Something in the decision path is not a pure function.",
            )
        replays[policy_name] = replay
        correct[policy_name] = [
            record.response is not None and task.is_correct(record.response)
            for record, task in zip(replay.records, corpus, strict=True)
        ]
        costs[policy_name] = [record.cost_micro_cents for record in replay.records]

    results: dict[str, PolicyResult] = {}
    for policy_name, replay in replays.items():
        hits = sum(correct[policy_name])
        results[policy_name] = PolicyResult(
            policy=policy_name,
            correctness=wilson(hits, len(replay)),
            answered=wilson(replay.answered, len(replay)),
            cost_total=replay.total_cost,
            cost_per_request=replay.total_cost / len(replay) if len(replay) else 0.0,
            calls=replay.calls,
            escalations=sum(1 for record in replay.records if record.escalated),
            p95_latency_us=replay.quantile_us(0.95),
            breaker_rejections=replay.breaker_rejections,
            digest=replay.digest(),
        )

    differences = [
        subject - null for subject, null in zip(costs[SUBJECT], costs[NULL_POLICY], strict=True)
    ]
    return WorkloadResult(
        workload=name,
        hard_share=corpus.hard_share,
        size=len(corpus),
        digest=corpus.digest(),
        policies=results,
        versus_null=mcnemar(correct[SUBJECT], correct[NULL_POLICY]),
        cost_difference_ci=paired_bootstrap(differences),
        dominated_by=dominated(
            [
                Point(
                    policy=name_,
                    cost_per_request=result.cost_per_request,
                    correctness=result.correctness.point,
                )
                for name_, result in results.items()
            ]
        ),
    )


def run_experiment(  # noqa: PLR0913 - the four workload roles are the
    # experiment's design; bundling them into a config object would hide which
    # corpus plays which part, and that is the whole point of the structure.
    fit_corpus: Corpus,
    control_corpus: Corpus,
    shift_corpora: dict[str, Corpus],
    upstreams: dict[str, Upstream],
    *,
    budget_multiple: int = 3,
    config: GatewayConfig | None = None,
) -> Experiment:
    """Fit, calibrate, and measure. See the module docstring for the shape.

    Raises:
        RefusalError: if the control workload is the fitting workload, if the two
            share any prompt, or if any policy's replay fails to reproduce.
    """
    settings = config or GatewayConfig()

    if control_corpus.digest() == fit_corpus.digest():
        raise RefusalError(
            "the control workload is the fitting workload",
            remedy=(
                "Generate a disjoint one with `amg synth --plan measure "
                "--disjoint-from <fit workload>`. Measuring a fitted router on its "
                "own training traffic is the failure this experiment exists to show."
            ),
        )
    fitted_prompts = {task.prompt for task in fit_corpus}
    for name, corpus in {"control": control_corpus, **shift_corpora}.items():
        shared = fitted_prompts.intersection(task.prompt for task in corpus)
        if shared:
            raise RefusalError(
                f"{len(shared)} prompts appear in both the fitting workload and {name}",
                remedy=(
                    "Regenerate with `--disjoint-from`. Dropping the overlap after the "
                    "fact removes samples unevenly and skews the difficulty mix, which "
                    "is the very thing being varied here."
                ),
            )

    estimator = fit(fit_corpus, upstreams[BY_PRICE[0]])
    thresholds = calibrate(fit_corpus, estimator, upstreams, budget_multiple=budget_multiple)
    fitted_policy = build(
        SUBJECT, estimator=estimator, thresholds=(thresholds.high, thresholds.low)
    )
    fitted_spend = run(fit_corpus, fitted_policy, upstreams, settings).total_cost
    shares = calibrate_blend(fit_corpus, upstreams, fitted_spend)

    policies = {
        name: build(
            name,
            estimator=estimator,
            thresholds=(thresholds.high, thresholds.low),
            shares=shares,
        )
        for name in POLICY_NAMES
    }

    return Experiment(
        estimator=estimator,
        thresholds=thresholds,
        blend_shares=shares,
        budget_multiple=budget_multiple,
        fit_workload=_measure(fit_corpus, "fit", policies, upstreams, settings),
        control=_measure(control_corpus, "control", policies, upstreams, settings),
        shifts=tuple(
            _measure(corpus, name, policies, upstreams, settings)
            for name, corpus in shift_corpora.items()
        ),
    )
