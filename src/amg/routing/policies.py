"""The routing policies, as pure functions of what a gateway can see.

A policy answers one question: **given this prompt, which upstreams should be
tried, in what order?** It returns a ladder rather than a single choice, because
the interesting policy in this set -- the cascade -- decides whether to climb it
based on what came back.

Everything here is pure. A policy takes a prompt string and returns a
:class:`Decision`; it performs no I/O, holds no mutable state, and cannot see
the task's difficulty, its family, or its answer. That is enforced by the
signature rather than by discipline: those fields are not reachable from a
``str``.

**The same `Decision` drives the served path and the replayed one.** ``amg
serve`` and ``amg replay`` both call :meth:`Policy.decide` and neither has its
own copy of the logic, because a regret table computed by a replay path that had
drifted from the served path would be a table about a gateway nobody is running.
``tests/integration/test_routing_core.py`` asserts the two produce identical
decisions over the whole corpus.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Protocol

from amg.errors import ConfigError
from amg.routing.estimator import Estimator
from amg.upstream.simulated import BY_PRICE

#: Resolution of the blend policy's traffic shares: parts per ten thousand.
SHARE_SCALE: Final[int] = 10_000

#: Policy names, so a report, a baseline and a CLI flag cannot drift apart.
POLICY_NAMES: Final[tuple[str, ...]] = (
    "cheapest",
    "best",
    "cascade",
    "blend",
    "fitted",
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Which upstreams to try, in order, and why.

    Attributes:
        ladder: Upstream names, first choice first. A single entry means no
            escalation is permitted -- the gateway may still *retry* that
            upstream after a transport failure, which is a different thing.
        reason: One short phrase for the audit record. A gateway that cannot
            say why it routed somewhere is one nobody can debug at 3am.
    """

    ladder: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not self.ladder:
            raise ConfigError("a routing decision must name at least one upstream")

    @property
    def first(self) -> str:
        """The upstream this request goes to."""
        return self.ladder[0]


#: The three fixed policies ignore the prompt entirely, which is the point of
#: them: they are the baselines every routing claim is measured against. Ruff
#: flags the unused parameter, and the parameter has to stay -- it is the
#: Protocol's signature, and a policy that could not be called like the others
#: would not be a comparable arm.
class Policy(Protocol):
    """Something that turns a prompt into a :class:`Decision`."""

    @property
    def name(self) -> str:
        """Stable identifier used in reports and baselines."""
        ...

    def decide(self, prompt: str) -> Decision:
        """Route *prompt*. Pure: same input, same output, always."""
        ...


@dataclass(frozen=True, slots=True)
class Cheapest:
    """Always the cheapest upstream. **The null baseline.**

    Every claim a router makes is a claim against this policy. It needs no
    fitting, no traffic history and no maintenance, so a router that does not
    beat it on the cost-quality frontier is a router that is not paying for
    itself -- and one that beats it only on the workload it was fitted to is
    worse than that, because it also has to be retrained.
    """

    name: str = "cheapest"

    def decide(self, prompt: str) -> Decision:  # noqa: ARG002 - baseline; see above
        """Always the cheapest upstream, whatever the prompt says."""
        return Decision(ladder=(BY_PRICE[0],), reason="null baseline: always cheapest")


@dataclass(frozen=True, slots=True)
class Best:
    """Always the most expensive upstream: the quality ceiling.

    The other end of the frontier. Any policy scoring above this on correctness
    is measuring noise, and any policy costing more is indefensible.
    """

    name: str = "best"

    def decide(self, prompt: str) -> Decision:  # noqa: ARG002 - baseline; see above
        """Always the most expensive upstream."""
        return Decision(ladder=(BY_PRICE[-1],), reason="quality ceiling: always best")


@dataclass(frozen=True, slots=True)
class Cascade:
    """Try the cheapest; escalate if the response fails validation.

    The escalation trigger is :func:`amg.workload.tasks.parse_answer` -- did the
    model return the JSON shape it was asked for -- and **not** correctness,
    which a gateway cannot know. That distinction is the whole point of this
    policy being in the comparison: it can only recover the errors it can
    detect, and a confidently wrong answer in well-formed JSON is invisible to
    it.

    The evaluation measures the size of that gap rather than assuming it, and it
    is the number to read before believing any self-validating cascade's claims.
    """

    name: str = "cascade"

    def decide(self, prompt: str) -> Decision:  # noqa: ARG002 - baseline; see above
        """Cheapest first, with the best model as the escalation rung."""
        return Decision(
            ladder=(BY_PRICE[0], BY_PRICE[-1]),
            reason="cheapest first, escalate on malformed output",
        )


