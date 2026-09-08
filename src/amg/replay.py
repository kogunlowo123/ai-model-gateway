"""Running a whole workload through a policy, and proving the run reproduces.

Two modes, because the questions need different ones.

**Sequential** serves each request in its own simulation. No request ever waits
for another, so cost and correctness are measured without queueing confounding
them. This is the mode the routing experiment uses: the question there is "which
upstream did the policy pick and was that a good idea", and load would add
variance that has nothing to do with the answer.

**Loaded** puts every request into one simulation with a deterministic arrival
process and a bounded concurrency pool. Requests contend, retries take slots
from fresh traffic, and the breaker sees a shared history. This is the mode the
resilience experiment uses, and it is the only one in which retry amplification
can happen at all -- with unbounded capacity a retry costs money and nothing
else, which is precisely why a gateway measured one-request-at-a-time looks
fine right up until it does not.

**Replay is verified, not assumed.** :func:`verify_determinism` runs the same
scenario twice and compares digests over the decisions. Every counterfactual
number this project reports rests on the claim that a policy re-run on the same
traffic does the same thing; a claim that load-bearing gets checked on every
evaluation rather than trusted because the code looks pure.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from amg.clock import Pool, ProcessGenerator, Simulation
from amg.errors import RefusalError
from amg.gateway import ANSWERED, GatewayConfig, Served, serve
from amg.routing.policies import Policy
from amg.upstream.base import Upstream
from amg.workload.corpus import Corpus
from amg.workload.tasks import Task

#: Arrival rate for the loaded mode, in requests per second. Chosen so that the
#: default 24-slot pool sits comfortably below saturation at the catalogue's
#: ordinary latencies -- the sweep then has somewhere to collapse *to*, rather
#: than starting already broken.
DEFAULT_ARRIVAL_RATE: Final[int] = 40


@dataclass(frozen=True, slots=True)
class Replay:
    """Every request a policy served, and the aggregates worth naming."""

    policy: str
    records: tuple[Served, ...]
    breaker_rejections: int
    breaker_opens: int
    peak_queue_depth: int

    def __len__(self) -> int:
        return len(self.records)

    @property
    def total_cost(self) -> int:
        """Micro-cents, summed as integers. See :mod:`amg.money`."""
        return sum(record.cost_micro_cents for record in self.records)

    @property
    def answered(self) -> int:
        """Requests that came back with a well-formed answer."""
        return sum(1 for record in self.records if record.outcome == ANSWERED)

    @property
    def calls(self) -> int:
        """Upstream calls made, including retries and escalations."""
        return sum(len(record.attempts) for record in self.records)

    def latencies(self) -> list[int]:
        """End-to-end latencies, sorted, for quantiles."""
        return sorted(record.latency_us for record in self.records)

    def quantile_us(self, share: float) -> int:
        """The *share* quantile of end-to-end latency, by nearest rank.

        Nearest rank rather than interpolation: interpolating between two
        observed latencies invents a value nothing produced, and at the tail --
        which is the only place anyone reads this number -- the invented value
        sits in the gap the tail branch created.
        """
        ordered = self.latencies()
        if not ordered:
            return 0
        index = min(len(ordered) - 1, max(0, round(share * len(ordered)) - 1))
        return ordered[index]

    def digest(self) -> str:
        """A digest over the decisions, for the determinism check.

        Covers what the gateway *chose* and what it spent -- not the latency, so
        that a change to the arrival process shows up as a different schedule
        rather than as a spurious routing difference.
        """
        hasher = hashlib.sha256()
        for record in self.records:
            hasher.update(
                f"{record.task_id}\x1f{record.outcome}\x1f{record.upstream}"
                f"\x1f{record.cost_micro_cents}\x1f{len(record.attempts)}\n".encode()
            )
        return f"sha256:{hasher.hexdigest()}"


def _arrival_offsets(tasks: Sequence[Task], rate_per_second: int) -> list[int]:
    """Deterministic inter-arrival times, in microseconds since the run started.

    Exponential inter-arrivals would be the textbook choice and would need
    ``log``; this uses a uniform spread over twice the mean gap, which has the
    same mean, real jitter, and stays in integer arithmetic. What matters for
    the amplification result is that arrivals are not synchronised, not that
    they are Poisson -- and a distribution nobody can reproduce would be worse
    than one that is slightly the wrong shape.
    """
    mean_gap = max(1, 1_000_000 // max(1, rate_per_second))
    offsets: list[int] = []
    clock = 0
    for task in tasks:
        digest = hashlib.blake2b(f"{task.task_id}\x1farrival".encode(), digest_size=8).digest()
        clock += int.from_bytes(digest, "big") % (2 * mean_gap + 1)
        offsets.append(clock)
    return offsets


def run(
    corpus: Corpus,
    policy: Policy,
    upstreams: dict[str, Upstream],
    config: GatewayConfig | None = None,
    *,
    arrival_rate: int | None = None,
) -> Replay:
    """Serve every task in *corpus* under *policy*.

    Args:
        corpus: The workload to serve.
        policy: How to route each request.
        upstreams: The providers, by name.
        config: Retry, timeout, breaker and concurrency settings.
        arrival_rate: ``None`` for sequential mode -- each request in its own
            simulation, no queueing. An integer for loaded mode: requests arrive
            at that many per second into one shared pool, and contend.
    """
    settings = config or GatewayConfig()
    if arrival_rate is None:
        return _sequential(corpus, policy, upstreams, settings)
    return _loaded(corpus, policy, upstreams, settings, arrival_rate)


def _sequential(
    corpus: Corpus, policy: Policy, upstreams: dict[str, Upstream], config: GatewayConfig
) -> Replay:
    records: list[Served] = []
    breakers = config.breakers()
    peak = 0
    for task in corpus:
        sim = Simulation()
        pool = Pool(config.concurrency)
        done = sim.start(serve(sim, task, policy, upstreams, config, breakers, pool))
        sim.run()
        records.append(done.value)
        peak = max(peak, pool.peak_queue_depth)
    return Replay(
        policy=policy.name,
        records=tuple(records),
        breaker_rejections=breakers.rejections,
        breaker_opens=breakers.opens,
        peak_queue_depth=peak,
    )


def _loaded(
    corpus: Corpus,
    policy: Policy,
    upstreams: dict[str, Upstream],
    config: GatewayConfig,
    arrival_rate: int,
) -> Replay:
    sim = Simulation()
    pool = Pool(config.concurrency)
    breakers = config.breakers()
    collected: list[Served] = []
    tasks = list(corpus)

    def arrive(task: Task, offset: int) -> ProcessGenerator:
        yield sim.timeout(offset)
        served = yield from serve(sim, task, policy, upstreams, config, breakers, pool)
        collected.append(served)

    for task, offset in zip(tasks, _arrival_offsets(tasks, arrival_rate), strict=True):
        sim.start(arrive(task, offset))
    sim.run()

    if len(collected) != len(tasks):
        raise RefusalError(
            f"the simulation finished with {len(collected)} of {len(tasks)} requests served",
            remedy=(
                "Requests are still queued, which means a slot was leaked or the "
                "pool deadlocked. A partial run cannot be reported as a rate."
            ),
        )
    # Arrival order, not completion order, so the digest is stable regardless of
    # how the scheduler interleaved them.
    order = {task.task_id: index for index, task in enumerate(tasks)}
    collected.sort(key=lambda record: order[record.task_id])
    return Replay(
        policy=policy.name,
        records=tuple(collected),
        breaker_rejections=breakers.rejections,
        breaker_opens=breakers.opens,
        peak_queue_depth=pool.peak_queue_depth,
    )


def verify_determinism(
    corpus: Corpus,
    policy: Policy,
    upstreams: dict[str, Upstream],
    config: GatewayConfig | None = None,
    *,
    arrival_rate: int | None = None,
) -> str:
    """Run the same scenario twice and refuse if the two disagree.

    Every counterfactual figure this project publishes assumes a policy re-run
    on the same traffic does the same thing. That assumption is cheap to check
    and catastrophic to get wrong -- a float creeping into a threshold, a set
    iteration leaking into an ordering, a shared breaker surviving between runs
    -- so it is checked on every evaluation rather than trusted.

    Returns:
        The digest, so a caller can record what was verified.
    """
    first = run(corpus, policy, upstreams, config, arrival_rate=arrival_rate)
    second = run(corpus, policy, upstreams, config, arrival_rate=arrival_rate)
    if first.digest() != second.digest():
        raise RefusalError(
            f"policy {policy.name!r} did not reproduce: {first.digest()} then {second.digest()}",
            remedy=(
                "Something in the decision path is not a pure function of its inputs. "
                "Look for float comparisons, unseeded randomness, or state shared "
                "between runs."
            ),
        )
    return first.digest()
