#!/usr/bin/env python
"""Route one request, then a thousand, and show what changes between them.

The point of the example is the gap. A single request through an idle gateway
tells you almost nothing about a loaded one: the queue is empty, no retry
competes for a slot, and the latency you measure is the latency you would get if
you were the only caller. Every resilience number in this repository is a loaded
number for exactly that reason, and this is where a reader sees why.

Run it::

    uv run python examples/quickstart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amg.gateway import GatewayConfig, serve_one
from amg.money import format_micro_cents
from amg.replay import run
from amg.routing.policies import build
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_NAME
from amg.workload.build import PLANS, generate

POLICIES = ("cheapest", "best", "cascade")
OUTCOMES = ("answered", "malformed", "failed", "expired")


def _upstreams() -> dict[str, Upstream]:
    """The catalogue, typed as the protocol.

    ``dict`` is invariant in its value type, so ``dict[str, SimulatedUpstream]``
    is not a ``dict[str, Upstream]`` however obviously one is a kind of the
    other. Naming it once here is cheaper than annotating every call site.
    """
    return dict(BY_NAME)


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    upstreams = _upstreams()
    corpus = generate(PLANS["measure"]).corpus

    rule("one request, idle gateway")
    task = next(t for t in corpus if t.difficulty == 1)
    print(f"prompt   {task.prompt}")
    for name in POLICIES:
        served = serve_one(task, build(name), upstreams)
        print(
            f"  {name:<9}{served.outcome:<10}via {served.upstream or '-':<10}"
            f"{format_micro_cents(served.cost_micro_cents):>12}"
            f"{served.latency_us / 1000:>9.1f} ms"
        )

    # Sequential mode: every request in its own empty simulation. No queueing, no
    # contention, and therefore no resilience information at all. This is the
    # number a benchmark script produces when nobody has thought about load.
    rule(f"{len(corpus)} requests, one at a time")
    print(f"{'policy':<10}{'answered':>10}{'correct':>10}{'spend':>14}{'p99':>12}")
    for name in POLICIES:
        replay = run(corpus, build(name), upstreams)
        correct = sum(
            1
            for record, source in zip(replay.records, corpus, strict=True)
            if record.response is not None and source.is_correct(record.response)
        )
        print(
            f"{name:<10}{replay.answered / len(replay):>9.2%}"
            f"{correct / len(replay):>10.2%}"
            f"{format_micro_cents(replay.total_cost):>14}"
            f"{replay.quantile_us(0.99) / 1000:>9.1f} ms"
        )

    # The same corpus, arriving at a rate, against a bounded pool. Nothing about
    # the policies changed. Only the load did.
    rule(f"{len(corpus)} requests arriving at 40/second, 24 slots")
    config = GatewayConfig(concurrency=24)
    print(f"{'policy':<10}" + "".join(f"{name:>11}" for name in OUTCOMES), end="")
    print(f"{'p99':>12}{'peak queue':>13}")
    for name in POLICIES:
        replay = run(corpus, build(name), upstreams, config, arrival_rate=40)
        shares = "".join(
            f"{sum(1 for r in replay.records if r.outcome == outcome) / len(replay):>10.2%} "
            for outcome in OUTCOMES
        )
        print(
            f"{name:<10}{shares}"
            f"{replay.quantile_us(0.99) / 1000:>9.1f} ms"
            f"{replay.peak_queue_depth:>13}"
        )

    rule("what to take from this")
    print(
        "Two things, and neither is visible one request at a time.\n"
        "\n"
        "`best` is the same policy in both tables. Served one at a time it\n"
        "answers almost everything; at 40 requests a second against 24 slots it\n"
        "collapses, and it collapses on the deadline rather than on errors. The\n"
        "flagship is thirty times slower, so the same arrival rate needs thirty\n"
        "times the concurrency, and what does not fit queues until it expires. A\n"
        "dashboard watching error rates would show nothing wrong at all.\n"
        "\n"
        "`cascade` answers nearly everything at every load, and answered is not\n"
        "correct: compare those two columns in the middle table. A gateway that\n"
        "reported success from response validity would call that healthy while\n"
        "it was wrong about a fifth of the time.\n"
        "\n"
        "Both are measured in reports/evaluation.md and stated on the front page\n"
        "of README.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
