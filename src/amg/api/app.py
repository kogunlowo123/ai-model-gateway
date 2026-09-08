"""The HTTP surface: an OpenAI-compatible subset over the same routing core.

Thin on purpose. Every request handled here goes through
:func:`amg.gateway.serve_one` with a policy built from the environment -- the
same function, the same policies, the same upstream protocol that
:mod:`amg.replay` uses to compute counterfactuals. A gateway whose served path
differs from its measured path publishes numbers about software nobody is
running, and ``tests/integration/test_routing_core.py`` asserts the two agree
decision for decision.

**What the response carries that OpenAI's does not.** Every completion comes
back with an ``amg`` block naming the upstream that answered, the policy's
reason for choosing it, the cost in micro-cents, and how many upstream calls it
took. A gateway that hides which model answered is one nobody can debug, and
cost that is only visible on next month's invoice is cost nobody can attribute.

**What it deliberately does not have.** No ``/metrics`` endpoint: a previous
project in this series is an observability pipeline and does the job properly,
and a thin Prometheus surface here would be a worse duplicate. No streaming:
the routing decision this project measures happens before the first token, so
streaming would add a large amount of surface without touching anything being
studied. Both are stated in ``docs/http.md`` rather than left to be discovered.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Final

from fastapi import Body, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from amg.errors import GatewayError
from amg.gateway import ANSWERED, MALFORMED, serve_one
from amg.settings import Settings
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_NAME, CATALOGUE
from amg.workload.tasks import Task

#: Requests larger than this are refused before any work happens. A gateway in
#: front of a paid API is a place where an unbounded body turns directly into an
#: unbounded bill.
MAX_PROMPT_CHARS: Final[int] = 32_000


class Message(BaseModel):
    """One chat message, in the shape an OpenAI client already sends."""

    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    """The subset of the chat-completions request this gateway understands.

    ``model`` is accepted and **ignored for routing**, deliberately: choosing
    is the whole job of the gateway, so honouring a client's request for a
    specific tier would make it a proxy instead.

    The response does not echo this field back. It carries the upstream that
    actually answered, which is what OpenAI itself does -- ask for a family,
    get told the concrete version that served you. A client asserting that the
    response model equals the request model is asserting something no gateway
    can promise, and returning the request value would let that assertion pass
    while hiding which model was paid for.
    """

    model: str = "amg-auto"
    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    """An OpenAI-shaped response, plus the provenance a gateway owes its caller."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int]
    amg: dict[str, Any]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, resolving configuration once at startup.

    The policy is constructed here rather than per request, so a
    misconfiguration -- a fitted policy with no estimator, a deadline below the
    call timeout -- fails at start rather than on the first request that happens
    to arrive at 3am.
    """
    resolved = settings or Settings.from_environment()
    policy = resolved.build_policy()
    config = resolved.gateway()
    # Annotated as the protocol rather than the concrete type: `dict` is
    # invariant, so a dict[str, SimulatedUpstream] is not a dict[str, Upstream]
    # and every call downstream would need a cast.
    upstreams: dict[str, Upstream] = dict(BY_NAME)

    app = FastAPI(
        title="ai-model-gateway",
        version="0.1.0",
        summary="A model gateway that treats its routing policy as a fitted model.",
    )
    app.state.settings = resolved
    app.state.policy = policy

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Says nothing about upstreams."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        """Readiness: the policy resolved and the catalogue loaded.

        Separate from liveness because they answer different questions and a
        single endpoint doing both gets restarted for problems a restart cannot
        fix.
        """
        return {
            "status": "ready",
            "policy": policy.name,
            "upstreams": sorted(upstreams),
            "deadline_us": config.deadline_us,
        }

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        """The catalogue, in the shape an OpenAI client expects to list."""
        return {
            "object": "list",
            "data": [
                {
                    "id": upstream.name,
                    "object": "model",
                    "owned_by": "simulated",
                    "amg": {
                        "summary": upstream.summary,
                        "input_price_per_1k_micro_cents": upstream.input_price_per_1k,
                        "output_price_per_1k_micro_cents": upstream.output_price_per_1k,
                    },
                }
                for upstream in CATALOGUE
            ],
        }

    @app.post("/v1/chat/completions", response_model=ChatResponse)
    def chat_completions(
        request: Request,
        body: Annotated[ChatRequest, Body()],
    ) -> ChatResponse:
        """Route one request, call an upstream, and say what happened."""
        prompt = "\n".join(message.content for message in body.messages)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(f"prompt is {len(prompt)} characters, over the {MAX_PROMPT_CHARS} limit"),
            )

        # The task id is what every deterministic draw is keyed on, so a
        # request with the same content routes and resolves identically -- which
        # is what makes an HTTP call reproducible in a replay.
        task = Task(
            task_id=f"http:{hash_prompt(prompt)}",
            family="http",
            difficulty=1,
            prompt=prompt,
            answer="",
        )
        try:
            served = serve_one(task, policy, upstreams, config)
        except GatewayError as error:  # pragma: no cover - defensive
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error

        if served.outcome not in (ANSWERED, MALFORMED):
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT
                if served.outcome == "expired"
                else status.HTTP_502_BAD_GATEWAY,
                detail={
                    "outcome": served.outcome,
                    "attempts": len(served.attempts),
                    "reason": served.reason,
                    "cost_micro_cents": served.cost_micro_cents,
                },
            )

        return ChatResponse(
            id=task.task_id,
            created=int(time.time()),
            model=served.upstream or body.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": served.response},
                    "finish_reason": "stop",
                }
            ],
            usage={
                "prompt_tokens": sum(a.input_tokens for a in served.attempts),
                "completion_tokens": sum(a.output_tokens for a in served.attempts),
                "total_tokens": sum(a.input_tokens + a.output_tokens for a in served.attempts),
            },
            amg={
                "policy": policy.name,
                "upstream": served.upstream,
                "reason": served.reason,
                "outcome": served.outcome,
                "escalated": served.escalated,
                "attempts": len(served.attempts),
                "cost_micro_cents": served.cost_micro_cents,
                "latency_us": served.latency_us,
                "client": request.client.host if request.client else None,
            },
        )

    return app


def hash_prompt(prompt: str) -> str:
    """A short stable identifier for a prompt.

    BLAKE2b rather than Python's ``hash``, which is salted per process: two
    replicas of this gateway would otherwise assign different identities to the
    same request, and every deterministic draw keyed on that identity would
    diverge between them with nothing raising.
    """
    import hashlib  # noqa: PLC0415 - one call site

    return hashlib.blake2b(prompt.encode(), digest_size=8).hexdigest()
