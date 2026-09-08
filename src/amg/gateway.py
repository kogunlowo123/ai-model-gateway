"""The gateway: one request, from a routing decision to an answer or a failure.

**This module is the single routing core.** ``amg serve`` runs it and ``amg
replay`` runs it, over the same policies and the same upstream protocol, because
a regret table computed by a path that had drifted from the served one would
describe a gateway nobody is running. ``tests/integration/test_routing_core.py``
pins that: it drives both entry points over the whole corpus and asserts the
decisions are identical.

The execution model is a generator over the virtual clock in :mod:`amg.clock`,
so a run of ten thousand requests through a saturated pool takes milliseconds
and produces the same interleaving every time. See that module for why real
sleeps were rejected.

**What happens to one request**

1. The policy returns a ladder of upstreams. It saw only the prompt text.
2. For each rung, the gateway calls that upstream, retrying transport failures
   with jittered backoff, subject to a circuit breaker and a concurrency pool.
3. If the answer comes back and **parses**, the request is done.
4. If it comes back malformed and a rung remains, the gateway escalates. This is
   the only escalation trigger available at request time; correctness is not
   knowable here, which is exactly why a cascade cannot recover a confidently
   wrong answer.
5. If every rung is exhausted, the request failed, and that is recorded rather
   than raised -- a failure rate is a measurement, and a gateway that throws on
   the last retry cannot report one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from amg.clock import Pool, ProcessGenerator, Simulation
from amg.errors import ConfigError
from amg.resilience.breaker import (
    DEFAULT_COOLDOWN_US,
    DEFAULT_FAILURE_THRESHOLD,
    Breakers,
)
from amg.resilience.retry import (
    DEFAULT_CONCURRENCY,
    DEFAULT_DEADLINE_US,
    RetryPolicy,
)
from amg.routing.policies import Policy
from amg.upstream.base import Attempt, Outcome, Upstream
from amg.workload.tasks import Task, parse_answer

#: How a request ended, from the caller's point of view.
ANSWERED: Final[str] = "answered"
MALFORMED: Final[str] = "malformed"
FAILED: Final[str] = "failed"
#: The request ran out of its end-to-end budget, queued or in flight. Named
#: apart from FAILED because the two want different responses: an upstream that
#: is failing needs a different provider, and a gateway that is timing out needs
#: less load or more capacity.
EXPIRED: Final[str] = "expired"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Everything about how the gateway behaves that is not the routing policy.

    Separated from the policy on purpose: the routing experiment holds this
    fixed and varies the policy, and the resilience experiment holds the policy
    fixed and varies this. Two knobs in one object would let a change to one
    quietly move the other's results.
    """

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    concurrency: int = DEFAULT_CONCURRENCY
    #: The whole-request budget, covering queue time. See
    #: :data:`amg.resilience.retry.DEFAULT_DEADLINE_US` for why a per-attempt
    #: timeout alone leaves a saturated gateway with unbounded latency rather
    #: than a visible failure rate.
    deadline_us: int = DEFAULT_DEADLINE_US
    breaker_enabled: bool = True
    breaker_threshold: int = DEFAULT_FAILURE_THRESHOLD
    breaker_cooldown_us: int = DEFAULT_COOLDOWN_US

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ConfigError("the gateway needs at least one concurrency slot")
        if self.deadline_us < self.retry.timeout_us:
            raise ConfigError(
                f"the end-to-end deadline ({self.deadline_us} us) is below the "
                f"per-attempt timeout ({self.retry.timeout_us} us)",
                remedy=(
                    "The deadline covers queueing plus the call, so a deadline under "
                    "the call timeout can never allow a single complete attempt."
                ),
            )

    def breakers(self) -> Breakers:
        """A fresh set of breakers. Never shared between runs."""
        return Breakers(
            failure_threshold=self.breaker_threshold,
            cooldown_us=self.breaker_cooldown_us,
            enabled=self.breaker_enabled,
        )


@dataclass(frozen=True, slots=True)
class Served:
    """What the gateway did with one request.

    Deliberately does **not** carry correctness. The gateway cannot know it, and
    a field here would be readable by a policy through a future refactor -- the
    single easiest way to build a router that scores brilliantly and cannot be
    deployed. :mod:`amg.evaluate` joins this against ground truth afterwards.
    """

    task_id: str
    policy: str
    outcome: str
    upstream: str | None
    response: str | None
    reason: str
    attempts: tuple[Attempt, ...]
    latency_us: int
    escalated: bool
    rejected_by_breaker: int

    @property
    def cost_micro_cents(self) -> int:
        """Everything this request cost, including calls that returned nothing.

        A timed-out or failed call was still served upstream and still billed.
        Counting only the successful attempt would make every retry policy look
        free, which is the opposite of the finding.
        """
        return sum(attempt.cost_micro_cents for attempt in self.attempts)

    @property
    def answered(self) -> bool:
        """Did the caller get a well-formed answer?"""
        return self.outcome == ANSWERED


