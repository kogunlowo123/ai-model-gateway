"""What a model provider looks like to this gateway.

Kept deliberately small: a name, a price, and a way to answer a task. Everything
the routing and resilience layers do is expressed against this protocol, so the
simulated upstream and the Ollama-backed one are interchangeable and the
experiment can say honestly which one produced a number.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from amg.workload.tasks import Task


class Outcome(enum.StrEnum):
    """How an attempt ended, from the gateway's point of view.

    ``StrEnum`` so a JSON report carries ``"ok"`` rather than
    ``"Outcome.OK"`` -- a detail that is invisible until somebody tries to
    group by it in a query six months later.
    """

    #: The upstream answered. Whether the answer is *right* is a separate
    #: question the gateway cannot ask at request time.
    OK = "ok"
    #: The upstream failed to answer: a 5xx, a refused connection, a reset.
    #: Detectable, retryable, and injected on purpose by the simulator.
    ERROR = "error"
    #: The gateway gave up waiting. Named separately from ERROR because the two
    #: differ in the one way that matters to a cost report: a timed-out call was
    #: still served and still billed, so the money left even though no answer
    #: arrived. Folding it into ERROR would understate what a retry policy
    #: spends, which is the quantity the resilience sweep exists to measure.
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class Attempt:
    """One call to one upstream, and everything it cost.

    Note there is no ``correct`` field. Correctness is not knowable at request
    time and this record is what the *gateway* saw; the evaluation joins it
    against ground truth afterwards. Putting correctness here would let a
    routing policy read it by accident, which is the single easiest way to
    write a router that looks brilliant and cannot be deployed.
    """

    upstream: str
    outcome: Outcome
    response: str | None
    latency_us: int
    input_tokens: int
    output_tokens: int
    cost_micro_cents: int

    @property
    def ok(self) -> bool:
        """Did the upstream answer at all?"""
        return self.outcome is Outcome.OK


@runtime_checkable
class Upstream(Protocol):
    """A model provider the gateway can route to."""

    @property
    def name(self) -> str:
        """Stable identifier, used in reports, baselines and audit records."""
        ...

    @property
    def input_price_per_1k(self) -> int:
        """Micro-cents per 1,000 input tokens. An integer; see :mod:`amg.money`."""
        ...

    @property
    def output_price_per_1k(self) -> int:
        """Micro-cents per 1,000 output tokens."""
        ...

    def attempt(self, task: Task, *, nonce: int = 0, at_us: int = 0) -> Attempt:
        """Answer *task*.

        Args:
            task: What to answer.
            nonce: Distinguishes retries of the same task against the same
                upstream. Without it a retry is bit-identical to the attempt
                that just failed, every retry fails too, and the measured value
                of retrying is exactly zero -- a result that would look like a
                finding and be an artefact of the simulator.
            at_us: The simulated instant of the call. Only a correlated outage
                depends on it; independent failures do not. It exists because a
                circuit breaker defends against an upstream being *down*, not
                against one being *flaky*, and a simulator with no notion of
                time cannot tell those apart -- which would make every breaker
                measurement a measurement of the wrong thing.
        """
        ...


def estimate_tokens(text: str) -> int:
    """A deterministic token estimate: four characters to a token.

    Crude on purpose, and the crudeness is stated rather than hidden. A real
    tokeniser would make this figure look authoritative while still being wrong
    for any provider using a different vocabulary, and every cost in this
    project is a *relative* comparison between policies over identical traffic,
    where a constant factor cancels.

    What must not vary is the estimate itself: it is integer arithmetic over the
    character count, so it is identical on every platform and every run.
    """
    return -(-len(text) // 4)
