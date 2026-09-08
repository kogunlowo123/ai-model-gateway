"""A real model provider, over a local Ollama server. No credentials, no bill.

This module exists for one reason: **every accuracy number this project
publishes is a property of the table in :mod:`amg.upstream.simulated`**, and a
table nobody has ever compared against a real model is a table that could say
anything. So this is the path that compares it. It speaks the same
:class:`~amg.upstream.base.Upstream` protocol as the simulator, answers the same
generated tasks, and reports the same :class:`~amg.upstream.base.Attempt`, which
means the measurement is like-for-like rather than an analogy.

``docs/simulator.md`` records what that comparison found -- including that the
generated difficulty scale separates a real small model far less sharply than it
separates the simulated tiers -- and ``scripts/measure-ollama.py`` produced it.

**This upstream is deliberately not on the experiment path**, and the reason is
not squeamishness about network calls:

* it is **not deterministic**. Ollama at ``temperature=0`` is far more stable
  than at 1.0, but it is not bit-reproducible across builds, quantisations, GPU
  drivers or batch sizes, and :mod:`amg.replay` exists to fail loudly when a
  replay does not reproduce. Wiring a real model into the experiment would mean
  either deleting that gate or watching it flake;
* it makes **wall-clock latency**, and the resilience sweep runs on a virtual
  clock precisely so that a queueing result is a queueing result rather than a
  measurement of this laptop;
* the numbers would not be **portable**. Nobody reproducing this repository has
  the same model at the same quantisation, so a committed baseline built from
  real calls would fail for every reader.

The simulator is therefore what the gateway is measured against, and this is
what the simulator is measured against. Both statements are in the README,
together, because only the pair of them is honest.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from amg.errors import ConfigError, UpstreamError
from amg.upstream.base import Attempt, Outcome, estimate_tokens
from amg.workload.tasks import Task, parse_answer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

#: Where an Ollama server listens unless told otherwise.
DEFAULT_BASE_URL: Final[str] = "http://localhost:11434"

#: How long to wait for one generation. Generous: a 3B model on CPU answering a
#: five-step arithmetic prompt is slow, and a timeout here would be recorded as
#: the *model* failing, which would make the comparison a measurement of the
#: hardware.
DEFAULT_TIMEOUT_S: Final[float] = 180.0

#: The instruction wrapped around every task. It is the same envelope the
#: simulator's ``malformed_share`` models -- a response that is not this shape
#: is the one kind of wrongness a gateway can detect at request time -- so the
#: prompt has to actually ask for it or the comparison is unfair to the model.
SYSTEM_PROMPT: Final[str] = (
    "You answer with a single JSON object and nothing else. "
    'The object has exactly one key, "answer". '
    "Do not explain, do not show working, do not use markdown fences."
)


def _client(base_url: str, timeout_s: float) -> Any:
    """Build an httpx client, or explain how to install one.

    ``httpx`` is an optional extra rather than a dependency because the shipped
    experiment never makes a network call, and a required HTTP library in a
    project whose whole point is a deterministic simulator would be a lie about
    what the code does.
    """
    try:
        import httpx  # noqa: PLC0415 - optional extra, imported where it is used
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        raise ConfigError(
            "the Ollama upstream needs httpx, which is an optional extra",
            remedy="Install it with `uv sync --extra ollama` (or `pip install 'amg[ollama]'`).",
        ) from exc
    return httpx.Client(base_url=base_url, timeout=timeout_s)


@dataclass(frozen=True, slots=True)
class OllamaUpstream:
    """A local model, wearing the same interface as the simulated ones.

    Attributes:
        model: The Ollama tag, for example ``qwen2.5:3b``. Pulled separately;
            this class never downloads anything.
        base_url: Where the server listens.
        input_price_per_1k: Micro-cents per 1,000 input tokens. **Zero by
            default, and that is a real statement rather than a placeholder**:
            a model running on hardware you already own has no per-token price,
            and pretending otherwise would put an invented number into a cost
            report. Set it if you are modelling a hosted equivalent.
        output_price_per_1k: The same, for output.
        timeout_s: Per-generation ceiling.
        options: Extra Ollama options, merged over the defaults below. The
            defaults pin ``temperature`` to 0 and ``seed`` to a constant, which
            is the closest a real model gets to reproducible -- close enough to
            re-run a measurement and see roughly the same figure, nowhere near
            close enough for :mod:`amg.replay`.
    """

    model: str
    base_url: str = DEFAULT_BASE_URL
    input_price_per_1k: int = 0
    output_price_per_1k: int = 0
    timeout_s: float = DEFAULT_TIMEOUT_S
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Stable identifier. The tag itself, so a report says what ran."""
        return self.model

    def attempt(self, task: Task, *, nonce: int = 0, at_us: int = 0) -> Attempt:
        """Answer *task* with the real model.

        ``nonce`` varies the sampling seed, so a retry is a genuinely different
        draw rather than a replay of the call that just failed. ``at_us`` is
        accepted and ignored: this upstream has no simulated clock, and an
        outage here is whatever the server is actually doing.
        """
        del at_us
        payload = {
            "model": self.model,
            "prompt": task.prompt,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "seed": 1 + nonce, **self.options},
        }
        started = time.perf_counter()
        try:
            with _client(self.base_url, self.timeout_s) as client:
                response = client.post("/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
        except ConfigError:
            # A missing optional dependency is a configuration error and must
            # stay one. Recording it as an error *rate* would report "this model
            # fails 100% of the time" when the truth is that httpx is not
            # installed -- a number that looks like a finding.
            raise
        except Exception:  # noqa: BLE001 - every transport failure is the same Outcome
            # A transport failure is a measurement, not a crash: it is exactly
            # what the simulator's `failure_rate` models, and raising here would
            # abort a calibration run half way through instead of showing up as
            # the error rate it is.
            return Attempt(
                upstream=self.name,
                outcome=Outcome.ERROR,
                response=None,
                latency_us=int((time.perf_counter() - started) * 1_000_000),
                input_tokens=estimate_tokens(task.prompt),
                output_tokens=0,
                cost_micro_cents=0,
            )

        text = str(body.get("response", ""))
        input_tokens = int(body.get("prompt_eval_count") or estimate_tokens(task.prompt))
        output_tokens = int(body.get("eval_count") or estimate_tokens(text))
        return Attempt(
            upstream=self.name,
            outcome=Outcome.OK,
            response=text,
            latency_us=int((time.perf_counter() - started) * 1_000_000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micro_cents=(
                input_tokens * self.input_price_per_1k // 1_000
                + output_tokens * self.output_price_per_1k // 1_000
            ),
        )


def available(base_url: str = DEFAULT_BASE_URL, timeout_s: float = 5.0) -> tuple[str, ...]:
    """Model tags the server has pulled, or ``()`` if there is no server.

    Returns rather than raises, because every caller wants to *skip* when there
    is nothing to talk to. A test that fails because a developer does not run
    Ollama would teach people to ignore the suite.
    """
    try:
        with _client(base_url, timeout_s) as client:
            body = client.get("/api/tags").json()
    except Exception:  # noqa: BLE001 - "no server" is the only thing being asked
        return ()
    return tuple(sorted(str(entry["name"]) for entry in body.get("models", [])))


@dataclass(frozen=True, slots=True)
class Observed:
    """What one real model did on one difficulty band."""

    difficulty: int
    asked: int
    correct: int
    malformed: int
    errors: int
    latency_us: tuple[int, ...]

    @property
    def accuracy_per_10k(self) -> int:
        """Correct answers in parts per ten thousand, the simulator's unit."""
        return round(10_000 * self.correct / self.asked) if self.asked else 0

    @property
    def malformed_share_of_wrong(self) -> float:
        """Of the answers that were wrong, the share a gateway could detect.

        The ceiling on what a self-validating cascade can recover, and the
        parameter the "cascade illusion" finding turns on. Measuring it against
        a real model is the point of this whole module.
        """
        wrong = self.asked - self.correct - self.errors
        return self.malformed / wrong if wrong > 0 else 0.0


def measure(
    upstream: OllamaUpstream,
    tasks: Iterable[Task],
) -> tuple[Observed, ...]:
    """Ask *upstream* every task and bucket the results by difficulty.

    The same ``is_correct`` and ``parse_answer`` the simulated path uses, so the
    only thing that differs between this measurement and the simulator's table
    is the model.
    """
    buckets: dict[int, dict[str, Any]] = {}
    for task in tasks:
        bucket = buckets.setdefault(
            task.difficulty,
            {"asked": 0, "correct": 0, "malformed": 0, "errors": 0, "latency": []},
        )
        attempt = upstream.attempt(task)
        bucket["asked"] += 1
        bucket["latency"].append(attempt.latency_us)
        if attempt.outcome is not Outcome.OK or attempt.response is None:
            bucket["errors"] += 1
            continue
        answer = parse_answer(attempt.response)
        if answer is None:
            bucket["malformed"] += 1
        elif task.is_correct(attempt.response):
            bucket["correct"] += 1
    return tuple(
        Observed(
            difficulty=difficulty,
            asked=values["asked"],
            correct=values["correct"],
            malformed=values["malformed"],
            errors=values["errors"],
            latency_us=tuple(values["latency"]),
        )
        for difficulty, values in sorted(buckets.items())
    )


def to_json(upstream: OllamaUpstream, observed: Iterable[Observed]) -> str:
    """Serialise a measurement, sorted, so it can be committed and diffed."""
    return json.dumps(
        {
            "model": upstream.model,
            "system_prompt": SYSTEM_PROMPT,
            "by_difficulty": [
                {
                    "difficulty": row.difficulty,
                    "asked": row.asked,
                    "correct": row.correct,
                    "malformed": row.malformed,
                    "errors": row.errors,
                    "accuracy_per_10k": row.accuracy_per_10k,
                    "malformed_share_of_wrong": round(row.malformed_share_of_wrong, 4),
                    "median_latency_us": sorted(row.latency_us)[len(row.latency_us) // 2]
                    if row.latency_us
                    else 0,
                }
                for row in observed
            ],
        },
        indent=2,
        sort_keys=True,
    )


def require(model: str, base_url: str = DEFAULT_BASE_URL) -> OllamaUpstream:
    """An upstream for *model*, refusing early if the server has not pulled it."""
    tags = available(base_url)
    if not tags:
        raise UpstreamError(
            f"no Ollama server answering at {base_url}",
            remedy="Start one with `ollama serve`, or pass --base-url.",
        )
    if model not in tags:
        raise ConfigError(
            f"{model!r} is not pulled on this server",
            remedy=f"Run `ollama pull {model}`. Available: {', '.join(tags)}.",
        )
    return OllamaUpstream(model=model, base_url=base_url)
