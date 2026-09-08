# Architecture

## What this program is

A model gateway: something that sits in front of several model providers, picks
one per request, retries when a call fails, and reports what it cost. There are
many of those. What makes this one worth reading is that it **measures whether
its own routing and its own retries are worth having**, against nulls chosen so
the answer can come back "no" — and twice it does.

## The shape

```
                    amg.cli            amg.api.app
                        \                  /
                         \                /
                      amg.gateway.serve_one
                                |
        +-----------------------+------------------------+
        |                       |                        |
  amg.routing            amg.resilience            amg.upstream
  (which upstream)     (retry, breaker, pool)    (what answers, cost)
        |                       |                        |
        +-----------------------+------------------------+
                                |
                           amg.clock
                    (discrete-event simulation)

                     amg.replay   ->   amg.evaluate
              (serve a whole corpus)   (statistics, gate, report)
```

Two entry points, one core. `amg.cli` and `amg.api.app` both call
`amg.gateway.serve_one`; `amg.replay` calls `amg.gateway.serve` for a whole
corpus. **The served path and the measured path are the same code**, and
`tests/integration/test_routing_core.py` asserts they agree decision for
decision. A gateway whose measured path differs from its served path publishes
numbers about software nobody is running.

## The layers

| Module | Responsibility |
| --- | --- |
| `amg.money` | Integer micro-cents. Prices parsed from decimal strings. |
| `amg.clock` | Discrete-event simulation: virtual time, a bounded pool, abandonment. |
| `amg.workload` | Task generation, the corpus file format, content-addressed digests. |
| `amg.upstream` | The `Upstream` protocol; the simulated catalogue; the Ollama adapter. |
| `amg.routing` | Features, the fitted estimator, threshold calibration, the policies. |
| `amg.resilience` | Retry policy, circuit breakers. |
| `amg.gateway` | One request, end to end: route, call, retry, escalate, deadline. |
| `amg.replay` | A whole corpus under one policy; the determinism check. |
| `amg.evaluate` | Paired statistics, the baseline gate, the reports. |
| `amg.api` | The OpenAI-compatible HTTP surface. |
| `amg.settings` | Environment configuration, validated by name. |

Dependencies point downward only. `amg.routing` does not know that
`amg.evaluate` exists, which is what keeps a routing policy from accidentally
reading a number it could not have at request time.

---

# Decisions

Each records what was chosen, what it was chosen over, and what would have to
be true to revisit it.

### ADR-001 — A discrete-event simulation, not `asyncio`

**Chosen:** virtual time as an integer count of microseconds, events in a heap
keyed `(time, priority, sequence)`.

**Over:** real concurrency with `asyncio` and real sleeps.

**Why:** the resilience results *are* concurrency results, and concurrency
measured against a real clock is not reproducible. Two runs of the same
`asyncio` program interleave differently, so a queueing number becomes a
property of the machine and the load next to it. The heap key is a **total**
order — `sequence` is never equal between two entries — so no two runs can order
two events differently.

**Cost, stated plainly:** these are not wall-clock benchmarks. A latency figure
here is what the model of the system says.

**Revisit if:** the project ever needs to measure a real provider's tail
latency, which is a different question and would need a different harness.

### ADR-002 — Money is an integer count of micro-cents

**Chosen:** every amount is an `int` of millionths of a cent. Prices are parsed
from decimal strings; `cost_of` rounds **up**.

**Over:** floats, or `Decimal`.

**Why:** this program sums costs over thousands of requests and compares totals
against budget thresholds, and picks between policies whose costs differ in the
seventh decimal place. With floats, `0.1 + 0.2 != 0.3` decides which policy
wins and the answer changes with summation order. `Decimal` is correct but
carries a context, and a context is a global that can be changed.

Rounding up rather than to nearest: a gateway that rounds a fractional
micro-cent down on every request under-reports its own spend systematically, and
in the direction that flatters it.

### ADR-003 — Nothing on a decision path touches a float

**Chosen:** accuracy as an integer table in parts per ten thousand, latency as
integer microseconds, estimator weights quantised to fixed point, every draw
keyed on BLAKE2b.

**Over:** the ordinary thing — `random.random()` against a probability, a
sigmoid over float weights.

**Why:** libm's transcendental functions differ by one unit in the last place
between platforms. This simulation makes ordering decisions on latency and
threshold decisions on accuracy, so a single flipped comparison changes which
upstream won a race and therefore the entire event schedule after it. The
replay gate would then fail on somebody else's machine for a reason nobody could
act on.

