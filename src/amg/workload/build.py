"""Generating a workload from a plan, reproducibly and disjointly.

A plan is everything needed to rebuild a corpus byte for byte: a seed, a size, a
difficulty mix and a family list. ``amg check`` re-derives a committed corpus
from its plan and compares digests, so a workload edited by hand fails the build
rather than silently changing every number downstream.

**`hard_share` is the workload-shift knob**, and it is the reason this module
exists in the shape it does. The routing experiment fits a difficulty estimator
on one workload and measures it on others; the interesting question is what
happens when the traffic mix moves away from the one it was fitted to. That
requires generating corpora that differ *only* in the difficulty mix, from the
same grammar, with no overlap.

**Disjointness is by construction, not by filtering.** ``generate(plan,
exclude=...)`` refuses to emit a prompt already in the exclusion set and draws
again. Filtering afterwards would be easier and wrong: it removes samples
unevenly across families and difficulties -- most from whichever bucket collides
most -- and the resulting corpus no longer has the mix it claims.

**One seeded generator per bucket, not one per run.** With a single stream,
adding a family or nudging one weight shifts every draw after it and the whole
corpus changes, so a diff shows everything and means nothing. Per-bucket streams
mean a change to the ``date_offset`` builder changes date tasks and nothing
else. The seeds are strings, so two buckets never get correlated streams the way
adjacent integer seeds can.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Final

from amg.errors import ConfigError
from amg.workload import tasks as task_module
from amg.workload.corpus import Corpus, build_corpus
from amg.workload.tasks import MAX_DIFFICULTY, MIN_DIFFICULTY, Task, make_task

#: Difficulty 4 and 5 are "hard"; 1 to 3 are "easy". The split is where the
#: simulated cheap model's accuracy falls off, which is what makes routing a
#: real decision rather than an arbitrary one.
HARD_LEVELS: Final[tuple[int, ...]] = (4, 5)
EASY_LEVELS: Final[tuple[int, ...]] = (1, 2, 3)

#: How many times a bucket may redraw a duplicate before the plan is declared
#: too small for its grammar. A corpus quietly short of its requested size is a
#: corpus whose difficulty mix is not what the plan says.
MAX_REDRAWS: Final[int] = 64


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything needed to regenerate a workload byte for byte."""

    name: str
    seed: int
    size: int
    hard_share: float
    families: tuple[str, ...] = field(default=task_module.FAMILY_NAMES)

    def __post_init__(self) -> None:
        if self.size < len(self.families):
            raise ConfigError(f"size must be at least {len(self.families)}, one task per family")
        if not 0.0 <= self.hard_share <= 1.0:
            raise ConfigError("hard_share is a proportion and must lie in [0, 1]")
        unknown = set(self.families) - set(task_module.FAMILY_NAMES)
        if unknown:
            raise ConfigError(f"unknown task families: {', '.join(sorted(unknown))}")
        if not self.families:
            raise ConfigError("a plan needs at least one family")


@dataclass(frozen=True, slots=True)
class Generation:
    """A generated workload and what happened while generating it."""

    corpus: Corpus
    requested: int
    collisions: int

    @property
    def collision_rate(self) -> float:
        """The share of draws that repeated a prompt already generated."""
        return self.collisions / self.requested if self.requested else 0.0

    def summary(self) -> str:
        """One line for a terminal."""
        return (
            f"{len(self.corpus)} tasks, {self.corpus.hard_share:.1%} hard, "
            f"{self.collision_rate:.2%} collisions"
        )


#: The shipped plans. `fit` trains the difficulty estimator; `measure` is the
#: same distribution and is the **control**, which reports how much evaluating a
#: router on its own fitting workload overstates it; the `shift-*` plans move
#: only the difficulty mix and are the treatment.
PLANS: Final[dict[str, Plan]] = {
    "fit": Plan(name="fit", seed=20260908, size=4_800, hard_share=0.30),
    "measure": Plan(name="measure", seed=771103, size=2_400, hard_share=0.30),
    "shift-10": Plan(name="shift-10", seed=310217, size=2_400, hard_share=0.10),
    "shift-50": Plan(name="shift-50", seed=515419, size=2_400, hard_share=0.50),
    "shift-70": Plan(name="shift-70", seed=707723, size=2_400, hard_share=0.70),
    "shift-90": Plan(name="shift-90", seed=909931, size=2_400, hard_share=0.90),
    "tiny": Plan(name="tiny", seed=5, size=60, hard_share=0.30),
}


def plan_named(name: str) -> Plan:
    """Look a plan up, listing the alternatives when it is not there."""
    try:
        return PLANS[name]
    except KeyError:
        raise ConfigError(
            f"unknown plan {name!r}",
            remedy=f"Known plans: {', '.join(sorted(PLANS))}.",
        ) from None


def _difficulty(rng: random.Random, hard_share: float) -> int:
    """Draw a difficulty with the plan's hard share.

    Uniform within each band. A more elaborate distribution would let the
    estimator learn the shape rather than the signal, and the point of the
    experiment is what the estimator does when the *shape* moves.
    """
    band = HARD_LEVELS if rng.random() < hard_share else EASY_LEVELS
    return band[rng.randrange(len(band))]


def generate(plan: Plan, *, exclude: frozenset[str] = frozenset()) -> Generation:
    """Build the workload *plan* describes, avoiding every prompt in *exclude*.

    Args:
        plan: What to build.
        exclude: Prompts that must not appear. Pass the training workload's
            prompts here and the result is disjoint **by construction**, which
            is the only way to get a holdout whose difficulty mix still matches
            its plan.

    Raises:
        ConfigError: if a bucket cannot find a fresh prompt within
            ``MAX_REDRAWS``, which means the grammar is exhausted at this size.
    """
    seen: set[str] = set(exclude)
    collected: list[Task] = []
    collisions = 0
    requested = 0

    families = [task_module.BY_NAME[name] for name in plan.families]
    per_family, remainder = divmod(plan.size, len(families))

    for index, family in enumerate(families):
        # One stream per bucket, seeded by name: editing one family's builder
        # changes that family's tasks and nothing else.
        rng = random.Random(f"{plan.seed}:{family.name}")  # noqa: S311  # nosec B311
        wanted = per_family + (1 if index < remainder else 0)
        for ordinal in range(wanted):
            requested += 1
            for attempt in range(MAX_REDRAWS):
                difficulty = _difficulty(rng, plan.hard_share)
                task_id = f"{plan.name}:{family.name}:{ordinal:05d}"
                candidate = make_task(family, difficulty, rng, task_id)
                if candidate.prompt not in seen:
                    seen.add(candidate.prompt)
                    collected.append(candidate)
                    break
                collisions += 1
                if attempt == MAX_REDRAWS - 1:
                    raise ConfigError(
                        f"family {family.name!r} could not find an unused prompt in "
                        f"{MAX_REDRAWS} draws at size {plan.size}",
                        remedy=(
                            "The grammar is exhausted at this size. Reduce `size`, or "
                            "widen the family's vocabulary in amg/workload/tasks.py."
                        ),
                    )

    return Generation(corpus=build_corpus(collected), requested=requested, collisions=collisions)


def difficulty_histogram(corpus: Corpus) -> str:
    """A one-line difficulty histogram, for `amg synth` and `amg doctor`."""
    counts = corpus.by_difficulty()
    total = max(1, len(corpus))
    return "  ".join(
        f"{level}:{counts[level] / total:.0%}"
        for level in range(MIN_DIFFICULTY, MAX_DIFFICULTY + 1)
    )
