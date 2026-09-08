"""The task grammar: prompts with checkable answers and a known difficulty.

Quality in this project is **exact match against a ground truth**, not a rating
and not a judge. That is a deliberate narrowing. A routing experiment whose
quality signal is itself a model has two unknowns and can only report their
product; these six families have one right answer each, computed by the
generator, so correctness is a fact rather than an estimate.

Every task asks for its answer as JSON: ``{"answer": ...}``. That is what makes
the cascade policy honest. A gateway cannot consult the ground truth at request
time -- if it could, it would not need a model -- but it *can* check that the
response parses and carries the field it asked for. So a response can fail in
two distinguishable ways:

* **malformed** -- no JSON, or no ``answer`` key. Detectable at runtime, and
  therefore recoverable by escalating to a better model.
* **confidently wrong** -- well-formed JSON with the wrong value. Undetectable
  at runtime, and therefore *not* recoverable.

The ratio between those two is the ceiling on what any self-validating cascade
can buy, and :mod:`amg.evaluate` measures it rather than assuming it.

Difficulty is an integer 1-5 and is a property of the *task*, fixed by the
generator. It is never visible to the router, which sees only the prompt text --
see :mod:`amg.routing.features` for what a router is actually allowed to know.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

#: The difficulty scale. Five levels rather than a continuum because the
#: simulator's skill curve is stated per level in `docs/simulator.md`, and a
#: continuous difficulty would imply a precision the calibration does not have.
MIN_DIFFICULTY: Final[int] = 1
MAX_DIFFICULTY: Final[int] = 5

#: How the answer must come back. Stated in every prompt, checked by the
#: gateway at runtime, and the reason a cascade has anything to escalate on.
ANSWER_KEY: Final[str] = "answer"

_INSTRUCTION: Final[str] = f'Reply with JSON only, as {{"{ANSWER_KEY}": <value>}}.'

#: Above this difficulty, arithmetic may multiply as well as add. Multiplying
#: two five-digit numbers is a different task from adding them, and gating it
#: on difficulty is what makes the level mean something.
_MULTIPLY_FROM: Final[int] = 3

#: A fair coin, named because a bare 0.5 in a generator reads as a threshold
#: somebody tuned.
_EVEN_ODDS: Final[float] = 0.5

#: Vocabularies are sized so that every family can fill its share of the
#: largest shipped plan without repeating a prompt. That is not cosmetic: a
#: family that exhausts its grammar silently stops matching the difficulty mix
#: its plan asked for, and `generate` refuses rather than shipping a corpus
#: whose stated `hard_share` is a fiction. The smallest family here reaches
#: roughly ten thousand distinct prompts at difficulty 1, against a few hundred
#: draws.
_WORDS: Final[tuple[str, ...]] = (
    "reconciliation",
    "throughput",
    "dependency",
    "invoice",
    "scheduler",
    "migration",
    "checksum",
    "pipeline",
    "namespace",
    "quantile",
    "provisioning",
    "idempotent",
    "backpressure",
    "partition",
    "replica",
    "watermark",
    "compaction",
    "ingress",
    "manifest",
    "rollback",
    "sharding",
    "telemetry",
    "quorum",
    "eviction",
    "throttle",
    "handshake",
    "digest",
    "cursor",
    "batch",
    "lease",
    "snapshot",
    "traversal",
    "predicate",
    "coalesce",
    "sentinel",
    "backfill",
    "topology",
    "affinity",
    "retention",
    "checkpoint",
)

#: Each field carries its own value pool, so a record is a draw from the
#: product rather than from a fixed pairing. With a fixed pairing the whole
#: family had only a few hundred distinct records and exhausted immediately.
_RECORD_FIELDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "region",
        (
            "eu-west-1",
            "us-east-2",
            "ap-south-1",
            "sa-east-1",
            "eu-north-1",
            "us-west-1",
            "ca-central-1",
            "af-south-1",
        ),
    ),
    (
        "tier",
        ("standard", "premium", "trial", "enterprise", "internal", "sandbox", "legacy", "preview"),
    ),
    (
        "status",
        (
            "degraded",
            "healthy",
            "draining",
            "suspended",
            "provisioning",
            "retired",
            "migrating",
            "paused",
        ),
    ),
    (
        "owner",
        ("platform", "payments", "identity", "search", "billing", "growth", "infra", "support"),
    ),
    ("cluster", ("blue", "green", "amber", "slate", "cobalt", "rust", "ivory", "teal")),
    (
        "queue",
        ("primary", "overflow", "dead-letter", "priority", "bulk", "replay", "shadow", "canary"),
    ),
    ("channel", ("email", "webhook", "sms", "push", "batch", "stream", "poll", "socket")),
    (
        "plan",
        (
            "monthly",
            "annual",
            "metered",
            "reserved",
            "spot",
            "committed",
            "trialling",
            "grandfathered",
        ),
    ),
    ("shard", ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")),
    ("stage", ("build", "verify", "canary", "rollout", "soak", "promote", "freeze", "archive")),
)

_UNITS: Final[tuple[tuple[str, str, int], ...]] = (
    ("kilometres", "metres", 1000),
    ("hours", "seconds", 3600),
    ("gigabytes", "megabytes", 1024),
    ("weeks", "days", 7),
    ("kilograms", "grams", 1000),
    ("days", "minutes", 1440),
    ("terabytes", "gigabytes", 1024),
    ("dozens", "units", 12),
    ("minutes", "milliseconds", 60000),
    ("miles", "feet", 5280),
)


@dataclass(frozen=True, slots=True)
class Task:
    """One request with a known correct answer.

    Attributes:
        task_id: Stable identity. Every deterministic draw in this project --
            which upstream gets it right, how long it takes -- is keyed on this
            string, so the whole simulation is a pure function of the corpus.
        family: Which generator produced it.
        difficulty: 1-5. Known to the corpus and to the evaluation, and never
            to the router.
        prompt: What an upstream is asked.
        answer: The exact string an upstream must return under ``answer``.
    """

    task_id: str
    family: str
    difficulty: int
    prompt: str
    answer: str

    def is_correct(self, response: str) -> bool:
        """Did *response* carry the right answer, in the requested shape?

        Both failure modes collapse to False here on purpose; the caller
        distinguishes them with :func:`parses`, which is the part a gateway can
        do at runtime.
        """
        parsed = parse_answer(response)
        return parsed is not None and parsed == self.answer


def parse_answer(response: str) -> str | None:  # noqa: PLR0911 - one return per
    # JSON type is the readable form; collapsing them needs a dispatch table
    # that is longer than the branches it replaces.
    """Extract the answer field, or None if the response is malformed.

    **This is the only validation a gateway may do at request time**, and the
    cascade policy is built on exactly this function. It knows nothing about
    what the answer should be -- only whether the model produced the shape it
    was asked for.

    Numbers come back as JSON numbers rather than strings from a real model as
    often as not, so an int or float is accepted and normalised. A float that
    is exactly an integer renders without the trailing ``.0``, because
    ``{"answer": 47.0}`` and ``{"answer": 47}`` are the same answer and only a
    pedant's parser would disagree.
    """
    try:
        document = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or ANSWER_KEY not in document:
        return None
    value = document[ANSWER_KEY]
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value.strip()
    return None


def render(answer: str) -> str:
    """The well-formed response carrying *answer*. Used by every upstream."""
    return json.dumps({ANSWER_KEY: answer})


def _magnitude(rng: random.Random, difficulty: int) -> int:
    """An operand whose size grows with difficulty.

    Bounds overlap between adjacent levels on purpose. Disjoint ranges would
    let a router separate difficulty perfectly from the digit count alone, and
    a routing benchmark nobody can lose measures nothing -- the same failure a
    previous project in this series hit with a corpus whose classes had
    disjoint vocabularies.
    """
    low = 10**difficulty
    high = 10 ** (difficulty + 2)
    return rng.randrange(low, high)


def _arithmetic(rng: random.Random, difficulty: int) -> tuple[str, str]:
    left, right = _magnitude(rng, difficulty), _magnitude(rng, difficulty)
    operator = rng.choice(("+", "*") if difficulty >= _MULTIPLY_FROM else ("+", "-"))
    if operator == "+":
        result = left + right
    elif operator == "-":
        result = left - right
    else:
        result = left * right
    return f"What is {left} {operator} {right}? {_INSTRUCTION}", str(result)


def _string_reverse(rng: random.Random, difficulty: int) -> tuple[str, str]:
    words = [rng.choice(_WORDS) for _ in range(difficulty + 2)]
    text = " ".join(words)
    return f"Reverse this text character by character: {text!r}. {_INSTRUCTION}", text[::-1]


def _count_letters(rng: random.Random, difficulty: int) -> tuple[str, str]:
    words = [rng.choice(_WORDS) for _ in range(difficulty + 2)]
    text = " ".join(words)
    letter = rng.choice("aeinorst")
    return (
        f"How many times does the letter {letter!r} appear in {text!r}? {_INSTRUCTION}",
        str(text.count(letter)),
    )


def _field_extract(rng: random.Random, difficulty: int) -> tuple[str, str]:
    chosen = rng.sample(_RECORD_FIELDS, k=min(difficulty + 2, len(_RECORD_FIELDS)))
    document = {name: rng.choice(values) for name, values in chosen}
    wanted = rng.choice(list(document))
    body = json.dumps(document, sort_keys=True)
    return (
        f"From this record, what is the value of {wanted!r}? {body} {_INSTRUCTION}",
        document[wanted],
    )


def _date_offset(rng: random.Random, difficulty: int) -> tuple[str, str]:
    start = date(2024, 1, 1) + timedelta(days=rng.randrange(0, 900))
    offset = rng.randrange(1, 30 * difficulty + 1)
    ahead = rng.random() < _EVEN_ODDS
    end = start + timedelta(days=offset if ahead else -offset)
    direction = "after" if ahead else "before"
    return (
        (
            f"What date is {offset} days {direction} {start.isoformat()}? "
            f"Use YYYY-MM-DD. {_INSTRUCTION}"
        ),
        end.isoformat(),
    )


def _unit_convert(rng: random.Random, difficulty: int) -> tuple[str, str]:
    source, target, factor = rng.choice(_UNITS)
    amount = _magnitude(rng, difficulty)
    return (
        f"How many {target} are in {amount} {source}? {_INSTRUCTION}",
        str(amount * factor),
    )


@dataclass(frozen=True, slots=True)
class Family:
    """One task generator, named so a report can group by it."""

    name: str
    summary: str
    build: Callable[[random.Random, int], tuple[str, str]]


FAMILIES: Final[tuple[Family, ...]] = (
    Family("arithmetic", "Multi-digit addition, subtraction and multiplication", _arithmetic),
    Family("string_reverse", "Reverse a phrase character by character", _string_reverse),
    Family("count_letters", "Count occurrences of a letter in a phrase", _count_letters),
    Family("field_extract", "Read one field out of a JSON record", _field_extract),
    Family("date_offset", "Add or subtract days from a date", _date_offset),
    Family("unit_convert", "Multiply by a fixed conversion factor", _unit_convert),
)

FAMILY_NAMES: Final[tuple[str, ...]] = tuple(family.name for family in FAMILIES)

BY_NAME: Final[dict[str, Family]] = {family.name: family for family in FAMILIES}


def make_task(family: Family, difficulty: int, rng: random.Random, task_id: str) -> Task:
    """Build one task, and check the generator agrees with its own answer.

    The self-check is not defensive programming. Every quality number in this
    repository is exact-match against ``answer``, so a family whose stated
    answer does not satisfy its own parser would make one whole family
    unanswerable, and the result would read as a routing finding rather than as
    a corpus bug.
    """
    if not MIN_DIFFICULTY <= difficulty <= MAX_DIFFICULTY:
        raise ValueError(f"difficulty must lie in [{MIN_DIFFICULTY}, {MAX_DIFFICULTY}]")
    prompt, answer = family.build(rng, difficulty)
    task = Task(
        task_id=task_id,
        family=family.name,
        difficulty=difficulty,
        prompt=prompt,
        answer=answer,
    )
    if not task.is_correct(render(answer)):
        raise RuntimeError(f"family {family.name!r} does not agree with its own answer")
    return task
