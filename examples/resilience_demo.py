#!/usr/bin/env python
"""Retries, circuit breakers, and the capacity that decides whether they help.

This is the third finding, run small enough to watch:

    **A circuit breaker is a failover mechanism, and failover needs capacity.**

A breaker's job is to stop calling a provider that is down and start calling one
that is up. That moves the *entire* load onto the one that is up. When the
survivor is slower -- and the cheap fast model is exactly what an outage takes
away -- the same arrival rate now needs proportionally more concurrency. A pool
sized for the fast path does not have it, so the requests queue, and they fail
by running out of *time* rather than by erroring.

That failure is invisible to the monitoring most people would set up for this.
The error rate looks fine. The gateway is simply not answering.

The demo also shows the case where the breaker is actively harmful: a partial
outage on a small pool, where shedding traffic to a slower provider is worse
than waiting for the flaky one.

Run it::

    uv run python examples/resilience_demo.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amg.evaluate.resilience import PROVISIONED, UNDER_PROVISIONED, sweep
from amg.routing.policies import build
from amg.upstream.simulated import SCALE
from amg.workload.build import PLANS, generate

#: Small: this is an illustration of a shape the full sweep measures properly.
SIZE = 600

#: Outage depths to show, in parts per ten thousand of every cycle. The last is
#: a provider that is completely gone.
LEVELS = (2_500, 5_000, SCALE)


def main() -> int:
    corpus = generate(replace(PLANS["measure"], size=SIZE, name="resilience")).corpus
    print(f"{len(corpus)} requests at 40/second, cascade routing")
    print("one provider is out for a share of every cycle; the others are healthy\n")

    result = sweep(
        corpus,
        build("cascade"),
        failure_rates=(),
        duty_cycles=LEVELS,
        concurrencies=(UNDER_PROVISIONED, PROVISIONED),
    )

    for concurrency in (UNDER_PROVISIONED, PROVISIONED):
        label = "under-provisioned" if concurrency == UNDER_PROVISIONED else "provisioned"
        print(f"{label} -- {concurrency} concurrent slots")
        print(
            f"  {'outage':>8}{'single':>10}{'retry':>10}{'retry+breaker':>16}"
            f"{'breaker gains':>16}{'p99':>11}"
        )
        for level in LEVELS:
            row = {
                measurement.arm: measurement
                for measurement in result.measurements
                if measurement.fault == "outage"
                and measurement.level == level
                and measurement.concurrency == concurrency
            }
            if len(row) < 3:
                continue
            gain = row["retry+breaker"].answered.point - row["retry"].answered.point
            print(
                f"  {level / SCALE:>7.0%}"
                f"{row['single'].answered.point:>10.1%}"
                f"{row['retry'].answered.point:>10.1%}"
                f"{row['retry+breaker'].answered.point:>16.1%}"
                f"{gain * 100:>+15.1f}p"
                f"{row['retry+breaker'].p99_latency_us / 1000:>8.0f} ms"
            )
        print()

    under, provisioned = result.under_total_outage("retry+breaker")
    harm = result.breaker_harm_during_partial_outage()
    print(
        "The 100% rows are the finding. The same breaker, the same policy, the\n"
        "same traffic, and the only difference is how many slots the pool has:\n"
        f"  {under:.1%} answered under-provisioned\n"
        f"  {provisioned:.1%} answered provisioned\n"
        "\n"
        "Capacity is worth several times what the breaker is worth, and no\n"
        "breaker setting recovers a pool that cannot absorb the failover. Note\n"
        "the p99 column while reading that: under-provisioned, the requests are\n"
        "not erroring, they are running out of time.\n"
        "\n"
        "The partial-outage rows carry the other half. At its worst the breaker\n"
        f"loses {abs(harm) * 100:.1f} points against plain retries on the small pool --\n"
        "shedding traffic onto a slower provider is worse than waiting for a\n"
        "flaky one, so a breaker is not a free safety net to switch on.\n"
        "\n"
        "The full sweep adds a second fault model, independent per-call errors,\n"
        "where a consecutive-failure breaker helps even less: `python tasks.py\n"
        "resilience`. Its figures differ slightly from these -- it runs a larger\n"
        "corpus over more outage depths -- so quote those rather than these."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
