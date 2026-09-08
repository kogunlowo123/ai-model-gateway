"""The command line: every measurement this project makes, reachable from a shell.

Exit codes are the interface, because the thing reading them is a pipeline:

===== ==========================================================================
0     The gate held
1     Usage error
2     A gate failed, or the run refused to report a number
3     Could not run
===== ==========================================================================

``argparse`` exits **2** on a usage error by default, which is this project's
"a gate failed" code -- so a misspelt flag would be indistinguishable from a
routing regression, and a pipeline reading exit codes would treat one as the
other. :class:`_Parser` overrides that, and ``tests/e2e/test_cli.py`` pins it,
because nothing running in-process can see the code the operating system gets.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, NoReturn

from amg.errors import (
    EXIT_OK,
    EXIT_USAGE,
    GatewayError,
)
from amg.evaluate.baseline import Baseline, enforce
from amg.evaluate.experiment import run_experiment
from amg.evaluate.report import write_reports
from amg.evaluate.resilience import sweep as run_sweep
from amg.gateway import GatewayConfig, serve_one
from amg.money import format_micro_cents
from amg.replay import verify_determinism
from amg.routing import features
from amg.routing.calibrate import DEFAULT_BUDGET_MULTIPLE, calibrate, calibrate_blend
from amg.routing.estimator import Estimator, fit
from amg.routing.policies import POLICY_NAMES, Policy, build
from amg.settings import Settings
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_NAME, CATALOGUE
from amg.workload.build import PLANS, difficulty_histogram, generate, plan_named
from amg.workload.corpus import read_corpus
from amg.workload.tasks import Task

#: The shift workloads the shipped experiment measures, in report order.
SHIFT_PLANS: Final[tuple[str, ...]] = ("shift-10", "shift-50", "shift-70", "shift-90")

#: Below this, `doctor` complains: two upstreams whose prices are within a few
#: percent make every routing comparison a measurement of nothing.
MIN_PRICE_RATIO: Final[int] = 2


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors exit 1 rather than 2.

    Without this, `amg evaluate --corpsu x` and a genuine routing regression
    both exit 2, and any pipeline distinguishing "the tool was called wrong"
    from "the gateway got worse" would be reading the same number for both.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _add_workload_commands(sub: argparse._SubParsersAction[_Parser]) -> None:
    synth = sub.add_parser("synth", help="Generate a workload from a named plan.")
    synth.add_argument("--plan", default="measure", help=f"One of: {', '.join(sorted(PLANS))}")
    synth.add_argument("--out", type=Path, required=True)
    synth.add_argument(
        "--disjoint-from",
        type=Path,
        help="Exclude every prompt in this workload, making the result disjoint.",
    )

    check = sub.add_parser("check", help="Does a committed workload still match its plan?")
    check.add_argument("--plan", required=True)
    check.add_argument("--corpus", type=Path, required=True)
    check.add_argument("--disjoint-from", type=Path)

    models = sub.add_parser("models", help="The upstream catalogue, with prices and skill.")
    models.add_argument("--json", action="store_true", dest="as_json")


def _add_routing_commands(sub: argparse._SubParsersAction[_Parser]) -> None:
    fit_parser = sub.add_parser("fit", help="Fit the difficulty estimator on a workload.")
    fit_parser.add_argument("--corpus", type=Path, required=True)
    fit_parser.add_argument("--out", type=Path, required=True)

    cal = sub.add_parser("calibrate", help="Calibrate routing thresholds to a spend budget.")
    cal.add_argument("--corpus", type=Path, required=True)
    cal.add_argument("--estimator", type=Path, required=True)
    cal.add_argument("--budget-multiple", type=int, default=DEFAULT_BUDGET_MULTIPLE)
    cal.add_argument("--json", action="store_true", dest="as_json")

    route = sub.add_parser("route", help="Where would a prompt go, and why?")
    route.add_argument("prompt")
    route.add_argument("--policy", default="cascade", choices=POLICY_NAMES)
    route.add_argument("--estimator", type=Path)
    route.add_argument("--threshold-high", type=int)
    route.add_argument("--threshold-low", type=int)
    route.add_argument("--blend-to-cheap", type=int)
    route.add_argument("--blend-to-middle", type=int)

    ask = sub.add_parser("ask", help="Send one request through the gateway.")
    ask.add_argument("prompt")
    ask.add_argument("--policy", default="cascade", choices=POLICY_NAMES)
    ask.add_argument("--estimator", type=Path)
    ask.add_argument("--threshold-high", type=int)
    ask.add_argument("--threshold-low", type=int)
    ask.add_argument("--blend-to-cheap", type=int)
    ask.add_argument("--blend-to-middle", type=int)
    ask.add_argument("--json", action="store_true", dest="as_json")


def _add_measurement_commands(sub: argparse._SubParsersAction[_Parser]) -> None:
    evaluate = sub.add_parser(
        "evaluate", help="Fit on one workload, measure on disjoint and shifted ones."
    )
    evaluate.add_argument("--corpus", type=Path, required=True, help="The fitting workload.")
    evaluate.add_argument("--control", type=Path, required=True, help="Disjoint, same mix.")
    evaluate.add_argument("--shift", type=Path, action="append", default=[])
    evaluate.add_argument("--baseline", type=Path)
    evaluate.add_argument("--update-baseline", action="store_true")
    evaluate.add_argument("--budget-multiple", type=int, default=DEFAULT_BUDGET_MULTIPLE)
    evaluate.add_argument("--with-resilience", action="store_true")
    evaluate.add_argument("--json-out", type=Path)
    evaluate.add_argument("--markdown-out", type=Path)
    evaluate.add_argument("--junit-out", type=Path)
    evaluate.add_argument("--quiet", action="store_true")

    resilience = sub.add_parser("resilience", help="Sweep faults, arms and capacity.")
    resilience.add_argument("--corpus", type=Path, required=True)
    resilience.add_argument("--policy", default="cascade", choices=POLICY_NAMES)
    resilience.add_argument("--blend-to-cheap", type=int)
    resilience.add_argument("--blend-to-middle", type=int)
    resilience.add_argument("--json-out", type=Path)

    sub.add_parser("doctor", help="Check this installation works end to end.")

    serve = sub.add_parser("serve", help="Run the HTTP gateway.")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)


def build_parser() -> _Parser:
    """The whole command line."""
    parser = _Parser(prog="amg", description=__doc__)
    parser.add_argument("--version", action="version", version="amg 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    _add_workload_commands(sub)
    _add_routing_commands(sub)
    _add_measurement_commands(sub)
    return parser


def _upstreams() -> dict[str, Upstream]:
    """The catalogue as the protocol type.

    `dict` is invariant, so a `dict[str, SimulatedUpstream]` is not a
    `dict[str, Upstream]` and every call site would otherwise need a cast.
    """
    return dict(BY_NAME)


def _cmd_synth(args: argparse.Namespace) -> int:
    exclude = (
        frozenset(task.prompt for task in read_corpus(args.disjoint_from))
        if args.disjoint_from
        else frozenset()
    )
    result = generate(plan_named(args.plan), exclude=exclude)
    path = result.corpus.write(args.out)
    print(f"{path}: {result.summary()}")
    print(f"difficulty {difficulty_histogram(result.corpus)}")
    print(f"digest {result.corpus.digest()}")
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    committed = read_corpus(args.corpus)
    exclude = (
        frozenset(task.prompt for task in read_corpus(args.disjoint_from))
        if args.disjoint_from
        else frozenset()
    )
    rebuilt = generate(plan_named(args.plan), exclude=exclude).corpus
    if committed.digest() != rebuilt.digest():
        print(f"{args.corpus} does not match plan {args.plan!r}", file=sys.stderr)
        print(f"  committed {committed.digest()}", file=sys.stderr)
        print(f"  rebuilt   {rebuilt.digest()}", file=sys.stderr)
        print("Regenerate it with `amg synth`, and commit the result.", file=sys.stderr)
        return 2
    print(f"{args.corpus} matches plan {args.plan!r} ({committed.digest()})")
    if args.disjoint_from:
        print(f"  and is disjoint from {args.disjoint_from}")
    return EXIT_OK


def _cmd_models(args: argparse.Namespace) -> int:
    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "name": upstream.name,
                        "summary": upstream.summary,
                        "accuracy_per_10k": list(upstream.accuracy),
                        "input_price_per_1k": upstream.input_price_per_1k,
                        "output_price_per_1k": upstream.output_price_per_1k,
                    }
                    for upstream in CATALOGUE
                ],
                indent=2,
            )
        )
        return EXIT_OK
    print(f"{'name':<10}{'out $/1k':>10}  {'accuracy at difficulty 1..5':<32}summary")
    for upstream in CATALOGUE:
        curve = " ".join(f"{value / 100:.0f}%" for value in upstream.accuracy)
        print(
            f"{upstream.name:<10}{format_micro_cents(upstream.output_price_per_1k):>10}  "
            f"{curve:<32}{upstream.summary}"
        )
    print("\nThese are a simulator's numbers, not any provider's. See docs/simulator.md.")
    return EXIT_OK


def _cmd_fit(args: argparse.Namespace) -> int:
    corpus = read_corpus(args.corpus)
    estimator = fit(corpus, BY_NAME[CATALOGUE[0].name])
    path = estimator.write(args.out)
    print(f"{path}: converged in {estimator.iterations} Newton steps")
    print(f"  target   {estimator.target}")
    print(f"  fitted on {estimator.fitted_on}")
    return EXIT_OK


def _cmd_calibrate(args: argparse.Namespace) -> int:
    corpus = read_corpus(args.corpus)
    estimator = Estimator.load(args.estimator)
    upstreams: dict[str, Upstream] = dict(BY_NAME)
    thresholds = calibrate(corpus, estimator, upstreams, budget_multiple=args.budget_multiple)
    policy = build("fitted", estimator=estimator, thresholds=(thresholds.high, thresholds.low))
    from amg.replay import run as replay_run  # noqa: PLC0415 - avoids a cycle

    spend = replay_run(corpus, policy, upstreams).total_cost
    shares = calibrate_blend(corpus, upstreams, spend)
    if args.as_json:
        print(
            json.dumps(
                {
                    "threshold_high": thresholds.high,
                    "threshold_low": thresholds.low,
                    "blend_to_cheap": shares[0],
                    "blend_to_middle": shares[1],
                    "budget_multiple": thresholds.budget_multiple,
                    "calibrated_on": thresholds.calibrated_on,
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(f"calibrated at {thresholds.budget_multiple}x the cheapest policy's spend")
    print(f"  AMG_THRESHOLD_HIGH={thresholds.high}")
    print(f"  AMG_THRESHOLD_LOW={thresholds.low}")
    print(f"  AMG_BLEND_TO_CHEAP={shares[0]}")
    print(f"  AMG_BLEND_TO_MIDDLE={shares[1]}")
    print(f"expected on the calibration workload: {thresholds.expected_accuracy:.2%} correct")
    print("That figure is optimistic: it is measured on the workload it was fitted to.")
    return EXIT_OK


def _shares_from_args(args: argparse.Namespace) -> tuple[int, int] | None:
    """The blend's traffic shares, or None so that `build` refuses.

    No default. The blend exists to be spend-matched against the fitted router,
    and a blend with arbitrary shares is a different policy wearing the same
    name -- which is the one degradation this CLI would otherwise allow, in the
    one place a reader is least likely to check.
    """
    cheap = getattr(args, "blend_to_cheap", None)
    middle = getattr(args, "blend_to_middle", None)
    return None if cheap is None or middle is None else (cheap, middle)


def _policy_from_args(args: argparse.Namespace) -> Policy:
    estimator = Estimator.load(args.estimator) if args.estimator else None
    thresholds = (
        (args.threshold_high, args.threshold_low)
        if args.threshold_high is not None and args.threshold_low is not None
        else None
    )
    return build(
        args.policy,
        estimator=estimator,
        thresholds=thresholds,
        shares=_shares_from_args(args),
    )


def _cmd_route(args: argparse.Namespace) -> int:
    policy = _policy_from_args(args)
    decision = policy.decide(args.prompt)
    print(f"policy   {policy.name}")
    print(f"ladder   {' -> '.join(decision.ladder)}")
    print(f"reason   {decision.reason}")
    print("features (everything the router is allowed to see):")
    for name, value in features.describe(args.prompt).items():
        print(f"  {name:<16}{value}")
    return EXIT_OK


def _cmd_ask(args: argparse.Namespace) -> int:
    policy = _policy_from_args(args)
    task = Task(task_id="cli", family="adhoc", difficulty=1, prompt=args.prompt, answer="")
    served = serve_one(task, policy, _upstreams())
    if args.as_json:
        print(
            json.dumps(
                {
                    "outcome": served.outcome,
                    "upstream": served.upstream,
                    "response": served.response,
                    "reason": served.reason,
                    "cost_micro_cents": served.cost_micro_cents,
                    "latency_us": served.latency_us,
                    "attempts": len(served.attempts),
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(f"{served.outcome:<10}via {served.upstream or '-'}  ({served.reason})")
    print(f"response  {served.response}")
    print(
        f"cost      {format_micro_cents(served.cost_micro_cents)} "
        f"in {served.latency_us / 1000:.0f} ms over {len(served.attempts)} call(s)"
    )
    return EXIT_OK


def _cmd_evaluate(args: argparse.Namespace) -> int:
    def note(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    fit_corpus = read_corpus(args.corpus)
    control = read_corpus(args.control)
    shifts = {path.stem.split(".")[0]: read_corpus(path) for path in args.shift}

    note("measuring policies over every workload")
    experiment = run_experiment(
        fit_corpus,
        control,
        shifts,
        _upstreams(),
        budget_multiple=args.budget_multiple,
    )
    sweep = None
    if args.with_resilience:
        note("sweeping faults, arms and capacity")
        sweep = run_sweep(control, build("cascade"))

    write_reports(
        experiment,
        sweep,
        json_out=args.json_out,
        markdown_out=args.markdown_out,
        junit_out=args.junit_out,
    )

    print(f"optimism gap        {experiment.optimism_gap * 100:+.2f} points")
    print(
        f"budget              calibrated {experiment.budget_multiple}x, "
        f"worst realised {experiment.budget_drift:.2f}x"
    )
    for workload in experiment.all_workloads():
        subject = workload.policies["fitted"]
        null = workload.policies["blend"]
        print(
            f"  {workload.workload:<10}hard {workload.hard_share:>4.0%}  "
            f"fitted {subject.correctness.point:>7.2%}  null {null.correctness.point:>7.2%}  "
            f"{(subject.correctness.point - null.correctness.point) * 100:>+6.2f}p  "
            f"spend {workload.budget_multiple:>5.2f}x vs cheapest, "
            f"{workload.spend_match:>4.2f}x vs null"
        )

    if args.update_baseline:
        if args.baseline is None:
            print("--update-baseline needs --baseline", file=sys.stderr)
            return EXIT_USAGE
        Baseline.from_experiment(experiment, sweep).write(args.baseline)
        print(f"\nrecorded {args.baseline}. Commit it on its own.")
        return EXIT_OK

    if args.baseline is not None:
        improvements = enforce(experiment, Baseline.load(args.baseline), sweep)
        for line in improvements:
            print(f"  improved: {line}")
        print("\nno regression against the committed baseline")
    return EXIT_OK


def _cmd_resilience(args: argparse.Namespace) -> int:
    corpus = read_corpus(args.corpus)
    sweep = run_sweep(corpus, build(args.policy, shares=_shares_from_args(args)))
    under, provisioned = sweep.under_total_outage("retry+breaker")
    harm = sweep.breaker_harm_during_partial_outage()
    print(f"policy {sweep.policy} at {sweep.arrival_rate} requests/second")
    print(
        f"breaker under a total outage: {under:.2%} answered under-provisioned, "
        f"{provisioned:.2%} provisioned"
    )
    print(f"breaker during a partial outage, under-provisioned: {harm * 100:+.2f} points")
    print(
        f"\n{'fault':<8}{'level':>7}{'pool':>6}  "
        + "".join(f"{arm:>16}" for arm in ("single", "retry", "retry+breaker"))
    )
    seen: set[tuple[str, int, int]] = set()
    for measurement in sweep.measurements:
        key = (measurement.fault, measurement.level, measurement.concurrency)
        if key in seen:
            continue
        seen.add(key)
        row = {m.arm: m for m in sweep.measurements if (m.fault, m.level, m.concurrency) == key}
        line = (
            f"{measurement.fault:<8}{measurement.level_share:>7.0%}{measurement.concurrency:>6}  "
        )
        line += "".join(
            f"{row[arm].answered.point:>16.2%}" if arm in row else f"{'-':>16}"
            for arm in ("single", "retry", "retry+breaker")
        )
        print(line)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                [
                    {
                        "arm": m.arm,
                        "fault": m.fault,
                        "level": m.level,
                        "concurrency": m.concurrency,
                        "answered": m.answered.point,
                        "expired": m.expired,
                        "calls_per_request": m.calls_per_request,
                        "p99_latency_us": m.p99_latency_us,
                    }
                    for m in sweep.measurements
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    del args
    problems: list[str] = []
    print("amg 0.1.0")

    corpus = generate(PLANS["tiny"]).corpus
    print(f"  workload      {len(corpus)} tasks, {corpus.hard_share:.0%} hard")

    prices = sorted(upstream.output_price_per_1k for upstream in CATALOGUE)
    ratio = prices[-1] // max(1, prices[0])
    print(f"  catalogue     {len(CATALOGUE)} upstreams, {ratio}x price spread")
    if ratio < MIN_PRICE_RATIO:
        problems.append(
            f"the price spread is only {ratio}x; routing between upstreams that cost "
            "the same measures nothing"
        )

    policy = build("cascade")
    digest = verify_determinism(corpus, policy, dict(BY_NAME), GatewayConfig())
    print(f"  replay        reproduces ({digest[:23]}...)")

    served = serve_one(corpus.tasks[0], policy, _upstreams())
    print(f"  gateway       {served.outcome} via {served.upstream}")
    if served.outcome == "failed":
        problems.append("the gateway could not answer a task from its own workload")

    for line in problems:
        print(f"  FAIL          {line}", file=sys.stderr)
    if problems:
        return 2
    print("\nthis installation works.")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415 - only `serve` needs a web server

    from amg.api.app import create_app  # noqa: PLC0415

    settings = Settings.from_environment()
    overrides: dict[str, object] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if overrides:
        settings = settings.model_copy(update=overrides)
    print(f"serving on http://{settings.host}:{settings.port} with policy {settings.policy}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return EXIT_OK


COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "synth": _cmd_synth,
    "check": _cmd_check,
    "models": _cmd_models,
    "fit": _cmd_fit,
    "calibrate": _cmd_calibrate,
    "route": _cmd_route,
    "ask": _cmd_ask,
    "evaluate": _cmd_evaluate,
    "resilience": _cmd_resilience,
    "doctor": _cmd_doctor,
    "serve": _cmd_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch, turning a known error into its exit code."""
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except GatewayError as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        if error.remedy:
            print(error.remedy, file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
