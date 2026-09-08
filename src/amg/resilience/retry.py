"""Retry timing, and the concurrency budget retries are spent out of.

Two small pieces that the gateway needs and that are worth isolating because
both are easy to get subtly wrong.

**Backoff is exponential with full jitter**, in the sense of the AWS
architecture note: the delay is drawn uniformly from ``[0, base * 2**attempt]``
rather than being set to it. Fixed backoff synchronises every client that failed
at the same moment and reproduces the original surge one interval later;
capping without jittering does the same thing more slowly. Full jitter spreads
the retries across the whole window, which is the cheapest available fix for a
correlated retry storm.

**The jitter is deterministic.** It is drawn from a BLAKE2b digest over the task
identity and the attempt number, not from ``random``. A gateway whose retry
schedule depends on process-global RNG state cannot be replayed, and replay is
what this project reports regret from. It is still *uniform* -- what it is not
is unpredictable, which nothing here needs it to be.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from amg.errors import ConfigError

#: One retry after the first attempt. Two total calls is the setting most
#: gateways ship, and the resilience sweep varies it rather than assuming it.
DEFAULT_MAX_ATTEMPTS: Final[int] = 2

#: The first backoff window. Small relative to the simulated model latencies,
#: because a backoff longer than the call it is retrying converts a retry into
#: a timeout from the caller's point of view.
DEFAULT_BACKOFF_BASE_US: Final[int] = 50_000

#: Never wait longer than this between attempts, however many have failed.
DEFAULT_BACKOFF_CEILING_US: Final[int] = 2_000_000

#: How long the gateway waits for one upstream call before giving up on it.
#: Above the slowest model's ordinary latency and below its tail branch, so a
#: timeout means "this one is pathological" rather than "this one is the
#: flagship".
DEFAULT_TIMEOUT_US: Final[int] = 4_000_000

#: The whole-request budget, covering **queueing as well as calling**.
#:
#: This is separate from `timeout_us` and the distinction turned out to matter.
#: A per-attempt timeout starts when the call leaves the queue, so it cannot see
#: queueing delay at all: measured here, a gateway with only a per-attempt
#: timeout and a saturated pool grew its queue to over a thousand requests
#: without its answered rate moving, because every request eventually got a slot
#: and eventually succeeded -- ten seconds later. Unbounded latency instead of
#: errors is the classic shape of a queue nobody bounded, and it is invisible to
#: any success-rate dashboard.
#:
#: A real caller does not wait forever. The deadline models that: once it has
#: passed, the gateway stops, whether the request is queued or in flight. It is
#: also what closes the retry-amplification loop -- queueing becomes failure,
#: failure becomes retries, retries become queueing.
DEFAULT_DEADLINE_US: Final[int] = 6_000_000

#: How many calls the gateway will have in flight at once. **This is the
#: parameter that makes retry amplification visible.** With unbounded
#: concurrency a retry costs money and nothing else; with a bounded pool it
#: costs a slot that a fresh request now has to queue for, which is the
#: mechanism behind every retry-driven collapse in production.
DEFAULT_CONCURRENCY: Final[int] = 24


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """When to try again, and how long to wait first."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_us: int = DEFAULT_BACKOFF_BASE_US
    backoff_ceiling_us: int = DEFAULT_BACKOFF_CEILING_US
    timeout_us: int = DEFAULT_TIMEOUT_US
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be at least one: zero calls is not a policy")
        if self.backoff_base_us < 0 or self.backoff_ceiling_us < 0:
            raise ConfigError("backoff times cannot be negative")
        if self.timeout_us < 1:
            raise ConfigError("a timeout of zero fails every call before it starts")

    def backoff_us(self, key: str, attempt: int) -> int:
        """How long to wait before *attempt*, counting the first attempt as 0.

        Returns 0 for the first attempt: a gateway that sleeps before its first
        call has added latency to every request in exchange for nothing.
        """
        if attempt <= 0:
            return 0
        window = min(self.backoff_base_us * (1 << (attempt - 1)), self.backoff_ceiling_us)
        if not self.jitter:
            return window
        digest = hashlib.blake2b(f"{key}\x1fbackoff\x1f{attempt}".encode(), digest_size=8).digest()
        # Full jitter: uniform over the whole window, in integer microseconds.
        return int.from_bytes(digest, "big") % (window + 1)
