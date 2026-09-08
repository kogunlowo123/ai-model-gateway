"""A circuit breaker, and the reason a gateway needs one to survive its own retries.

Retries are the obvious answer to an unreliable upstream and they are a positive
feedback loop. Each failure produces another call, another call occupies another
concurrency slot, occupied slots make everything else queue, queueing turns into
timeouts, and timeouts are failures. Below some upstream failure rate the loop
damps out and retrying is free quality; above it the loop runs away and the
gateway spends its entire capacity on requests that were never going to succeed.

That transition is sharp, it is a property of the *system* rather than of the
upstream, and it is invisible to any test that sends one request at a time.
:mod:`amg.evaluate.resilience` sweeps the upstream failure rate with the breaker
on and off and reports where the two curves separate.

The breaker is the damping term. When an upstream has failed
``failure_threshold`` times in a row it is taken out of service for
``cooldown_us``; after that a single probe is allowed through, and the outcome
of that one call decides whether the upstream comes back or the cooldown starts
again.

**Consecutive failures rather than a rolling error rate.** A rolling window is
better behaved under mixed traffic and needs a window length, a bucket count and
a minimum-sample rule before it means anything -- three more parameters, each of
which would need calibrating and none of which changes the shape of the result
this project reports. The simpler rule is stated, not defended as optimal.

**Half-open admits exactly one probe.** Admitting several is how a breaker
turns into a thundering herd against an upstream that has not recovered, which
converts a partial outage into a total one at the moment of recovery.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Final

from amg.errors import ConfigError

#: Defaults chosen to be defensible, not optimal, and stated so a reader can
#: disagree with a number rather than with a mystery. Five consecutive failures
#: is roughly "this is not a blip" at any realistic per-call failure rate; two
#: seconds is long enough for a restarting process and short enough that a
#: recovered upstream is not stranded.
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
DEFAULT_COOLDOWN_US: Final[int] = 2_000_000


class State(enum.StrEnum):
    """Where a breaker is. ``StrEnum`` so a report carries ``"open"``."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """One breaker, for one upstream.

    Mutable by necessity -- it is the gateway's memory of how an upstream has
    been behaving -- and therefore the one place in the routing path that is not
    a pure function. It is keyed per upstream and never shared between
    simulation runs, so two runs of the same scenario still produce identical
    schedules.
    """

    name: str
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_us: int = DEFAULT_COOLDOWN_US
    consecutive_failures: int = 0
    opened_at: int | None = None
    probe_in_flight: bool = False
    #: Counters for the report. Rejections are the interesting one: they are
    #: requests the gateway refused to send, which is the breaker doing its job
    #: and also the cost of it being wrong.
    rejections: int = 0
    opens: int = 0
    _forced: State | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ConfigError("a breaker needs a threshold of at least one failure")
        if self.cooldown_us < 0:
            raise ConfigError("a cooldown cannot be negative")

    def state(self, now: int) -> State:
        """Where the breaker is *at this simulated instant*.

        Computed from the clock rather than updated by a timer: a timer would be
        another event in the schedule, and an event whose only job is to change
        a value that could have been derived is an event that can interleave
        differently and make a run non-reproducible.
        """
        if self.opened_at is None:
            return State.CLOSED
        if now - self.opened_at >= self.cooldown_us:
            return State.HALF_OPEN
        return State.OPEN

    def allows(self, now: int) -> bool:
        """May a call go out right now?

        Counts a rejection as a side effect, which is why this is not a
        property. The count is what the report uses to say how much traffic the
        breaker shed.
        """
        current = self.state(now)
        if current is State.CLOSED:
            return True
        if current is State.OPEN:
            self.rejections += 1
            return False
        if self.probe_in_flight:
            self.rejections += 1
            return False
        self.probe_in_flight = True
        return True

    def record_success(self, now: int) -> None:  # noqa: ARG002 - see below
        """A call came back. Close the breaker and forget the history.

        Takes *now* it does not use, so that it and :meth:`record_failure` have
        the same signature. The gateway calls one or the other from the same
        place, and a caller that has to remember which one takes the clock is a
        caller that will eventually pass it to the wrong one.
        """
        self.consecutive_failures = 0
        self.opened_at = None
        self.probe_in_flight = False

    def release_probe(self) -> None:
        """Abandon a half-open probe without recording an outcome.

        The gateway asks permission before it calls, and permission for a
        half-open probe is exclusive -- exactly one call may go out. If that
        call is then never made, because the request ran out of its deadline
        while queued, nothing reports back and ``probe_in_flight`` stays set
        **for the rest of the run**. Every subsequent call to that upstream is
        rejected by a breaker that is waiting for a probe nobody is going to
        send.

        That is a permanent wedge produced by a transient condition, it only
        appears under load, and it was found by the resilience sweep rather than
        by any unit test of the breaker itself. This method is how the gateway
        hands the permission back.
        """
        self.probe_in_flight = False

    def record_failure(self, now: int) -> None:
        """A call failed. Open the breaker if that was one too many.

        A failure while half-open reopens immediately regardless of the counter:
        the probe existed precisely to answer "has it recovered", and the answer
        was no.
        """
        if self.probe_in_flight:
            self.probe_in_flight = False
            self.opened_at = now
            self.opens += 1
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = now
            self.opens += 1
            self.consecutive_failures = 0


@dataclass(slots=True)
class Breakers:
    """A breaker per upstream, created on first use.

    A disabled set is not an empty set: :meth:`allows` must still answer, and
    ``enabled=False`` makes it always answer yes. That is what lets the
    resilience sweep run the same code path with and without the breaker, so the
    two curves differ by the breaker and not by which branch of the gateway ran.
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_us: int = DEFAULT_COOLDOWN_US
    enabled: bool = True
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def get(self, name: str) -> CircuitBreaker:
        """The breaker for *name*, created on first use."""
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(
                name=name,
                failure_threshold=self.failure_threshold,
                cooldown_us=self.cooldown_us,
            )
            self._breakers[name] = breaker
        return breaker

    def allows(self, name: str, now: int) -> bool:
        """May a call to *name* go out? Always yes when breaking is disabled."""
        if not self.enabled:
            return True
        return self.get(name).allows(now)

    def record_success(self, name: str, now: int) -> None:
        """Tell *name*'s breaker a call came back."""
        if self.enabled:
            self.get(name).record_success(now)

    def release_probe(self, name: str) -> None:
        """Hand back permission for a probe the gateway never sent."""
        if self.enabled:
            self.get(name).release_probe()

    def record_failure(self, name: str, now: int) -> None:
        """Tell *name*'s breaker a call failed."""
        if self.enabled:
            self.get(name).record_failure(now)

    @property
    def rejections(self) -> int:
        """Total calls the breakers refused to make."""
        return sum(breaker.rejections for breaker in self._breakers.values())

    @property
    def opens(self) -> int:
        """How many times any breaker opened."""
        return sum(breaker.opens for breaker in self._breakers.values())