def _call(  # noqa: PLR0913, PLR0917 - a call needs its whole context; a
    # parameter object here would be constructed per attempt and read once.
    sim: Simulation,
    task: Task,
    upstream: Upstream,
    config: GatewayConfig,
    breakers: Breakers,
    pool: Pool,
    nonce_base: int,
    collected: list[Attempt],
    started: int,
) -> ProcessGenerator:
    """Call one upstream, retrying transport failures. Yields the winning attempt.

    Returns the successful :class:`Attempt`, or ``None`` if every permitted
    attempt failed. A breaker rejection ends the loop immediately rather than
    retrying: the breaker's whole claim is that this upstream is not answering,
    and retrying into an open breaker is a busy-wait with extra steps.
    """
    rejected = 0
    for index in range(config.retry.max_attempts):
        if sim.now - started >= config.deadline_us:
            return None, rejected
        delay = config.retry.backoff_us(f"{task.task_id}:{upstream.name}", index)
        if delay:
            yield sim.timeout(delay)

        if not breakers.allows(upstream.name, sim.now):
            rejected += 1
            break

        # The wait for a slot is bounded by the same deadline as the call. A
        # queued request that only discovers it is too late once it reaches the
        # front makes end-to-end latency a property of the queue rather than of
        # the budget, which is the failure this deadline exists to prevent.
        slot = yield from pool.hold_until(sim, config.deadline_us - (sim.now - started))
        if slot is None:
            # Permission was granted and the call was never made. If that
            # permission was a half-open probe it is exclusive, and not handing
            # it back wedges the breaker permanently -- see
            # `CircuitBreaker.release_probe`.
            breakers.release_probe(upstream.name)
            return None, rejected
        with slot:
            # The nonce makes a retry a genuinely different draw. Without it the
            # retry is bit-identical to the attempt that just failed, every
            # retry fails too, and the measured value of retrying is exactly
            # zero -- an artefact that would read as a finding.
            attempt = upstream.attempt(task, nonce=nonce_base + index, at_us=sim.now)
            # Whichever budget runs out first ends the call.
            remaining = config.deadline_us - (sim.now - started)
            allowed = min(config.retry.timeout_us, max(0, remaining))
            timed_out = attempt.latency_us > allowed
            waited = min(attempt.latency_us, allowed)
            yield sim.timeout(waited)

        if timed_out:
            collected.append(
                Attempt(
                    upstream=attempt.upstream,
                    outcome=Outcome.TIMEOUT,
                    response=None,
                    latency_us=waited,
                    input_tokens=attempt.input_tokens,
                    output_tokens=0,
                    # Billed anyway: the upstream did the work, the gateway just
                    # stopped listening.
                    cost_micro_cents=attempt.cost_micro_cents,
                )
            )
            breakers.record_failure(upstream.name, sim.now)
            continue

        collected.append(attempt)
        if attempt.ok:
            breakers.record_success(upstream.name, sim.now)
            return attempt, rejected
        breakers.record_failure(upstream.name, sim.now)

    return None, rejected


def serve(  # noqa: PLR0913, PLR0917 - see _call above.
    sim: Simulation,
    task: Task,
    policy: Policy,
    upstreams: dict[str, Upstream],
    config: GatewayConfig,
    breakers: Breakers,
    pool: Pool,
) -> ProcessGenerator:
    """Handle one request end to end. Yields a :class:`Served`."""
    started = sim.now
    decision = policy.decide(task.prompt)
    collected: list[Attempt] = []
    rejected = 0
    winner: Attempt | None = None
    escalated = False

    for rung, name in enumerate(decision.ladder):
        if sim.now - started >= config.deadline_us:
            break
        upstream = upstreams.get(name)
        if upstream is None:
            raise ConfigError(
                f"policy {policy.name!r} routed to unknown upstream {name!r}",
                remedy=f"Known upstreams: {', '.join(sorted(upstreams))}.",
            )
        if rung:
            escalated = True
        attempt, breaker_rejections = yield from _call(
            sim, task, upstream, config, breakers, pool, rung * 100, collected, started
        )
        rejected += breaker_rejections
        if attempt is None:
            # Transport failure exhausted this rung. Climbing is the right move
            # when a rung remains: a different provider is the most useful thing
            # to try when one has stopped answering.
            continue
        winner = attempt
        if parse_answer(attempt.response or "") is not None:
            break
        # Well-formed-looking but unparseable: escalate if we can, otherwise
        # hand back what we have and let the caller see it.

    if winner is None:
        outcome = EXPIRED if sim.now - started >= config.deadline_us else FAILED
    elif parse_answer(winner.response or "") is not None:
        outcome = ANSWERED
    else:
        outcome = MALFORMED

    return Served(
        task_id=task.task_id,
        policy=policy.name,
        outcome=outcome,
        upstream=winner.upstream if winner else None,
        response=winner.response if winner else None,
        reason=decision.reason,
        attempts=tuple(collected),
        latency_us=sim.now - started,
        escalated=escalated,
        rejected_by_breaker=rejected,
    )


def serve_one(
    task: Task,
    policy: Policy,
    upstreams: dict[str, Upstream],
    config: GatewayConfig | None = None,
) -> Served:
    """Serve a single request in its own simulation. The synchronous entry point.

    Used by the HTTP surface and by anything that wants one answer rather than a
    throughput experiment. A single request in an empty simulation never queues,
    so this reports the latency the request would see on an idle gateway --
    which is what a caller means by "how long does this take" and is *not* what
    the loaded experiments measure. The distinction is stated in
    ``docs/resilience.md`` rather than left for a reader to trip over.
    """
    settings = config or GatewayConfig()
    sim = Simulation()
    pool = Pool(settings.concurrency)
    done = sim.start(serve(sim, task, policy, upstreams, settings, settings.breakers(), pool))
    sim.run()
    if not done.triggered:
        raise ConfigError("the gateway simulation did not complete")
    served: Served = done.value
    return served
