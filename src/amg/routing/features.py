"""What a router is allowed to know about a request.

This module is a boundary, and the boundary is the point of it. A routing policy
may look at the prompt text and nothing else -- not the task's difficulty, not
its family, and certainly not its answer. All three exist on :class:`~amg.
workload.tasks.Task` because the *evaluation* needs them, and a router that
reached for them would score beautifully and be undeployable, because in
production none of them exist.

So the features here are exactly what a gateway can compute from an inbound
request in a few microseconds: counts of characters, words, digits and
punctuation. That is a deliberately weak signal. It is also the signal a real
cost-routing layer actually has, which is what makes the experiment about
routing rather than about clairvoyance.

**Every feature is an integer**, and the reason is the same one that governs
:mod:`amg.money` and :mod:`amg.upstream.simulated`: a routing decision is a
threshold comparison, and a threshold comparison on floats can resolve
differently on two machines whose libm differs in the last place. Integers make
the served decision bit-identical to the replayed one, which is what lets
:mod:`amg.replay` compare a policy against a counterfactual and mean it.
"""

from __future__ import annotations

import re
from typing import Final

#: The feature vector's layout. Order is part of the fitted artefact -- weights
#: are stored positionally -- so a test asserts this tuple against the length of
#: any estimator loaded from disk. Reordering these without refitting would
#: silently pair every weight with the wrong feature and produce a router that
#: still runs.
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "chars",
    "words",
    "digits",
    "longest_number",
    "distinct_words",
    "punctuation",
    "braces",
    "quotes",
)

#: Longer prompts are not proportionally harder, and an unbounded length feature
#: lets one outlier dominate a linear model. Counts are clipped rather than
#: logged: `log` is a libm call, and this module is integers by policy.
CLIP: Final[int] = 512

_NUMBER = re.compile(r"\d+")
_WORD = re.compile(r"[A-Za-z']+")
_PUNCT = re.compile(r"[.,;:!?()\[\]/-]")


def extract(prompt: str) -> tuple[int, ...]:
    """The feature vector for *prompt*, positionally matching FEATURE_NAMES."""
    numbers = _NUMBER.findall(prompt)
    words = _WORD.findall(prompt)
    return (
        min(len(prompt), CLIP),
        min(len(words), CLIP),
        min(sum(len(number) for number in numbers), CLIP),
        min(max((len(number) for number in numbers), default=0), CLIP),
        min(len({word.lower() for word in words}), CLIP),
        min(len(_PUNCT.findall(prompt)), CLIP),
        min(prompt.count("{") + prompt.count("}"), CLIP),
        min(prompt.count("'") + prompt.count('"'), CLIP),
    )


def describe(prompt: str) -> dict[str, int]:
    """The same vector, named, for `amg explain` and for a failing assertion."""
    return dict(zip(FEATURE_NAMES, extract(prompt), strict=True))
