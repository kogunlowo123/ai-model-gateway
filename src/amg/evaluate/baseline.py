"""The committed baseline, and the regression gate that reads it.

A single absolute budget cannot gate this project. "Correctness above 80%" is
meaningless when the shifted workloads legitimately sit at 65% by design, and
"cost below X" is meaningless when the whole finding is that cost moves with the
traffic mix. Either would be a gate that passes whatever happens, which is the
failure mode this series exists to argue against.

So the gate is a **comparison against a recorded measurement**.
``examples/baseline.json`` holds what this gateway actually did -- per workload,
per policy -- on a specific pair of corpora at a specific budget, and CI
re-measures and fails on a move past a tolerance.

Six things fail the build:

* **correctness regressed** on any workload for any policy;
* **cost grew** on any workload for any policy;
* the **estimator's advantage over the spend-matched null** shrank -- the number
  that says the router is worth having at all;
* a workload or policy in the baseline that this run **did not measure**, which
  is how a silently deleted arm passes as a clean run;
* a workload or policy this run measured that the baseline has **never heard
  of**, which is how a renamed arm stops being gated;
* the **resilience figures**, when the recorded baseline carries them -- the
  breaker arm's answered rate under a total outage, at each capacity. Those two
  numbers are on the front page, and a number on a front page that nothing
  re-measures is a number that will eventually be wrong. Recording them takes a
  sweep, so a run without ``--with-resilience`` against a baseline that has them
  fails rather than quietly skipping the check.

The two shape checks -- an arm that vanished, an arm that appeared -- are the
ones people leave out, and they are the reason a gate that has only ever been
observed passing is indistinguishable from ``true``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from amg.errors import ConfigError, GateError
from amg.evaluate.experiment import NULL_POLICY, SUBJECT, Experiment
from amg.evaluate.resilience import PROVISIONED, UNDER_PROVISIONED, Sweep

FORMAT: Final[int] = 1

#: How far a rate may move before the gate fires, in proportion points. Wide
#: enough to absorb sampling noise on a 2,400-request workload -- the Wilson
#: half-width there is around 1.6 points at these rates -- and narrow enough
#: that a real regression cannot hide inside it.
DEFAULT_TOLERANCE: Final[float] = 0.02

#: How far cost may grow before the gate fires, as a proportion of the recorded
#: figure. Cost is a deterministic sum here rather than a sample, so this is
#: much tighter than the correctness tolerance: anything above rounding is a
#: real change in what the gateway spends.
DEFAULT_COST_TOLERANCE: Final[float] = 0.01

#: Floating-point slack, so a bit-identical rerun cannot fail on representation.
EPSILON: Final[float] = 1e-9


@dataclass(frozen=True, slots=True)
class Baseline:
    """What was measured, in the form the gate compares against."""

    correctness: dict[str, dict[str, float]]
    cost_total: dict[str, dict[str, float]]
    estimator_advantage: dict[str, float]
    optimism_gap: float
    budget_multiple: int
    worst_budget_drift: float
    fit_digest: str
    control_digest: str
    #: The breaker arm's answered rate under a total outage, keyed by pool
    #: size as a string. Empty when the recording run had no sweep, which
    #: is why every check on it is conditional rather than assuming a key.
    breaker_under_total_outage: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_experiment(cls, experiment: Experiment, sweep: Sweep | None = None) -> Baseline:
        """Record what *experiment* measured."""
        correctness: dict[str, dict[str, float]] = {}
        cost: dict[str, dict[str, float]] = {}
        advantage: dict[str, float] = {}
        for workload in experiment.all_workloads():
            correctness[workload.workload] = {
                name: result.correctness.point for name, result in workload.policies.items()
            }
            cost[workload.workload] = {
                name: float(result.cost_total) for name, result in workload.policies.items()
            }
            advantage[workload.workload] = (
                workload.policies[SUBJECT].correctness.point
                - workload.policies[NULL_POLICY].correctness.point
            )
        return cls(
            correctness=correctness,
            cost_total=cost,
            estimator_advantage=advantage,
            optimism_gap=experiment.optimism_gap,
            budget_multiple=experiment.budget_multiple,
            worst_budget_drift=experiment.budget_drift,
            fit_digest=experiment.fit_workload.digest,
            control_digest=experiment.control.digest,
            breaker_under_total_outage=_outage_figures(sweep),
        )

    def to_json(self) -> str:
        """Serialise, sorted, so a committed baseline has a stable diff."""
        return json.dumps(
            {
                "format": FORMAT,
                "correctness": self.correctness,
                "cost_total": self.cost_total,
                "estimator_advantage": self.estimator_advantage,
                "optimism_gap": self.optimism_gap,
                "budget_multiple": self.budget_multiple,
                "worst_budget_drift": self.worst_budget_drift,
                "fit_digest": self.fit_digest,
                "control_digest": self.control_digest,
                "breaker_under_total_outage": self.breaker_under_total_outage,
            },
            indent=2,
            sort_keys=True,
        )

    def write(self, path: Path) -> Path:
        """Write the baseline to *path*, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Baseline:
        """Read a committed baseline."""
        if not path.exists():
            raise ConfigError(
                f"no baseline at {path}",
                remedy=(
                    "Record one with `amg evaluate --update-baseline`, and commit it "
                    "on its own so the diff shows what moved."
                ),
            )
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") != FORMAT:
            raise ConfigError(
                f"{path} is format {document.get('format')}, this build expects {FORMAT}"
            )
        return cls(
            correctness=document["correctness"],
            cost_total=document["cost_total"],
            estimator_advantage=document["estimator_advantage"],
            optimism_gap=float(document["optimism_gap"]),
            budget_multiple=int(document["budget_multiple"]),
            worst_budget_drift=float(document["worst_budget_drift"]),
            fit_digest=str(document["fit_digest"]),
            control_digest=str(document["control_digest"]),
            breaker_under_total_outage={
                str(key): float(value)
                for key, value in document.get("breaker_under_total_outage", {}).items()
            },
        )