**And never Python's `hash`:** it is salted per process, so a corpus scored in
one interpreter and replayed in another would take different branches and
produce different numbers, with nothing raising.

### ADR-004 — `Attempt` carries no `correct` field

**Chosen:** the request-time record holds what the gateway saw. Correctness is
joined afterwards, in `amg.evaluate`.

**Why:** correctness is not knowable at request time. Putting it on the record a
policy can see would let a router read it by accident, which is the easiest way
in the world to write a router that looks brilliant in evaluation and cannot be
deployed. This is a structural guarantee rather than a rule people remember.

### ADR-005 — `TIMEOUT` is a separate outcome from `ERROR`

**Chosen:** three outcomes, not two.

**Why:** a timed-out call was still served and still billed. The money left
even though no answer arrived. Folding it into `ERROR` would understate what a
retry policy spends, which is exactly the quantity the resilience sweep exists
to measure.

### ADR-006 — An end-to-end deadline, not only a per-attempt timeout

**Chosen:** `GatewayConfig.deadline_us` covers queueing plus the call. A
request still queued when its deadline passes abandons its slot and returns
`expired`.

**Over:** the per-attempt timeout the first implementation had.

**Why:** **a per-attempt timeout starts after the queue.** A request that waits
three seconds for a slot and then runs a two-second call took five seconds and
exceeded no timeout. Measured: p99 of 9,023 ms against a 6,000 ms budget, with
the error rate flat. With unbounded queueing, latency grows without bound while
every error-rate dashboard stays green.

`Settings` refuses to start if the deadline is below the timeout, because no
complete attempt could fit inside it.

### ADR-007 — IRLS for fitting, not gradient descent

**Chosen:** iteratively reweighted least squares — Newton's method on the
log-likelihood — with a small ridge on the slopes only.

**Over:** heavy-ball gradient descent, which is what was there first.

**Why:** it did not converge. At 4,000 iterations the gradient norm was still
3.9e-6 at a learning rate of 0.5, and higher rates oscillated. Sweeping learning
rates to rescue a convex problem is a sign of the wrong solver. IRLS reaches the
same optimum in six Newton steps and about a fifth of a second, **to a stated
tolerance rather than an iteration budget** — so "converged" is a fact.

The intercept is never penalised: shrinking it shifts the base rate, which is
not a thing that needs shrinking.

### ADR-008 — The null is spend-matched, and sized once

**Chosen:** `blend` routes a fixed share of traffic to each tier at random, with
shares calibrated so its total spend matches the fitted router's **on the
fitting workload**. Every headline difference is measured against it.

**Over:** comparing the fitted router against always-cheapest.

**Why:** a router that spends 2.7x what the cheapest policy spends *should* be
more correct than the cheapest policy. The question worth asking is whether it
beats **spending 2.7x at random**. Without that null, "routing works" is a
claim about having a bigger budget.

**Sized once, deliberately:** a real deployment sets both from the same month of
logs and retunes neither. The consequence is that the match is exact on the
fitting distribution and drifts off it, so every report prints the realised
spend ratio next to every difference, and rows above 1.00x are not clean
attribution claims. Recalibrating per workload would pin every ratio at one and
make the null a different policy on every row.

### ADR-009 — Correlated outages as well as independent failures

**Chosen:** two fault models. `flaky` fails each call independently; `outage`
takes **one** provider down for a share of every cycle.

**Why:** a consecutive-failure circuit breaker defends against an upstream being
*down*, not against one being *flaky*, and a simulator with only independent
failures cannot tell those apart — which would make every breaker measurement a
measurement of the wrong thing. The first version of this sweep had only
independent failures and concluded breakers never help, which was an artefact.

**And the outage hits one provider, not all three.** A version that took
everything down at once left failover nowhere to go and made breakers look
useless — a finding about a sweep with no survivor.

### ADR-010 — The gate compares against a recorded measurement

**Chosen:** `examples/baseline.json` holds what this gateway actually did, and
CI re-measures and fails on a move past a tolerance.

**Over:** absolute thresholds.

**Why:** "correctness above 80%" is meaningless when the shifted workloads sit
at 65% by design, and "cost below X" is meaningless when the whole finding is
that cost moves with the traffic mix. Either would be a gate that passes
whatever happens.

Six things fail the build, including the two people leave out: an arm in the
baseline this run did not measure, and an arm this run measured that the
baseline has never heard of. The resilience figures are in there too, because a
number on a front page that nothing re-measures will eventually be wrong.

