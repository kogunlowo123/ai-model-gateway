"""Rendering an experiment: JSON for machines, Markdown for people, JUnit for CI.

Three formats because three audiences read the same run and none of them wants
the others' output. The JSON is the record a later comparison is made against;
the Markdown is what lands in a pull request summary; the JUnit is what turns a
regression into a red test in a CI UI that has never heard of this project.

The Markdown deliberately leads with the finding rather than with the totals.
A table of policy averages is the shape of report that gets skimmed and quoted
by its best number.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree  # noqa: ICN001  # nosec B405

from amg.evaluate.experiment import NULL_POLICY, SUBJECT, Experiment
from amg.evaluate.resilience import Sweep
from amg.money import format_micro_cents
from amg.routing.policies import POLICY_NAMES


def to_json(experiment: Experiment, sweep: Sweep | None = None) -> dict[str, Any]:
    """The full result as a plain dictionary, ready for `json.dump`."""
    document: dict[str, Any] = {
        "estimator": {
            "fitted_on": experiment.estimator.fitted_on,
            "iterations": experiment.estimator.iterations,
            "converged": experiment.estimator.converged,
            "target": experiment.estimator.target,
        },
        "thresholds": {
            "high": experiment.thresholds.high,
            "low": experiment.thresholds.low,
            "budget_multiple": experiment.thresholds.budget_multiple,
            "calibrated_on": experiment.thresholds.calibrated_on,
        },
        "blend_shares": list(experiment.blend_shares),
        "optimism_gap": experiment.optimism_gap,
        "worst_budget_drift": experiment.budget_drift,
        "workloads": [],
    }
    for workload in experiment.all_workloads():
        document["workloads"].append(
            {
                "workload": workload.workload,
                "hard_share": workload.hard_share,
                "size": workload.size,
                "digest": workload.digest,
                "budget_multiple_realised": workload.budget_multiple,
                "fitted_versus_null": {
                    "wins": workload.versus_null.wins,
                    "losses": workload.versus_null.losses,
                    "ties": workload.versus_null.ties,
                    "p_value": workload.versus_null.p_value,
                    "difference": workload.versus_null.difference,
                },
                "cost_difference_ci": list(workload.cost_difference_ci),
                "spend_versus_null": workload.spend_match,
                "dominated_by": workload.dominated_by,
                "policies": {
                    name: {
                        "correctness": result.correctness.point,
                        "correctness_ci": [result.correctness.low, result.correctness.high],
                        "answered": result.answered.point,
                        "answered_but_wrong": result.answered_but_wrong,
                        "cost_total": result.cost_total,
                        "cost_per_request": result.cost_per_request,
                        "calls": result.calls,
                        "escalations": result.escalations,
                        "p95_latency_us": result.p95_latency_us,
                        "digest": result.digest,
                    }
                    for name, result in workload.policies.items()
                },
            }
        )
    if sweep is not None:
        under, provisioned = sweep.under_total_outage("retry+breaker")
        document["resilience"] = {
            "policy": sweep.policy,
            "arrival_rate": sweep.arrival_rate,
            "breaker_under_total_outage": {
                "under_provisioned": under,
                "provisioned": provisioned,
            },
            "breaker_harm_during_partial_outage": sweep.breaker_harm_during_partial_outage(),
            "measurements": [
                {
                    "arm": m.arm,
                    "fault": m.fault,
                    "level": m.level,
                    "concurrency": m.concurrency,
                    "answered": m.answered.point,
                    "expired": m.expired,
                    "calls_per_request": m.calls_per_request,
                    "p99_latency_us": m.p99_latency_us,
                    "breaker_rejections": m.breaker_rejections,
                    "peak_queue_depth": m.peak_queue_depth,
                }
                for m in sweep.measurements
            ],
        }
    return document


def to_markdown(experiment: Experiment, sweep: Sweep | None = None) -> str:
    """A summary that leads with the finding rather than with the totals."""
    optimism = (
        f"* Fitting optimism: **{experiment.optimism_gap * 100:+.2f} points** of"
        " correctness between the workload the router was fitted on and a disjoint"
        " one from the same distribution."
    )
    drift = (
        f"* Worst realised spend across the shifted workloads:"
        f" **{experiment.budget_drift:.2f}x** the cheapest policy, against a"
        f" {experiment.budget_multiple}x budget."
    )
    calibrated = (
        f"Thresholds were calibrated on the fitting workload to spend"
        f" **{experiment.budget_multiple}x** what always-cheapest spends."
    )

    spend_note = (
        "**Read the last column before reading the difference column.** The"
        " spend-matched null's traffic shares are sized once, against the fitted"
        " router's spend on the fitting workload, because that is what a real"
        " deployment does: both are set from the same month of logs and neither"
        " is retuned afterwards. So the match is exact on that distribution and"
        " drifts off it, and how it drifts decides how a row may be read."
        "\n\n"
        "* **At or below 1.00x** the fitted router bought its advantage with the"
        " same money or less, and the difference is attributable to the"
        " estimator. Below one it is a lower bound on what the estimator is"
        " worth, not a higher one.\n"
        "* **Above 1.00x** the fitted router also spent more, and part of the"
        " difference is simply the extra money. Those rows say the router"
        " escalates harder on harder traffic -- which is the first finding --"
        " and they are not clean attribution claims.\n"
        "\n"
        "Recalibrating the null per workload would pin every ratio at one and"
        " break something worse: the null would be a different policy on every"
        " row, and nothing could be compared across rows."
    )

    lines: list[str] = [
        "# Gateway evaluation",
        "",
        "## The router's quality generalises; its budget does not",
        "",
        calibrated,
        "",
        optimism,
        drift,
        "",
        spend_note,
        "",
        (
            "| Workload | Hard | Fitted | Null | Difference | p |"
            " Spend vs cheapest | Spend vs null |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for workload in experiment.all_workloads():
        subject = workload.policies[SUBJECT]
        null = workload.policies[NULL_POLICY]
        lines.append(
            f"| `{workload.workload}` | {workload.hard_share:.0%} | "
            f"{subject.correctness.point:.2%} | {null.correctness.point:.2%} | "
            f"{(subject.correctness.point - null.correctness.point) * 100:+.2f}p | "
            f"{workload.versus_null.p_value:.1e} | {workload.budget_multiple:.2f}x |"
            f" {workload.spend_match:.2f}x |"
        )

    control = experiment.control
    header = (
        "| Policy | Correct | 95% interval | Answered | Answered but wrong | Cost | Dominated by |"
    )
    lines += [
        "",
        "## Every policy, on the control workload",
        "",
        header,
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in POLICY_NAMES:
        result = control.policies[name]
        dominators = control.dominated_by.get(name) or []
        lines.append(
            f"| `{name}` | {result.correctness.point:.2%} | "
            f"{result.correctness.low:.2%} - {result.correctness.high:.2%} | "
            f"{result.answered.point:.2%} | {result.answered_but_wrong:.2%} | "
            f"{format_micro_cents(result.cost_total)} | "
            f"{', '.join(f'`{d}`' for d in dominators) if dominators else '-'} |"
        )

    cascade = control.policies.get("cascade")
    if cascade is not None:
        illusion = (
            f"**The cascade illusion.** `cascade` returns a well-formed answer to"
            f" {cascade.answered.point:.2%} of requests and a *correct* one to"
            f" {cascade.correctness.point:.2%}. A gateway reporting success from"
            f" response validity alone would claim near-perfect service while being"
            f" wrong {cascade.answered_but_wrong:.2%} of the time: self-validation"
            f" recovers only the failures it can detect."
        )
        lines += ["", illusion]

    if sweep is not None:
        under, provisioned = sweep.under_total_outage("retry+breaker")
        harm = sweep.breaker_harm_during_partial_outage()
        capacity = (
            f"Under a total outage of the cheapest provider, the same breaker serves"
            f" **{under:.2%}** of requests on a pool sized for the fast provider and"
            f" **{provisioned:.2%}** on one sized for the slow one."
        )
        harmful = (
            f"During a *partial* outage on the small pool the breaker is actively"
            f" harmful, costing up to **{harm * 100:.2f} points** against plain"
            f" retries: it fails traffic over to a provider that cannot absorb it,"
            f" and the gateway then dies on its deadline rather than on errors."
        )
        lines += [
            "",
            "## A circuit breaker is a failover mechanism, and failover needs capacity",
            "",
            capacity,
            "",
            harmful,
            "",
            "| Fault | Level | Pool | single | retry | retry+breaker |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        seen: set[tuple[str, int, int]] = set()
        for measurement in sweep.measurements:
            key = (measurement.fault, measurement.level, measurement.concurrency)
            if key in seen:
                continue
            seen.add(key)
            row = {m.arm: m for m in sweep.measurements if (m.fault, m.level, m.concurrency) == key}
            cells = " | ".join(
                f"{row[arm].answered.point:.2%}" if arm in row else "-"
                for arm in ("single", "retry", "retry+breaker")
            )
            lines.append(
                f"| {measurement.fault} | {measurement.level_share:.0%} | "
                f"{measurement.concurrency} | {cells} |"
            )

    caveat = (
        "Both the workload and the model that answers it are **simulated**, and the"
        " accuracy figures are a property of the table in"
        " `amg/upstream/simulated.py`. What is being measured is the routing and"
        " resilience arithmetic -- how much a fitted router's advantage survives"
        " held-out traffic, what a self-validating cascade can recover, what a"
        " breaker is worth at each capacity -- not the quality of any real model."
    )
    lines += ["", "## What this is a measurement of", "", caveat, ""]
    return "\n".join(lines)


def to_junit(experiment: Experiment, *, min_advantage: float = 0.0) -> str:
    """A JUnit suite, so a CI UI can show which arm regressed.

    One case per workload, failing when the fitted router's advantage over the
    spend-matched null drops below *min_advantage*. That is the assertion worth
    surfacing per-arm: a router that no longer beats a coin flip at the same
    price is a router with no reason to exist, whatever its raw correctness.
    """
    suite = ElementTree.Element(
        "testsuite",
        name="amg.routing",
        tests=str(len(experiment.all_workloads())),
    )
    failures = 0
    for workload in experiment.all_workloads():
        case = ElementTree.SubElement(
            suite,
            "testcase",
            classname="amg.routing.estimator_advantage",
            name=workload.workload,
        )
        advantage = (
            workload.policies[SUBJECT].correctness.point
            - workload.policies[NULL_POLICY].correctness.point
        )
        if advantage < min_advantage:
            failures += 1
            failure = ElementTree.SubElement(
                case,
                "failure",
                message=(
                    f"the estimator's advantage over the spend-matched null is "
                    f"{advantage:+.2%}, below {min_advantage:+.2%}"
                ),
            )
            failure.text = (
                f"workload {workload.workload}: fitted "
                f"{workload.policies[SUBJECT].correctness.point:.2%} against null "
                f"{workload.policies[NULL_POLICY].correctness.point:.2%} at "
                f"{workload.budget_multiple:.2f}x the cheapest policy's spend"
            )
        else:
            ElementTree.SubElement(
                case, "system-out"
            ).text = f"advantage {advantage:+.2%} (p={workload.versus_null.p_value:.1e})"
    suite.set("failures", str(failures))
    return ElementTree.tostring(suite, encoding="unicode") + "\n"


def write_reports(
    experiment: Experiment,
    sweep: Sweep | None = None,
    *,
    json_out: Path | None = None,
    markdown_out: Path | None = None,
    junit_out: Path | None = None,
) -> None:
    """Write whichever reports were asked for, creating directories as needed."""
    import json  # noqa: PLC0415 - only this function needs it

    renderers: tuple[tuple[Path | None, Callable[[], str]], ...] = (
        (json_out, lambda: json.dumps(to_json(experiment, sweep), indent=2, sort_keys=True)),
        (markdown_out, lambda: to_markdown(experiment, sweep)),
        (junit_out, lambda: to_junit(experiment)),
    )
    for path, render in renderers:
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render() + "\n", encoding="utf-8")