def _outage_figures(sweep: Sweep | None) -> dict[str, float]:
    """The breaker arm's answered rate under a total outage, at each capacity."""
    if sweep is None:
        return {}
    under, provisioned = sweep.under_total_outage("retry+breaker")
    return {str(UNDER_PROVISIONED): under, str(PROVISIONED): provisioned}


def enforce(  # noqa: C901, PLR0912 - one branch per failure mode, and each is a
    # distinct thing a reader needs to see named; collapsing them into a table
    # would make the messages generic exactly where they need to be specific.
    experiment: Experiment,
    baseline: Baseline,
    sweep: Sweep | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    cost_tolerance: float = DEFAULT_COST_TOLERANCE,
) -> list[str]:
    """Compare *experiment* to *baseline*; raise on a regression.

    Returns:
        The improvements, so a passing run still says what got better. A gate
        that prints nothing when things improve trains people to read only its
        exit code, and then nobody notices the day it stops measuring.

    Raises:
        GateError: on any regression, missing arm, or unknown arm.
    """
    problems: list[str] = []
    improvements: list[str] = []

    measured = {workload.workload: workload for workload in experiment.all_workloads()}

    missing = set(baseline.correctness) - set(measured)
    if missing:
        problems.append(
            f"the baseline records {sorted(missing)} but this run did not measure them; "
            "a silently dropped workload is not a passing run"
        )
    unknown = set(measured) - set(baseline.correctness)
    if unknown:
        problems.append(
            f"this run measured {sorted(unknown)}, which the baseline has never heard of; "
            "re-record the baseline deliberately rather than letting a new arm go ungated"
        )

    for name in sorted(set(baseline.correctness) & set(measured)):
        workload = measured[name]
        recorded = baseline.correctness[name]

        missing_policies = set(recorded) - set(workload.policies)
        if missing_policies:
            problems.append(f"{name}: policies {sorted(missing_policies)} were not measured")
        unknown_policies = set(workload.policies) - set(recorded)
        if unknown_policies:
            problems.append(f"{name}: policies {sorted(unknown_policies)} are not in the baseline")

        for policy in sorted(set(recorded) & set(workload.policies)):
            was = recorded[policy]
            now = workload.policies[policy].correctness.point
            if now < was - tolerance - EPSILON:
                problems.append(
                    f"{name}/{policy}: correctness fell from {was:.2%} to {now:.2%} "
                    f"(tolerance {tolerance:.0%})"
                )
            elif now > was + tolerance:
                improvements.append(f"{name}/{policy}: correctness {was:.2%} -> {now:.2%}")

            recorded_cost = baseline.cost_total.get(name, {}).get(policy)
            if recorded_cost is not None:
                current_cost = float(workload.policies[policy].cost_total)
                ceiling = recorded_cost * (1 + cost_tolerance) + EPSILON
                if current_cost > ceiling:
                    problems.append(
                        f"{name}/{policy}: cost grew from {recorded_cost:,.0f} to "
                        f"{current_cost:,.0f} micro-cents "
                        f"(tolerance {cost_tolerance:.0%})"
                    )
                elif current_cost < recorded_cost * (1 - cost_tolerance):
                    improvements.append(
                        f"{name}/{policy}: cost {recorded_cost:,.0f} -> {current_cost:,.0f}"
                    )

        was_advantage = baseline.estimator_advantage.get(name)
        if was_advantage is not None:
            now_advantage = (
                workload.policies[SUBJECT].correctness.point
                - workload.policies[NULL_POLICY].correctness.point
            )
            if now_advantage < was_advantage - tolerance - EPSILON:
                problems.append(
                    f"{name}: the estimator's advantage over the spend-matched null fell "
                    f"from {was_advantage:+.2%} to {now_advantage:+.2%} -- the router is "
                    "worth less than it was"
                )

    if baseline.breaker_under_total_outage:
        if sweep is None:
            problems.append(
                "the baseline records resilience figures and this run swept nothing; "
                "pass --with-resilience, or re-record a baseline without them"
            )
        else:
            swept = _outage_figures(sweep)
            for capacity, was in sorted(baseline.breaker_under_total_outage.items()):
                rate = swept.get(capacity)
                if rate is None:
                    problems.append(f"resilience/pool {capacity}: not swept by this run")
                elif rate < was - tolerance - EPSILON:
                    problems.append(
                        f"resilience/pool {capacity}: the breaker arm's answered rate "
                        f"under a total outage fell from {was:.2%} to {rate:.2%}"
                    )

    if problems:
        raise GateError(
            "the gateway regressed against its committed baseline:\n  - " + "\n  - ".join(problems),
            remedy=(
                "Fix the regression, or re-record with `amg evaluate --update-baseline` "
                "if the change was intended -- in its own commit, so the diff is the "
                "argument for it."
            ),
        )
    return improvements