### ADR-011 — A test suite that watches the gate go red

**Chosen:** `tests/meta/` breaks one thing at a time and asserts each check
fires, and `scripts/check-gateway.py` does the same against the shipped binary
in CI.

**Why:** a gate that has only ever been observed passing is indistinguishable
from `exit 0`. Both layers run a green control first, so a red result cannot be
a fact about a broken environment.

### ADR-012 — Four exit codes, and argparse does not get to pick

**Chosen:** 0 held, 1 usage, 2 gate failed, 3 could not run. `_Parser`
subclasses `ArgumentParser` to exit 1 rather than argparse's default 2.

**Why:** a misspelt flag and a routing regression would otherwise be the same
number, and a pipeline that cannot tell "you typed the path wrong" from "quality
regressed" will be taught to ignore both. `tests/e2e` asserts it from a real
subprocess, which is the only place an exit code is visible.

### ADR-013 — Configuration is validated by name, not just by shape

**Chosen:** `Settings.from_environment()` compares the environment against the
known field names and refuses on an unknown `AMG_` variable.

**Over:** relying on pydantic's `extra="forbid"`.

**Why:** measured — **`extra="forbid"` does not catch a misspelt prefixed
variable.** `AMG_CONCURENCY=48` constructs cleanly with concurrency still 24,
because pydantic-settings walks from field names to the environment and never
looks at a variable that matches no field. A setting somebody chose that
silently does nothing for six months is worse than a crash. A test pins both the
fix and the underlying pydantic behaviour, so a library change would show up as
a failure rather than as silently dead code.

### ADR-014 — Policies refuse rather than degrade

**Chosen:** `build()` raises if a `fitted` policy has no estimator or a `blend`
has no calibrated shares. The HTTP surface refuses at startup.

**Why:** a gateway that silently falls back to always-cheapest when its
estimator fails to load **looks healthy, costs less, and answers worse**. It is
the failure mode that presents as a cost saving, and the only symptom is a
quality number nobody is watching.

### ADR-015 — The Ollama adapter exists, and is not on the experiment path

**Chosen:** ship a real `Upstream` implementation against a local Ollama server,
and keep every published number away from it.

**Why:** the simulator's accuracy table is a *stipulation*, and a table nobody
has ever compared against a real model is a table that could say anything. So
there is a path that compares it, using the same protocol, the same generated
tasks and the same parser.

It stays off the experiment path because real models are not bit-reproducible
across builds, quantisations or drivers; because wall-clock latency would
undo ADR-001; and because nobody reproducing this repository has the same models,
so a baseline built from real calls would fail for every reader.

**Credential-free by construction.** Ollama is local and needs no key, which is
what makes it the one real integration a public portfolio repository can carry.

### ADR-016 — The workloads are content-addressed, not byte-compared

**Chosen:** each corpus carries a digest over the task fields, and `amg check`
re-derives it from its plan and compares.

**Over:** regenerating and running `git diff`.

**Why:** gzip carries header fields no consumer reads — and both bit us.
`mtime=0` was needed so the same corpus written twice does not differ by a
timestamp, and `filename=""` was needed because `GzipFile` takes the `FNAME`
header from the file object it is handed, so the same corpus written to two
different paths produced different bytes. Comparing digests over the fields
sidesteps all of it, and still fails on a hand-edited prompt — which matters,
because editing the corpus is the fastest way to make a baseline pass.

`DIGEST_FIELDS` is asserted equal to the fields of `Task`, so adding a field
without adding it to the digest is a test failure rather than a blind spot.

### ADR-017 — No `/metrics`, no streaming, no authentication

**Chosen:** a deliberately small HTTP surface.

**Why, one at a time.** An earlier project in this series is an observability
pipeline and does metrics properly; a thin Prometheus surface here would be a
worse duplicate. The routing decision this project measures happens *before* the
first token, so streaming would add a large amount of surface without touching
anything under study. And this server holds no credentials and routes between
simulated upstreams, so a bearer token would imply a protection that does not
exist — it binds to loopback and says to put it behind something that
authenticates.

All three are stated in `docs/http.md` rather than left to be discovered.

## See also

* [Routing and the estimator](docs/routing.md)
* [Retries, breakers, capacity](docs/resilience.md)
* [The simulator, and its limits](docs/simulator.md)
* [Reproducibility](docs/reproducibility.md)
* [Threat model](THREAT-MODEL.md)
