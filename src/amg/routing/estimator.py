"""The fitted part of the router, and the reason this repository exists.

A cost-routing policy that sends easy requests to a cheap model and hard ones to
an expensive one needs to decide, at request time, whether a request is easy.
That decision is a **model**: it has parameters, those parameters are fitted to
traffic, and like any fitted model it is optimistic on the traffic it was fitted
to.

Almost nothing in the routing literature or in the gateways that ship it splits
that data. A router is tuned on a month of logs and then reported against the
same month, and the saving it quotes is partly the fit talking about itself.
:mod:`amg.evaluate.experiment` measures how much: it fits here, on one workload,
and reports on a disjoint one drawn from the same distribution (the **control**,
which isolates the optimism of fitting) and on workloads whose difficulty mix
has moved (the **treatment**, which is what happens in a real deployment when
traffic changes and nobody refits).

Two implementation decisions are worth the words.

**Fitting uses floats; serving uses integers.** Gradient descent in fixed point
is miserable and pointless -- the fit happens once, offline, and its output is a
committed artefact. But the *decision* is a threshold comparison, and a
threshold comparison on floats can resolve differently on two machines. So the
fitted weights are quantised to integers scaled by :data:`WEIGHT_SCALE`, the
served score is an integer dot product, and the routing decision is an integer
comparison. Replay then reproduces the served decision exactly, which is the
whole basis for reporting regret against a counterfactual policy.

**The model predicts "will the cheapest upstream get this right", not
difficulty.** Difficulty is a label the corpus happens to carry and production
does not. Predicting the thing the routing decision actually turns on keeps the
estimator honest and makes it retrainable against real traffic, where the label
is whatever downstream signal a deployment has -- a thumbs-down, a retry, a
failed schema validation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from amg.errors import ConfigError, RefusalError
from amg.routing.features import FEATURE_NAMES, extract
from amg.upstream.base import Upstream
from amg.workload.corpus import Corpus

#: Fixed-point scale for the served weights. 2**20 keeps five decimal digits of
#: a weight, far finer than the fit's own uncertainty, and keeps the integer dot
#: product small enough to read in a debug line.
WEIGHT_SCALE: Final[int] = 1 << 20

#: Features are counts with wildly different ranges -- `chars` reaches 512 while
#: `braces` rarely exceeds 4 -- so the fit standardises them. The divisors are
#: integers, computed from the fitting corpus and stored with the model, so the
#: served path stays in integer arithmetic.
MIN_DIVISOR: Final[int] = 1

#: Training runs to a gradient tolerance, not to a step count. A model stopped
#: at an arbitrary iteration is not at an optimum, and a routing threshold
#: calibrated against a half-trained score is calibrated against nothing. A
#: previous project in this series published a headline that moved by half its
#: own value when this was fixed, so the ceiling here exists only as a runaway
#: guard and is never expected to be reached.
#:
#: The optimiser is **IRLS** -- Newton's method on the logistic likelihood --
#: rather than gradient descent. With eight features the Hessian is a 9x9 matrix
#: and solving it exactly is cheaper than the hundreds of first-order steps the
#: same tolerance needs: measured here, gradient descent at its best stable
#: learning rate reached 3.9e-6 after 4,000 passes over the workload, which is
#: minutes of pure Python, while IRLS reaches 1e-9 in about ten. Convergence is
#: quadratic near the optimum, so the tolerance can be tight enough that the
#: stopping point is not a parameter anybody has to defend.
MAX_ITERATIONS: Final[int] = 100
TOLERANCE: Final[float] = 1e-9
L2: Final[float] = 1e-4

#: Added to the Hessian's diagonal before solving. The ridge penalty already
#: makes the system well conditioned for this feature set; this is the guard for
#: a future feature that is constant on some corpus, which would otherwise make
#: the Hessian singular and turn a bad feature into a crash.
_HESSIAN_RIDGE: Final[float] = 1e-9

#: A fit needs both outcomes present to separate anything.
_NEEDED_CLASSES: Final[int] = 2

FORMAT: Final[int] = 1


@dataclass(frozen=True, slots=True)
class Estimator:
    """A fitted predictor of "the cheapest upstream will answer this correctly".

    Attributes:
        weights: One integer per feature, scaled by :data:`WEIGHT_SCALE`.
        intercept: Also scaled by :data:`WEIGHT_SCALE`.
        divisors: Per-feature integer divisors from standardisation.
        fitted_on: Digest of the corpus this was fitted to. The experiment
            refuses to report a control number if the measurement corpus has
            this digest, because that would be the fit reporting on itself.
        target: What the model predicts, named so a reader is never guessing.
        iterations: How many steps the fit took.
        converged: Whether it reached the gradient tolerance.
    """

    weights: tuple[int, ...]
    intercept: int
    divisors: tuple[int, ...]
    fitted_on: str
    target: str
    iterations: int
    converged: bool

    def __post_init__(self) -> None:
        if len(self.weights) != len(FEATURE_NAMES):
            raise ConfigError(
                f"estimator has {len(self.weights)} weights for {len(FEATURE_NAMES)} features",
                remedy="Refit it: the feature layout changed under a stored model.",
            )
        if len(self.divisors) != len(FEATURE_NAMES):
            raise ConfigError("estimator has the wrong number of divisors")

    def score(self, prompt: str) -> int:
        """The fixed-point logit for *prompt*. Pure integer arithmetic.

        Higher means "more likely the cheapest upstream gets this right". The
        value is a scaled logit rather than a probability: converting it would
        need `exp`, which would put a libm call on the decision path for no
        benefit, since every threshold this is compared against lives in the
        same space.
        """
        vector = extract(prompt)
        total = self.intercept
        for value, weight, divisor in zip(vector, self.weights, self.divisors, strict=True):
            total += value * weight // divisor
        return total

    def to_json(self) -> str:
        """Serialise, sorted, so a committed artefact has a stable digest."""
        return json.dumps(
            {
                "format": FORMAT,
                "features": list(FEATURE_NAMES),
                "weights": list(self.weights),
                "intercept": self.intercept,
                "divisors": list(self.divisors),
                "weight_scale": WEIGHT_SCALE,
                "fitted_on": self.fitted_on,
                "target": self.target,
                "iterations": self.iterations,
                "converged": self.converged,
            },
            indent=2,
            sort_keys=True,
        )

    def write(self, path: Path) -> Path:
        """Write the estimator to *path*, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Estimator:
        """Read a fitted estimator, refusing one that does not match this code.

        The feature list is checked by *name*, not just by length. A reordering
        would otherwise pair every weight with the wrong feature and produce a
        router that runs, routes badly, and raises nothing.
        """
        if not path.exists():
            raise ConfigError(
                f"no estimator at {path}",
                remedy="Fit one with `amg fit --corpus <workload> --out <path>`.",
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") != FORMAT:
            raise ConfigError(f"{path} is format {document.get('format')}, expected {FORMAT}")
        stored = tuple(document.get("features", ()))
        if stored != FEATURE_NAMES:
            raise ConfigError(
                f"{path} was fitted over features {stored}, this build uses {FEATURE_NAMES}",
                remedy="Refit. Stored weights are positional and cannot be remapped.",
            )
        if document.get("weight_scale") != WEIGHT_SCALE:
            raise ConfigError(f"{path} uses a different fixed-point scale")
        return cls(
            weights=tuple(int(value) for value in document["weights"]),
            intercept=int(document["intercept"]),
            divisors=tuple(int(value) for value in document["divisors"]),
            fitted_on=str(document["fitted_on"]),
            target=str(document["target"]),
            iterations=int(document["iterations"]),
            converged=bool(document["converged"]),
        )


def _labels(corpus: Corpus, cheapest: Upstream) -> list[int]:
    """1 where the cheapest upstream answers correctly, else 0."""
    labels: list[int] = []
    for task in corpus:
        attempt = cheapest.attempt(task)
        correct = attempt.ok and attempt.response is not None and task.is_correct(attempt.response)
        labels.append(1 if correct else 0)
    return labels


def fit(corpus: Corpus, cheapest: Upstream) -> Estimator:  # noqa: C901, PLR0912
    """Fit the estimator on *corpus*, predicting whether *cheapest* succeeds.

    Plain logistic regression by heavy-ball gradient descent, in floats, run to
    a gradient tolerance and then quantised. Deliberately the simplest model
    that can express the relationship: the finding is about *evaluation*, and a
    stronger estimator would move the numbers while leaving the methodology
    point exactly where it is.

    Raises:
        RefusalError: if the fit hits the iteration ceiling. An unconverged
            estimator produces a score whose thresholds mean nothing, and a
            router calibrated against it would report a saving that is an
            artefact of where the optimiser happened to stop.
    """
    vectors = [extract(task.prompt) for task in corpus]
    labels = _labels(corpus, cheapest)
    if not vectors:
        raise ConfigError("cannot fit an estimator on an empty corpus")
    if len(set(labels)) < _NEEDED_CLASSES:
        raise RefusalError(
            "the cheapest upstream answered every task the same way, so there is "
            "nothing for an estimator to separate",
            remedy="Widen the corpus difficulty mix, or check the upstream catalogue.",
        )

    width = len(FEATURE_NAMES)
    # Integer divisors: the mean of each feature, floored, never below one.
    # Integers so the served path can reuse them without a float in sight.
    divisors = tuple(
        max(MIN_DIVISOR, sum(vector[index] for vector in vectors) // len(vectors))
        for index in range(width)
    )
    # Column 0 is the intercept, so the parameter vector and the design matrix
    # share an index and the Hessian assembly below needs no special case.
    design = [
        [1.0, *(value / divisor for value, divisor in zip(vector, divisors, strict=True))]
        for vector in vectors
    ]
    size = width + 1
    theta = [0.0] * size
    count = len(design)
    iterations = 0
    converged = False

    for step in range(1, MAX_ITERATIONS + 1):
        iterations = step
        gradient = [0.0] * size
        hessian = [[0.0] * size for _ in range(size)]
        for row, label in zip(design, labels, strict=True):
            logit = sum(parameter * value for parameter, value in zip(theta, row, strict=True))
            # The two-branch sigmoid is exact and never calls exp on a large
            # positive argument, where the one-line form overflows.
            if logit >= 0:
                prediction = 1.0 / (1.0 + math.exp(-logit))
            else:
                exponential = math.exp(logit)
                prediction = exponential / (1.0 + exponential)
            residual = label - prediction
            weight = prediction * (1.0 - prediction)
            for i in range(size):
                gradient[i] += residual * row[i]
                if weight:
                    weighted = weight * row[i]
                    for j in range(i, size):
                        hessian[i][j] += weighted * row[j]

        # Ridge on the slopes only. Penalising the intercept would shrink the
        # base rate towards a half, which is a claim about the data nobody made.
        for i in range(1, size):
            gradient[i] -= L2 * count * theta[i]
            hessian[i][i] += L2 * count
        for i in range(size):
            hessian[i][i] += _HESSIAN_RIDGE
            for j in range(i):
                hessian[i][j] = hessian[j][i]

        if max(abs(value) for value in gradient) / count < TOLERANCE:
            converged = True
            break

        step_vector = _solve(hessian, gradient)
        for i in range(size):
            theta[i] += step_vector[i]

    if not converged:
        raise RefusalError(
            f"the estimator did not converge in {MAX_ITERATIONS} Newton steps",
            remedy=(
                "An unconverged estimator has thresholds that mean nothing. This "
                "usually means a feature is collinear or constant on this corpus; "
                "check `amg explain` on a few prompts before raising the ceiling."
            ),
        )

    return Estimator(
        weights=tuple(round(value * WEIGHT_SCALE) for value in theta[1:]),
        intercept=round(theta[0] * WEIGHT_SCALE),
        divisors=divisors,
        fitted_on=corpus.digest(),
        target=f"{cheapest.name}_answers_correctly",
        iterations=iterations,
        converged=converged,
    )


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve ``matrix @ x = vector`` by Gaussian elimination with partial pivoting.

    Nine unknowns, so an explicit solve is a few hundred operations and brings
    no dependency. Partial pivoting rather than naive elimination because the
    Hessian's diagonal entries differ by orders of magnitude across features
    whose scales differ, and eliminating on a small pivot loses precision that
    Newton then spends iterations recovering.
    """
    size = len(vector)
    augmented = [[*row, value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if augmented[pivot][column] == 0.0:
            raise RefusalError(
                "the Hessian is singular: a feature is constant or collinear on this workload",
                remedy="Drop the offending feature, or widen the corpus.",
            )
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / divisor
            if factor:
                for index in range(column, size + 1):
                    augmented[row][index] -= factor * augmented[column][index]
    solution = [0.0] * size
    for row in reversed(range(size)):
        total = augmented[row][size] - sum(
            augmented[row][column] * solution[column] for column in range(row + 1, size)
        )
        solution[row] = total / augmented[row][row]
    return solution