@dataclass(frozen=True, slots=True)
class Blend:
    """Route by coin flip, with the shares calibrated to a spend target.

    **This is the null that matters**, and it is a stricter one than
    :class:`Cheapest`. A fitted router that beats always-cheapest has proved
    only that spending more money buys more correctness, which was never in
    doubt. The question is whether its *estimate* is worth anything -- whether
    knowing which requests to escalate beats escalating the same proportion at
    random.

    So this policy escalates the same share of traffic as the fitted router, at
    the same price, choosing which requests by a hash of the prompt. Any
    correctness the fitted router has over this one is attributable to the
    estimator rather than to the budget.

    The hash is BLAKE2b over the prompt rather than ``random``: the choice must
    be identical on a replay, and it must not depend on the order requests
    arrived in. Python's ``hash`` is salted per process and would make two runs
    disagree with nothing raising.
    """

    to_cheap: int
    to_middle: int
    name: str = "blend"

    def __post_init__(self) -> None:
        if not 0 <= self.to_cheap <= SHARE_SCALE:
            raise ConfigError("shares are parts per ten thousand")
        if not 0 <= self.to_middle <= SHARE_SCALE:
            raise ConfigError("shares are parts per ten thousand")
        if self.to_cheap + self.to_middle > SHARE_SCALE:
            raise ConfigError(
                f"shares sum to more than {SHARE_SCALE}",
                remedy="They partition the traffic; the remainder goes to the best model.",
            )

    def decide(self, prompt: str) -> Decision:
        """Pick a tier by hashing the prompt, ignoring what it says."""
        draw = (
            int.from_bytes(hashlib.blake2b(prompt.encode(), digest_size=8).digest(), "big")
            % SHARE_SCALE
        )
        if draw < self.to_cheap:
            return Decision((BY_PRICE[0],), reason="spend-matched coin: cheapest")
        if draw < self.to_cheap + self.to_middle:
            return Decision((BY_PRICE[1],), reason="spend-matched coin: middle")
        return Decision((BY_PRICE[-1],), reason="spend-matched coin: best")


@dataclass(frozen=True, slots=True)
class Fitted:
    """Route on a fitted estimate of whether the cheapest upstream will cope.

    Two integer thresholds over the estimator's fixed-point score, both
    calibrated on the fitting workload by :mod:`amg.routing.calibrate`. Above
    *high* the request goes to the cheapest model; below *low* it goes to the
    best; between them to the middle tier.

    This is the policy the repository is about. It is the one everybody ships,
    it is a fitted model, and it is the one whose reported saving is inflated by
    being evaluated on its own training traffic.
    """

    estimator: Estimator
    high: int
    low: int
    name: str = "fitted"

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ConfigError(
                f"the high threshold ({self.high}) is below the low one ({self.low})",
                remedy="Thresholds are on a score where higher means easier.",
            )
        if not self.estimator.converged:
            raise ConfigError(
                "refusing to route on an estimator that did not converge",
                remedy="Refit it; its thresholds do not mean anything.",
            )

    def decide(self, prompt: str) -> Decision:
        """Score the prompt and pick the tier its estimate falls into."""
        score = self.estimator.score(prompt)
        if score >= self.high:
            return Decision((BY_PRICE[0],), reason=f"predicted easy (score {score})")
        if score >= self.low:
            return Decision((BY_PRICE[1],), reason=f"predicted moderate (score {score})")
        return Decision((BY_PRICE[-1],), reason=f"predicted hard (score {score})")


def build(
    name: str,
    *,
    estimator: Estimator | None = None,
    thresholds: tuple[int, int] | None = None,
    shares: tuple[int, int] | None = None,
) -> Policy:
    """Construct a policy by name, refusing a fitted one with nothing fitted.

    The refusal matters. A gateway that silently degrades to "always cheapest"
    when its estimator is missing looks healthy, costs less, and answers worse --
    and the only symptom is a quality number nobody is watching.
    """
    if name == "cheapest":
        return Cheapest()
    if name == "best":
        return Best()
    if name == "cascade":
        return Cascade()
    if name == "blend":
        if shares is None:
            raise ConfigError(
                "the blend policy needs traffic shares calibrated to a spend target",
                remedy=(
                    "Run `amg calibrate`, which sizes them against the fitted policy's "
                    "spend. Ungated shares would make it a different experiment."
                ),
            )
        return Blend(to_cheap=shares[0], to_middle=shares[1])
    if name == "fitted":
        if estimator is None or thresholds is None:
            raise ConfigError(
                "the fitted policy needs an estimator and calibrated thresholds",
                remedy=(
                    "Run `amg fit` then `amg calibrate`, and pass --estimator. "
                    "Falling back to another policy would hide the misconfiguration."
                ),
            )
        return Fitted(estimator=estimator, high=thresholds[0], low=thresholds[1])
    raise ConfigError(
        f"unknown policy {name!r}",
        remedy=f"Known policies: {', '.join(POLICY_NAMES)}.",
    )
