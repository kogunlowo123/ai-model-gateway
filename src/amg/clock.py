"""A virtual clock, and the discrete-event scheduler that turns it.

Every latency in this project is *simulated*, and every measurement about
timing -- retry amplification, the circuit breaker's crossover, what hedging
buys and what it costs -- is computed on this clock rather than on the wall.

**Why not `asyncio` and real sleeps.** Those measurements are concurrency
results: they depend on which request reached a saturated pool first, on whether
a breaker had already opened when the next attempt arrived, on whether a hedge
fired before its primary returned. Run that on a real event loop and the answer
depends on the host's scheduler, the suite takes minutes instead of milliseconds
to gather any statistical power, and -- worst -- the replay stops being
reproducible, which makes the refusal in :mod:`amg.replay` unreachable by
construction. A gateway that cannot replay its own decisions cannot report
regret against a counterfactual policy.

Discrete-event simulation is a named technique with a fifty-year literature, and
it is the honest choice here rather than a shortcut: nothing about a retry
policy's behaviour under load requires waiting in real time to observe.

**Time is an integer count of microseconds.** Never a float. Two events at
"the same time" must order deterministically, and float times introduce
comparisons whose result depends on how the values were computed. Microseconds
give sub-millisecond resolution over a simulated century in an int that never
loses precision.

**Ties break on insertion order.** The heap is keyed on ``(time, priority,
sequence)`` with a monotonically increasing sequence, so the schedule is a total
order and two runs of the same scenario produce the same interleaving. Without
the sequence, Python's heap would compare the payloads, which are not orderable
and would raise -- or worse, would order on something incidental.

The API is deliberately close to SimPy's, because that shape is familiar and
this is a few hundred lines rather than a dependency::

    def caller(sim: Simulation) -> ProcessGenerator:
        yield sim.timeout(1_500)          # 1.5 ms of simulated latency
        with (yield from pool.hold(sim)): # a concurrency slot
            yield sim.timeout(20_000)

    sim = Simulation()
    sim.start(caller(sim))
    sim.run()
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self

from amg.errors import RefusalError

#: Microseconds in a millisecond and in a second, so callers never write a bare
#: 1_000_000 and leave the reader counting zeros.
US_PER_MS: Final[int] = 1_000
US_PER_SECOND: Final[int] = 1_000_000

#: The default ceiling on how many events one run may process. A resilience
#: policy with a bug -- a retry loop with no cap, a breaker that reopens
#: immediately -- produces an unbounded event stream, and an unbounded loop in a
#: measurement harness looks exactly like a slow machine.
MAX_EVENTS: Final[int] = 5_000_000


class Event:
    """Something a process can wait for.

    An event is either pending, or triggered with a value. Waiting on an event
    that has already been triggered resumes the waiter immediately at the
    current simulated time, which is what makes a completed future safe to
    ``yield`` without a special case at every call site.
    """

    __slots__ = ("_callbacks", "triggered", "value")

    def __init__(self) -> None:
        self._callbacks: list[Callable[[Event], None]] = []
        self.triggered = False
        self.value: Any = None

    def then(self, callback: Callable[[Event], None]) -> None:
        """Run *callback* when this event fires, or now if it already has."""
        if self.triggered:
            callback(self)
        else:
            self._callbacks.append(callback)

    def succeed(self, value: Any = None) -> None:
        """Fire the event, resuming everything waiting on it."""
        if self.triggered:
            raise RuntimeError("an event cannot be triggered twice")
        self.triggered = True
        self.value = value
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)


#: What a process generator yields: an event to wait for.
ProcessGenerator = Generator[Event, Any, Any]


@dataclass(order=True, slots=True)
class _Scheduled:
    """One entry in the event heap.

    ``order=True`` over these three fields in this order is the total order the
    determinism of the whole project rests on. *sequence* is the tie-breaker
    and is never equal between two entries, so ``event`` is never compared.
    """

    at: int
    priority: int
    sequence: int
    event: Event = field(compare=False)


class Simulation:
    """The clock and its event queue.

    ``now`` is an integer count of microseconds since the start of the run and
    moves only when the queue is drained -- so a process that computes for a
    simulated hour takes no real time, and one that does nothing takes none
    either.
    """

    def __init__(self, *, max_events: int = MAX_EVENTS) -> None:
        self.now: int = 0
        self._queue: list[_Scheduled] = []
        self._sequence: int = 0
        self._processed: int = 0
        self._max_events = max_events

    @property
    def processed(self) -> int:
        """How many events this run has handled, for the runaway check."""
        return self._processed

    def _push(self, event: Event, *, delay: int, priority: int) -> None:
        if delay < 0:
            raise ValueError("cannot schedule an event in the past")
        self._sequence += 1
        heapq.heappush(self._queue, _Scheduled(self.now + delay, priority, self._sequence, event))

    def timeout(self, delay: int, *, value: Any = None) -> Event:
        """An event that fires *delay* microseconds from now."""
        event = Event()
        event.value = value
        self._push(event, delay=delay, priority=1)
        return event

    def event(self) -> Event:
        """A bare event some other process will trigger."""
        return Event()

    def start(self, generator: ProcessGenerator) -> Event:
        """Run *generator* as a process; the returned event fires when it ends.

        The generator is stepped immediately rather than on the next tick, so a
        process that does no waiting completes within the call. That keeps the
        simulated arrival time of a request equal to the time it was started,
        rather than one scheduler tick later for reasons a reader would have to
        reconstruct.
        """
        done = Event()
        self._step(generator, done, None)
        return done

    def _step(self, generator: ProcessGenerator, done: Event, sent: Any) -> None:
        try:
            waiting = generator.send(sent)
        except StopIteration as stop:
            done.succeed(stop.value)
            return
        waiting.then(lambda fired: self._step(generator, done, fired.value))

    def run(self, *, until: int | None = None) -> None:
        """Drain the queue, or run to *until* microseconds.

        Raises:
            RefusalError: if the run exceeds ``max_events``. A resilience policy
                that never settles produces an unbounded event stream, and a
                harness that hangs on it reaches an operator as "the gate is
                flaky" rather than as the bug it is.
        """
        while self._queue:
            if until is not None and self._queue[0].at > until:
                self.now = until
                return
            entry = heapq.heappop(self._queue)
            self.now = entry.at
            self._processed += 1
            if self._processed > self._max_events:
                raise RefusalError(
                    f"the simulation processed more than {self._max_events} events "
                    f"and was stopped at {self.now} us",
                    remedy=(
                        "A policy that never settles -- an uncapped retry loop, or a "
                        "breaker that reopens immediately -- produces an unbounded "
                        "event stream. Check the retry cap and the breaker's cooldown."
                    ),
                )
            entry.event.succeed(entry.event.value)
        if until is not None:
            self.now = until


def first_of(sim: Simulation, events: Sequence[Event]) -> Generator[Event, Any, int]:
    """Wait for whichever of *events* fires first; yields its index.

    Needed because a request waiting for a concurrency slot must also be able to
    give up when its deadline passes. Without it a queued request only discovers
    it is too late when it finally reaches the front, so end-to-end latency is
    bounded by the queue rather than by the deadline -- measured here at a p99 of
    9.0 seconds against a 6.0 second budget, which is exactly the "unbounded
    latency instead of errors" failure the deadline exists to prevent.

    The winner is decided by the scheduler's total order, so two events at the
    same simulated instant resolve the same way on every run.
    """
    gate = sim.event()
    resolved: list[int] = []

    def settle(index: int) -> Callable[[Event], None]:
        def callback(fired: Event) -> None:  # noqa: ARG001 - the callback
            # signature is fixed by `then`; which event fired is carried by the
            # closed-over index, not by the argument.
            if not resolved:
                resolved.append(index)
                if not gate.triggered:
                    gate.succeed(index)

        return callback

    for index, event in enumerate(events):
        event.then(settle(index))
    yield gate
    return resolved[0]


@dataclass(slots=True)
class _Waiter:
    """One process queued for a slot, and whether it is still waiting.

    A waiter that has given up must not be handed a slot: releasing into it
    would leak the slot permanently, and a leaked slot in a bounded pool
    deadlocks the run at some later point that looks nothing like the cause.
    """

    event: Event
    abandoned: bool = False


class _Slot:
    """A held resource slot, released on exit.

    A context manager rather than an explicit release call, because the failure
    mode of the explicit version -- an exception between acquire and release --
    leaks a slot, and a leaked slot in a concurrency limiter deadlocks the run
    at some later point that looks nothing like the cause.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._pool.release()


