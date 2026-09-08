"""The resilience sweep: what retries and breakers are worth, and when.

Three questions, each with a measured answer that is not the obvious one.

**1. Does retrying help?** Yes, and less as load rises. Retries are a positive
feedback loop: a failure produces another call, another call takes a slot from a
bounded pool, a fuller pool makes fresh requests queue, and a request that
queues past its deadline is another failure. The loop is invisible to any test
that sends one request at a time, which is why every arm here runs in **loaded**
mode.

**2. Does a circuit breaker help against a high error rate?** Measured: no. A
consecutive-failure breaker is a detector for an upstream being *down*, not for
one being *flaky*. Against elevated **independent** errors it trips by
coincidence and sheds traffic that would have succeeded. Configuring a breaker
against an error-rate SLO is a category error, and this sweep is what shows it.

**3. Does a breaker help against an outage?** **Only if the fallback has
capacity**, and that turns out to be the whole story. The breaker's job is to
stop calling a dead provider and start calling a live one -- which moves the
entire load onto the live one. When the fallback is slower, the same request
rate needs proportionally more concurrency, and a gateway sized for the fast
path collapses on its deadline rather than on errors: the failure looks like
latency, not like an outage, and the dashboards that would have caught it are
watching error rates.

Both sides of that are measured here, at two concurrency settings, which is why
the sweep has a capacity axis at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from amg.evaluate.metrics import Interval, wilson
from amg.gateway import EXPIRED, GatewayConfig
from amg.replay import DEFAULT_ARRIVAL_RATE, run
from amg.resilience.retry import DEFAULT_CONCURRENCY
from amg.routing.policies import Policy
from amg.upstream.simulated import CATALOGUE, SCALE, catalogue
from amg.workload.corpus import Corpus

#: Injected **independent** failure rates for the flakiness sweep, in parts per
#: ten thousand.
DEFAULT_FAILURE_RATES: Final[tuple[int, ...]] = (0, 1_000, 2_000, 4_000, 6_000)

#: The outage cycle. One provider is unavailable for a share of every period.
#: Twenty seconds is long relative to the 2-second breaker cooldown, so the
#: cooldown is a cost the sweep can see rather than a rounding error.
OUTAGE_PERIOD_US: Final[int] = 20_000_000

#: Outage duty cycles, as a share of the period, in parts per ten thousand.
#: 10_000 is a provider that never comes back, which is the case the breaker is
#: actually for.
DEFAULT_DUTY_CYCLES: Final[tuple[int, ...]] = (0, 1_000, 2_500, 5_000, 10_000)

#: The two capacity settings. The first is the default pool, sized for the fast
#: provider; the second is sized so the slow one can absorb the whole load. The
#: gap between them is the finding.
UNDER_PROVISIONED: Final[int] = DEFAULT_CONCURRENCY
PROVISIONED: Final[int] = 4 * DEFAULT_CONCURRENCY

#: Which provider goes down. The cheapest, because that is where a cost-routed
#: gateway sends most of its traffic and therefore where an outage hurts.
OUTAGE_TARGET: Final[str] = CATALOGUE[0].name


@dataclass(frozen=True, slots=True)
class Arm:
    """One configuration of the resilience stack, named for the report."""

    name: str
    max_attempts: int
    breaker: bool

    def configure(self, base: GatewayConfig) -> GatewayConfig:
        """This arm's settings, derived from a shared base.

        Derived rather than constructed, so everything the sweep is *not*
        varying -- timeouts, deadlines, backoff -- is provably identical across
        arms. Two arms differing in a parameter nobody meant to change is the
        standard way a sweep produces a finding about itself.
        """
        return replace(
            base,
            retry=replace(base.retry, max_attempts=self.max_attempts),
            breaker_enabled=self.breaker,
        )


ARMS: Final[tuple[Arm, ...]] = (
    Arm(name="single", max_attempts=1, breaker=False),
    Arm(name="retry", max_attempts=2, breaker=False),
    Arm(name="retry+breaker", max_attempts=2, breaker=True),
)


@dataclass(frozen=True, slots=True)
class Measurement:
    """One arm, at one injected fault level, at one capacity."""

    arm: str
    fault: str
    level: int
    concurrency: int
    answered: Interval
    expired: int
    calls_per_request: float
    cost_total: int
    p50_latency_us: int
    p99_latency_us: int
    breaker_rejections: int
    peak_queue_depth: int

    @property
    def level_share(self) -> float:
        """The injected level as a proportion, for an axis label."""
        return self.level / SCALE


@dataclass(frozen=True, slots=True)
class Sweep:
    """Every arm at every level, and the questions worth asking of it."""

    policy: str
    arrival_rate: int
    measurements: tuple[Measurement, ...]

    def select(self, *, fault: str, arm: str, concurrency: int) -> tuple[Measurement, ...]:
        """One curve, in ascending level order."""
        return tuple(
            sorted(
                (
                    m
                    for m in self.measurements
                    if m.fault == fault and m.arm == arm and m.concurrency == concurrency
                ),
                key=lambda m: m.level,
            )
        )

    def advantage(self, *, fault: str, level: int, concurrency: int, over: str, arm: str) -> float:
        """*arm* minus *over*, in answered-rate points, at one point of the sweep."""
        found = {
            m.arm: m
            for m in self.measurements
            if m.fault == fault and m.level == level and m.concurrency == concurrency
        }
        if arm not in found or over not in found:
            return 0.0
        return found[arm].answered.point - found[over].answered.point

    def breaker_needs_capacity(self) -> tuple[float, float]:
        """The breaker's advantage under a total outage, at both capacities.

        The headline pair. Under-provisioned, the breaker fails everything over
        to a slower provider that cannot absorb it and the gateway dies on its
        deadline; provisioned, the same breaker is the difference between
        serving almost everything and serving almost nothing.
        """
        return (
            self.advantage(
                fault="outage",
                level=SCALE,
                concurrency=UNDER_PROVISIONED,
                over="retry",
                arm="retry+breaker",
            ),
            self.advantage(
                fault="outage",
                level=SCALE,
                concurrency=PROVISIONED,
                over="retry",
                arm="retry+breaker",
            ),
        )

    def under_total_outage(self, arm: str) -> tuple[float, float]:
        """*arm*'s answered rate under a total outage, at both capacities.

        The pair that carries the finding. The breaker's *advantage* is larger
        when the gateway is under-provisioned, which reads as an argument for
        breakers until the absolute numbers are put next to it: the same
        breaker serves a fifth of the traffic on a pool sized for the fast
        provider and almost all of it on one sized for the slow one. Capacity is
        worth several times what the breaker is worth, and no breaker setting
        recovers a pool that cannot absorb the failover.
        """
        found = {
            m.concurrency: m
            for m in self.measurements
            if m.fault == "outage" and m.level == SCALE and m.arm == arm
        }
        return (
            found[UNDER_PROVISIONED].answered.point if UNDER_PROVISIONED in found else 0.0,
            found[PROVISIONED].answered.point if PROVISIONED in found else 0.0,
        )

    def breaker_harm_during_partial_outage(self) -> float:
        """Worst points the breaker *loses* at a partial outage, under-provisioned.

        Negative-signed findings are the ones worth surfacing: a breaker fails
        traffic over to a provider that cannot absorb it, and during a partial
        outage that is strictly worse than waiting for the flaky one.
        """
        return min(
            (
                self.advantage(
                    fault="outage",
                    level=level,
                    concurrency=UNDER_PROVISIONED,
                    over="retry",
                    arm="retry+breaker",
                )
                for level in (1_000, 2_500, 5_000)
            ),
            default=0.0,
        )


def _measure(  # noqa: PLR0913, PLR0917 - every argument is an axis of the
    # sweep or a label for it; bundling them hides what is being varied.
    corpus: Corpus,
    policy: Policy,
    upstreams: dict[str, object],
    arm: Arm,
    base: GatewayConfig,
    arrival_rate: int,
    *,
    fault: str,
    level: int,
) -> Measurement:
    replay = run(
        corpus,
        policy,
        dict(upstreams),  # type: ignore[arg-type]
        arm.configure(base),
        arrival_rate=arrival_rate,
    )
    total = len(replay)
    return Measurement(
        arm=arm.name,
        fault=fault,
        level=level,
        concurrency=base.concurrency,
        answered=wilson(replay.answered, total),
        expired=sum(1 for record in replay.records if record.outcome == EXPIRED),
        calls_per_request=replay.calls / total if total else 0.0,
        cost_total=replay.total_cost,
        p50_latency_us=replay.quantile_us(0.50),
        p99_latency_us=replay.quantile_us(0.99),
        breaker_rejections=replay.breaker_rejections,
        peak_queue_depth=replay.peak_queue_depth,
    )


def sweep(  # noqa: PLR0913 - see _measure.
    corpus: Corpus,
    policy: Policy,
    *,
    failure_rates: tuple[int, ...] = DEFAULT_FAILURE_RATES,
    duty_cycles: tuple[int, ...] = DEFAULT_DUTY_CYCLES,
    concurrencies: tuple[int, ...] = (UNDER_PROVISIONED, PROVISIONED),
    arrival_rate: int = DEFAULT_ARRIVAL_RATE,
) -> Sweep:
    """Run both fault models, at every level, arm and capacity.

    Two fault models rather than one, because they are the two things people
    mean by "the upstream is failing" and a breaker is useful against exactly
    one of them:

    * ``flaky`` -- every call fails independently with some probability;
    * ``outage`` -- one provider is completely unavailable for a share of every
      cycle, and the others are fine.
    """
    measurements: list[Measurement] = []

    for concurrency in concurrencies:
        base = GatewayConfig(concurrency=concurrency)

        for rate in failure_rates:
            upstreams = {up.name: up for up in catalogue(failure_rate=rate)}
            measurements.extend(
                _measure(
                    corpus,
                    policy,
                    dict(upstreams),
                    arm,
                    base,
                    arrival_rate,
                    fault="flaky",
                    level=rate,
                )
                for arm in ARMS
            )

        for duty in duty_cycles:
            length = OUTAGE_PERIOD_US * duty // SCALE
            upstreams = {
                up.name: (
                    replace(up, outage_period_us=OUTAGE_PERIOD_US, outage_length_us=length)
                    if up.name == OUTAGE_TARGET
                    else up
                )
                for up in CATALOGUE
            }
            measurements.extend(
                _measure(
                    corpus,
                    policy,
                    dict(upstreams),
                    arm,
                    base,
                    arrival_rate,
                    fault="outage",
                    level=duty,
                )
                for arm in ARMS
            )

    return Sweep(policy=policy.name, arrival_rate=arrival_rate, measurements=tuple(measurements))
