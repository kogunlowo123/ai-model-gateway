"""A deterministic model provider. It is a simulator, and it says so.

**Read this before reading any number this project publishes.** Both halves of
the experiment are synthetic: the workload is generated, and so is the model
that answers it. What is being measured is therefore the **routing arithmetic**
-- how much a fitted router's advantage shrinks on held-out traffic, how far a
retry policy amplifies load, what a self-validating cascade can and cannot
recover -- and not the quality of any real model. Every accuracy figure in this
repository is a property of the table below. ``docs/simulator.md`` records how
far that table sits from a real local model answering the same tasks, measured
through :mod:`amg.upstream.ollama` -- including the one place the comparison
argues against this table rather than for it.

Determinism is the whole point, so every draw here is **integer arithmetic over
a BLAKE2b digest**. Nothing calls ``exp`` or ``log``; nothing compares two
floats.

That is not fastidiousness. libm's transcendental functions differ by one unit
in the last place between platforms -- a previous project in this series
measured exactly that, one value in 32,768 -- and this simulation makes ordering
decisions on latency and threshold decisions on accuracy. A single flipped
comparison changes which upstream won a race, which changes the whole event
schedule after it, and the replay gate in :mod:`amg.replay` would then fail on
somebody else's machine for a reason nobody could act on.

So:

* **accuracy is an integer table in parts per ten thousand**, indexed by
  difficulty, compared against an integer draw. A reader can see the skill curve
  rather than infer it from a sigmoid's parameters.
* **latency is integer microseconds**, drawn as the mean of three uniform
  integers -- a discrete bell -- with an integer-probability heavy tail branch.
  The tail is what hedging exists for, so it is modelled explicitly rather than
  left to a distribution's shoulder.
* **Python's `hash` is never used.** It is salted per process, so a corpus
  scored in one interpreter and replayed in another would take different
  branches and produce different numbers, with nothing raising.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Final

from amg.money import cost_of, dollars_per_million_to_price
from amg.upstream.base import Attempt, Outcome, estimate_tokens
from amg.workload.tasks import MAX_DIFFICULTY, MIN_DIFFICULTY, Task, render

#: Accuracy and probability resolution: parts per ten thousand. Integer
#: comparison against an integer draw, so a threshold decision can never depend
#: on a float's last bit.
SCALE: Final[int] = 10_000

#: How many of the 64 digest bits each draw consumes. Four independent draws per
#: attempt -- answer quality, malformation, transport failure, latency -- taken
#: from separate slices so that changing one model parameter does not shift the
#: others' streams.
_BITS: Final[int] = 16
_MASK: Final[int] = (1 << _BITS) - 1


def _digest(*parts: str | int) -> int:
    """A 64-bit integer from BLAKE2b over the parts, joined unambiguously.

    The separator matters: without it ``("ab", "c")`` and ``("a", "bc")`` hash
    identically, and two different (task, upstream) pairs would share a verdict.
    """
    payload = "\x1f".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _draw(digest: int, slot: int) -> int:
    """One draw in [0, SCALE) from bit-slice *slot* of *digest*."""
    return ((digest >> (slot * _BITS)) & _MASK) * SCALE // (_MASK + 1)


@dataclass(frozen=True, slots=True)
class SimulatedUpstream:
    """A model provider whose behaviour is a pure function of its inputs.

    Attributes:
        name: Identifier used everywhere downstream.
        summary: One line for `amg models`.
        accuracy: Probability of a **correct** answer at difficulty 1 through 5,
            in parts per ten thousand. This is the skill curve, written out.
        malformed_share: Of the answers that are *wrong*, the share that are
            also malformed -- missing the JSON envelope the prompt asked for.
            This is the only part of being wrong that a gateway can detect at
            request time, and therefore the ceiling on what a cascade can
            recover. A low value is the pessimistic case and the realistic one:
            a confident model returns well-formed nonsense.
        failure_rate: Probability of an **independent** transport failure, in
            parts per ten thousand. Each call draws separately, which models a
            flaky provider rather than a broken one.
        outage_period_us: If non-zero, the upstream is completely unavailable
            for ``outage_length_us`` out of every ``outage_period_us``. This is
            a **correlated** failure, and it is a different thing entirely:
            a consecutive-failure circuit breaker is nearly useless against
            elevated independent errors -- it trips by chance and sheds traffic
            that would have succeeded -- and is the only thing that helps
            against an outage. ``docs/resilience.md`` measures both.
        outage_length_us: How long each outage lasts.
        latency_base_us: The floor.
        latency_span_us: Width of the body of the distribution.
        tail_permille: Probability of the slow branch, per thousand.
        tail_multiplier: How much slower the slow branch is.
    """

    name: str
    summary: str
    accuracy: tuple[int, ...]
    malformed_share: int
    failure_rate: int
    outage_period_us: int
    outage_length_us: int
    latency_base_us: int
    latency_span_us: int
    tail_permille: int
    tail_multiplier: int
    input_price_per_1k: int
    output_price_per_1k: int

    def __post_init__(self) -> None:
        expected = MAX_DIFFICULTY - MIN_DIFFICULTY + 1
        if len(self.accuracy) != expected:
            raise ValueError(f"accuracy needs one entry per difficulty ({expected})")
        if not all(0 <= value <= SCALE for value in self.accuracy):
            raise ValueError("accuracy entries are parts per ten thousand")

    def accuracy_at(self, difficulty: int) -> int:
        """The skill curve at *difficulty*, in parts per ten thousand."""
        return self.accuracy[difficulty - MIN_DIFFICULTY]

    def latency_for(self, task: Task, *, nonce: int = 0) -> int:
        """Latency in whole microseconds, drawn from three uniforms and a tail.

        The mean of three uniform draws is a discrete approximation to a bell,
        which is realistic enough for the body of a latency distribution and is
        exact integer arithmetic. The tail branch is separate and explicit
        because tail latency is the thing hedging is bought to fix, and burying
        it in a distribution's shoulder would make the hedging measurement a
        statement about a parameter nobody can see.
        """
        digest = _digest(self.name, "latency", task.task_id, nonce)
        span = max(1, self.latency_span_us)
        body = (
            self.latency_base_us
            + (
                (digest & _MASK) * span // (_MASK + 1)
                + ((digest >> 16) & _MASK) * span // (_MASK + 1)
                + ((digest >> 32) & _MASK) * span // (_MASK + 1)
            )
            // 3
        )
        if _draw(_digest(self.name, "tail", task.task_id, nonce), 0) * 1_000 // SCALE < (
            self.tail_permille
        ):
            return body * self.tail_multiplier
        return body

    def is_out(self, at_us: int) -> bool:
        """Is the upstream inside a correlated outage window at *at_us*?

        Periodic rather than randomly placed, so that a sweep over outage
        duration compares like with like: two runs at different duty cycles
        differ in how long the upstream is down and in nothing else.
        """
        if self.outage_period_us <= 0 or self.outage_length_us <= 0:
            return False
        return at_us % self.outage_period_us < self.outage_length_us

    def attempt(self, task: Task, *, nonce: int = 0, at_us: int = 0) -> Attempt:
        """Answer *task*, or fail trying. Pure function of its arguments."""
        latency = self.latency_for(task, nonce=nonce)
        input_tokens = estimate_tokens(task.prompt)
        digest = _digest(self.name, "verdict", task.task_id, nonce)

        if self.is_out(at_us) or _draw(digest, 2) < self.failure_rate:
            # A transport failure still costs the input tokens on most
            # providers, and pretending otherwise would understate what a retry
            # policy spends -- which is the number the retry experiment is for.
            return Attempt(
                upstream=self.name,
                outcome=Outcome.ERROR,
                response=None,
                latency_us=latency,
                input_tokens=input_tokens,
                output_tokens=0,
                cost_micro_cents=cost_of(input_tokens, self.input_price_per_1k),
            )

        if _draw(digest, 0) < self.accuracy_at(task.difficulty):
            body = render(task.answer)
        elif _draw(digest, 1) < self.malformed_share:
            body = _malformed(task, digest)
        else:
            body = render(_wrong_answer(task, digest))

        output_tokens = estimate_tokens(body)
        return Attempt(
            upstream=self.name,
            outcome=Outcome.OK,
            response=body,
            latency_us=latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micro_cents=(
                cost_of(input_tokens, self.input_price_per_1k)
                + cost_of(output_tokens, self.output_price_per_1k)
            ),
        )


def _wrong_answer(task: Task, digest: int) -> str:
    """A plausible wrong answer: right shape, wrong value.

    Perturbing the true answer rather than emitting something arbitrary, because
    a wrong answer that does not even look like an answer would be caught by any
    downstream sanity check and the "confidently wrong" case -- the one that
    matters -- would never be exercised.
    """
    if task.answer.lstrip("-").isdigit():
        offset = (_draw(digest, 3) % 9) + 1
        return str(int(task.answer) + offset)
    if not task.answer:
        return "unknown"
    index = _draw(digest, 3) % len(task.answer)
    characters = list(task.answer)
    characters[index] = characters[index - 1]
    return "".join(characters)


def _malformed(task: Task, digest: int) -> str:
    """A response the gateway can reject without knowing the right answer.

    Three shapes, because a cascade that only handles one of them would look
    better than it is: prose instead of JSON, JSON without the key, and a
    truncated object.
    """
    shape = _draw(digest, 3) % 3
    if shape == 0:
        return f"The answer is {task.answer}."
    if shape == 1:
        return '{"result": "' + task.answer + '"}'
    return '{"answer": "' + task.answer


def _price(dollars_in: str, dollars_out: str) -> tuple[int, int]:
    return (
        dollars_per_million_to_price(dollars_in),
        dollars_per_million_to_price(dollars_out),
    )


_NANO_IN, _NANO_OUT = _price("0.10", "0.40")
_MINI_IN, _MINI_OUT = _price("0.60", "2.40")
_FLAGSHIP_IN, _FLAGSHIP_OUT = _price("3.00", "12.00")

#: The three tiers the experiments route between. Prices are the shape real
#: providers charge -- roughly 6x and 30x from the cheapest -- rather than any
#: particular vendor's list, and the accuracy curves are the simulator's, not a
#: measurement of anything. They overlap deliberately: a cheap model that was
#: strictly worse everywhere would make routing trivial and the benchmark
#: vacuous.
CATALOGUE: Final[tuple[SimulatedUpstream, ...]] = (
    SimulatedUpstream(
        name="nano",
        summary="Cheapest and fastest; falls off sharply past difficulty 3",
        accuracy=(9_700, 9_200, 7_600, 4_100, 1_900),
        malformed_share=2_600,
        failure_rate=40,
        outage_period_us=0,
        outage_length_us=0,
        latency_base_us=120_000,
        latency_span_us=240_000,
        tail_permille=25,
        tail_multiplier=7,
        input_price_per_1k=_NANO_IN,
        output_price_per_1k=_NANO_OUT,
    ),
    SimulatedUpstream(
        name="mini",
        summary="Six times the price of nano; holds up into the hard band",
        accuracy=(9_900, 9_750, 9_300, 7_800, 5_400),
        malformed_share=1_500,
        failure_rate=30,
        outage_period_us=0,
        outage_length_us=0,
        latency_base_us=280_000,
        latency_span_us=520_000,
        tail_permille=20,
        tail_multiplier=6,
        input_price_per_1k=_MINI_IN,
        output_price_per_1k=_MINI_OUT,
    ),
    SimulatedUpstream(
        name="flagship",
        summary="Thirty times the price of nano, and slowest; best on hard tasks",
        accuracy=(9_960, 9_920, 9_800, 9_300, 8_400),
        malformed_share=800,
        failure_rate=25,
        outage_period_us=0,
        outage_length_us=0,
        latency_base_us=700_000,
        latency_span_us=1_400_000,
        tail_permille=18,
        tail_multiplier=5,
        input_price_per_1k=_FLAGSHIP_IN,
        output_price_per_1k=_FLAGSHIP_OUT,
    ),
)

BY_NAME: Final[dict[str, SimulatedUpstream]] = {upstream.name: upstream for upstream in CATALOGUE}

#: Cheapest first. Several policies need this order and deriving it at each call
#: site invites two of them to disagree about ties.
BY_PRICE: Final[tuple[str, ...]] = tuple(
    upstream.name
    for upstream in sorted(CATALOGUE, key=lambda item: (item.output_price_per_1k, item.name))
)


def catalogue(
    *,
    failure_rate: int | None = None,
    outage_period_us: int | None = None,
    outage_length_us: int | None = None,
) -> tuple[SimulatedUpstream, ...]:
    """The shipped catalogue, optionally with every failure rate overridden.

    The override is how the resilience experiments sweep upstream reliability
    without a second copy of the catalogue drifting away from this one. Note it
    replaces rather than scales: the sweep asks "what happens at 20% failure",
    and a scale factor would make the answer depend on three different starting
    points that the reader would have to look up.
    """
    if failure_rate is not None and not 0 <= failure_rate <= SCALE:
        raise ValueError("a failure rate is in parts per ten thousand")
    if failure_rate is None and outage_period_us is None and outage_length_us is None:
        return CATALOGUE
    return tuple(
        replace(
            upstream,
            failure_rate=upstream.failure_rate if failure_rate is None else failure_rate,
            outage_period_us=(
                upstream.outage_period_us if outage_period_us is None else outage_period_us
            ),
            outage_length_us=(
                upstream.outage_length_us if outage_length_us is None else outage_length_us
            ),
        )
        for upstream in CATALOGUE
    )