class Pool:
    """A counting semaphore with a FIFO queue: the gateway's concurrency limit.

    This is the component that makes retry amplification observable. Retries add
    load, load fills the pool, a full pool makes everything queue, queueing
    makes requests time out, and timeouts produce more retries. Without a
    bounded pool the simulation has infinite capacity and the collapse cannot
    happen -- which is exactly why a gateway measured without one looks fine.

    FIFO rather than LIFO: both are defensible under load shedding, and FIFO is
    what almost every real limiter does, so it is what the measurement should
    describe.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("a pool needs at least one slot")
        self.capacity = capacity
        self.in_use = 0
        self._waiting: deque[_Waiter] = deque()
        self.peak_queue_depth = 0
        self.abandoned = 0

    @property
    def queue_depth(self) -> int:
        """How many processes are currently waiting for a slot."""
        return len(self._waiting)

    def hold(self, sim: Simulation) -> Generator[Event, Any, _Slot]:
        """Acquire a slot, waiting as long as it takes.

        Used as ``with (yield from pool.hold(sim)):``. The double indirection is
        the price of expressing a blocking acquire in a generator-based process
        without a dependency; it is confined to this method and its sibling.
        """
        slot = yield from self.hold_until(sim, None)
        if slot is None:  # pragma: no cover - unreachable without a deadline
            raise RuntimeError("an unbounded acquire cannot time out")
        return slot

    def hold_until(
        self, sim: Simulation, deadline_us: int | None
    ) -> Generator[Event, Any, _Slot | None]:
        """Acquire a slot, or give up after *deadline_us* microseconds.

        Returns None when the wait was abandoned, so a caller can distinguish
        "the upstream failed" from "we never got as far as calling it" -- two
        conditions with very different remedies.
        """
        if self.in_use < self.capacity:
            self.in_use += 1
            return _Slot(self)
        if deadline_us is not None and deadline_us <= 0:
            self.abandoned += 1
            return None

        waiter = _Waiter(sim.event())
        self._waiting.append(waiter)
        self.peak_queue_depth = max(self.peak_queue_depth, len(self._waiting))

        if deadline_us is None:
            yield waiter.event
            return _Slot(self)

        winner = yield from first_of(sim, [waiter.event, sim.timeout(deadline_us)])
        if winner == 0:
            # The releaser handed the slot straight over rather than
            # decrementing, so `in_use` is already correct.
            return _Slot(self)
        waiter.abandoned = True
        self.abandoned += 1
        return None

    def release(self) -> None:
        """Give a slot back, handing it directly to the longest live waiter.

        Abandoned waiters are discarded rather than handed the slot: handing a
        slot to a process that has already given up loses it for the rest of the
        run, and a pool that quietly shrinks deadlocks later in a way that looks
        nothing like its cause.
        """
        while self._waiting:
            # Hand over rather than free-then-reacquire: freeing first lets a
            # process arriving in the same instant jump the queue, which turns a
            # FIFO limiter unfair only under load -- the exact condition the
            # measurement is about.
            waiter = self._waiting.popleft()
            if not waiter.abandoned:
                waiter.event.succeed()
                return
        if self.in_use == 0:
            raise RuntimeError("released a slot that was never held")
        self.in_use -= 1
