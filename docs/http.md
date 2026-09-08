# The HTTP surface

An OpenAI-compatible subset, over exactly the same routing core the experiment
measures.

```
$ amg serve
serving on http://127.0.0.1:8000 with policy cascade
```

## The served path and the measured path are the same function

Every request handled here goes through `amg.gateway.serve_one`, with a policy
built from the environment — the same function, the same policies, the same
upstream protocol that `amg.replay` uses to compute counterfactuals.

A gateway whose served path differs from its measured path publishes numbers
about software nobody is running. `tests/integration/test_routing_core.py`
asserts the two agree decision for decision, so that claim is a test rather than
an intention.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | The process is up. Liveness. |
| `GET` | `/readyz` | The policy built, the upstreams resolved. Readiness. |
| `GET` | `/v1/models` | The catalogue, in OpenAI's list shape. |
| `POST` | `/v1/chat/completions` | Route and answer. |

`healthz` and `readyz` are separate because they answer different questions. A
process can be alive and not ready — `AMG_POLICY=fitted` with an estimator that
failed to load is exactly that state, and it is the one where a gateway must
*not* take traffic. Collapsing them into one endpoint is how a misconfigured
router quietly starts serving always-cheapest.

## What the response carries that OpenAI's does not

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1788905586,
  "model": "nano",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "{\"answer\": 42}"},
     "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 16, "completion_tokens": 4, "total_tokens": 20},
  "amg": {
    "policy": "cascade",
    "upstream": "nano",
    "reason": "cheapest first, escalate on malformed output",
    "outcome": "answered",
    "escalated": false,
    "attempts": 1,
    "cost_micro_cents": 320,
    "latency_us": 218770,
    "client": "curl/8.4.0"
  }
}
```

The `amg` block is the point of the surface. A gateway that hides which model
answered is one nobody can debug, and cost that is only visible on next month's
invoice is cost nobody can attribute to a request, a customer or a feature.

The `model` field in the **request** is accepted and **ignored for routing**,
deliberately: choosing is the whole job of the gateway, and honouring a request
for a specific tier would make it a proxy instead.

The response does not echo that field back — `model` in the response is the
upstream that actually answered. This is what OpenAI itself does: ask for a
family, get told the concrete version that served you. Echoing the request value
would let a client assert `response.model == request.model` and pass, while
hiding which model was paid for.

## Limits and refusals

* **`MAX_PROMPT_CHARS = 32,000`**, refused with `413` before any work happens. A
  gateway in front of a paid API is exactly the place where an unbounded request
  body turns directly into an unbounded bill.
* **A policy that cannot be built refuses at startup**, not per request.
  `AMG_POLICY=fitted` without `AMG_ESTIMATOR_PATH` fails to start; so does
  `AMG_POLICY=blend` without calibrated shares. Falling back would be a gateway
  that looks healthy, costs less and answers worse.
* **A misspelt `AMG_` variable fails at startup.** See
  [configuration](#configuration-is-validated-by-name) below.

## What it deliberately does not have

Stated here rather than left to be discovered:

* **No `/metrics` endpoint.** An earlier project in this series is an
  observability pipeline and does that job properly. A thin Prometheus surface
  here would be a worse duplicate of it.
* **No streaming.** The routing decision this project measures happens *before*
  the first token. Streaming would add a large amount of surface area without
  touching anything under study.
* **No authentication.** This server has nothing to protect: it routes between
  simulated upstreams and holds no credentials. Adding a bearer token would
  imply otherwise. It binds to `127.0.0.1` by default; put it behind something
  that does authenticate before exposing it. See [SECURITY.md](../SECURITY.md).

## Configuration is validated by name

`Settings` is prefixed `AMG_`, frozen, and `extra="forbid"`.

That last one is not sufficient, and finding out why was worth a test of its
own. **`extra="forbid"` does not catch a misspelt prefixed variable.**
pydantic-settings walks from field names to the environment, so a variable that
matches no field is never looked at:

```
# What plain `Settings()` does -- note the missing R in CONCURRENCY:
>>> os.environ["AMG_CONCURENCY"] = "48"
>>> Settings().concurrency
24                                   # constructed cleanly, value silently ignored

# What this project ships instead:
$ AMG_CONCURENCY=48 amg serve
serve: these environment variables are set and nothing reads them: AMG_CONCURENCY
Check the spelling against: AMG_BACKOFF_BASE_US, ..., AMG_CONCURRENCY, ...
A setting somebody chose that silently does nothing is worse than one that
fails at startup.
```

A carefully set value that does nothing for six months is a worse failure than a
crash. `Settings.from_environment()` compares the environment against the known
field names and refuses on an unknown `AMG_` variable, and it is what both
process entry points use. `tests/unit` pins both the fix and the underlying
pydantic behaviour, so a library change that made `extra="forbid"` sufficient
would show up as a failing test rather than as silent redundancy.

## See also

* [Routing and the estimator](routing.md) — what the policies do.
* [`.env.example`](../.env.example) — every variable, with what it costs to get
  it wrong.
