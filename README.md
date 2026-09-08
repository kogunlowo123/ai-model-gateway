# ai-model-gateway

A model gateway that **measures whether its own routing and its own retries are
worth having** — against nulls chosen so the answer can come back "no". Twice,
it does.

[![CI](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/ci.yml)
[![Security](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/security.yml/badge.svg)](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/security.yml)
[![Container](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/docker.yml/badge.svg)](https://github.com/kogunlowo123/ai-model-gateway/actions/workflows/docker.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Route between model tiers by predicted difficulty, retry and fail over when
providers break, serve it behind an OpenAI-compatible endpoint — and then run
the experiment that says what any of that was actually worth.

---

## Read this before any number below

**Both halves of the experiment are synthetic.** The workload is generated and
so is the model that answers it. What is measured is the **routing and
resilience arithmetic** — how much a fitted router's advantage survives held-out
traffic, what a self-validating cascade can recover, what a breaker is worth at
each capacity — and **not the quality of any real model.** Every accuracy figure
here is a property of a stipulated table.

Two limits follow, and they are the ceiling on everything below:

* **The grammar cannot be held out.** The tasks come from a fixed generator, and
  the estimator's features are the ones that generator makes predictive.
  Held-out *traffic* is measured; a held-out *notion of difficulty* is not, and
  cannot be from inside the same generator.
* **The separation between tiers is stipulated, not measured.** Every routing
  finding turns on how far apart a cheap and an expensive model are. A real
  local model was measured on the same tasks to check the table's *shape*
  ([docs/simulator.md](docs/simulator.md)), and nothing flagship-class was —
  that would need credentials this repository deliberately does not have. That
  measurement also found the generated difficulty scale separates a real small
  model far less sharply than it separates the simulated tiers, which is the
  first limit above with a number attached to it.

Everything else here is a real measurement of a real implementation, and the
gates re-run it on every push.

---

## Three findings

### 1. The router's quality generalises. Its budget does not.

Thresholds were calibrated **once**, on the fitting workload, to spend 3x what
always-cheapest spends. Nothing re-checked them.

| Workload | Hard | Fitted | Null | Difference | p | Spend vs cheapest | Spend vs null |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `fit` | 30% | 81.42% | 73.42% | +8.00p | 1.0e-50 | 2.76x | 0.99x |
| `control` | 31% | 80.96% | 73.12% | +7.83p | 1.5e-24 | 2.72x | 0.91x |
| `shift-10` | 9% | 85.88% | 83.79% | +2.08p | 7.7e-05 | 1.87x | 0.70x |
| `shift-50` | 50% | 76.33% | 62.75% | +13.58p | 8.8e-46 | 3.62x | 1.28x |
| `shift-70` | 69% | 67.96% | 52.29% | +15.67p | 2.1e-46 | 4.33x | 1.49x |
| `shift-90` | 91% | 65.17% | 40.08% | +25.08p | 1.0e-87 | 5.10x | **1.67x** |

**Quality transfers.** The optimism gap between the fitting workload and a
disjoint one from the same distribution is **+0.46 points** — a well-behaved
fitted model.

**Budget does not.** The same thresholds realise 1.87x on easy traffic and
**5.10x** on hard. A threshold is a decision boundary, not a budget: hand it a
harder mix and more requests land above it. A cost control that is only ever
validated on the distribution it was tuned on is not a cost control.

*Read the last column before the difference column.* The null spends what the
fitted router spends **on the fitting distribution** — that is what a real
deployment does, setting both from one month of logs and retuning neither. On
`control` the fitted router wins by 7.83 points while spending 0.91x, so that
number is a lower bound on what the estimator is worth. On `shift-90` it also
spends 1.67x more, so part of that +25.08 is simply money, and the row is not a
clean attribution claim. Every report prints both.

### 2. The cascade illusion: 99.96% answered, 77.38% correct

A self-validating cascade — call the cheap model, escalate if the response fails
validation — returns a **well-formed answer to 99.96%** of requests on the
control workload. It returns a **correct** one to **77.38%**.

**A gateway reporting success from response validity would claim near-perfect
service while being wrong 22.58% of the time.** Self-validation recovers only
the failures it can *detect*, and a confident model returns well-formed
nonsense. A real local model measured on these tasks returned **2 malformed
answers out of 180** — so in practice a cascade has almost nothing to catch, and
a gateway that sets `format: "json"`, as a production one would, makes the
illusion *worse* rather than better.

### 3. A circuit breaker is a failover mechanism, and failover needs capacity

Under a **total** outage of the cheapest provider, with the others healthy:

| Pool | single | retry | retry + breaker |
| --- | --- | --- | --- |
| 24 slots | 3.54% | 2.12% | **21.00%** |
| 96 slots | 98.00% | 99.25% | **99.62%** |

The breaker's *advantage* is far larger on the small pool (+18.88p vs +0.37p) —
which reads as an argument for breakers right up until the absolute numbers sit
beside it. **The same breaker serves a fifth of the traffic on a pool sized for
the fast provider and almost all of it on one sized for the slow.**

The mechanism is latency, not errors. Failing over moves the entire load onto a
provider six or thirty times slower, so the same arrival rate needs
proportionally more concurrency. A gateway that does not have it **dies on its
deadline, not on errors** — and the dashboards that would have caught it are
watching error rates.

Two corollaries, both measured:

* against elevated **independent** errors a breaker is a small consistent loss
  (-4.46 points at a 60% failure rate): it trips by coincidence and sheds
  traffic that would have succeeded;
* during a **partial** outage on the small pool it costs up to **-38.71 points**
  against plain retries.

A breaker is not a free safety net to switch on.

---

## Quick start

```bash
uv sync
uv run amg doctor            # the install works, and a replay reproduces
uv run amg models            # the catalogue, and it says it is a simulator
uv run amg ask "What is 12 + 30? Reply with JSON only, as {\"answer\": <value>}."
uv run amg route "What is 12 + 30?"   # where would this go, and why
```

Reproduce the findings:

```bash
uv run python examples/quickstart.py            # ~1 min
uv run python examples/routing_holdout_demo.py  # finding 1, ~1 min
uv run python examples/resilience_demo.py       # finding 3, ~3 min
python tasks.py evaluate                        # all of it, gated, ~10 min
```

Serve it:

```bash
docker compose up --build
curl localhost:8000/readyz
curl localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"cascade","messages":[{"role":"user","content":"What is 12 + 30? Reply with JSON only, as {\"answer\": <value>}."}]}'
```

Every completion carries an `amg` block naming the upstream that answered, the
policy's reason, the cost in micro-cents, and how many calls it took. A gateway
that hides which model answered is one nobody can debug.

## The policies

| Policy | What it does |
| --- | --- |
| `cheapest` | Always the cheap tier. The cost floor. |
| `best` | Always the capable tier. The quality ceiling — and it collapses under load. |
| `cascade` | Climb the ladder while the response fails validation. Finding 2. |
| `blend` | Route a fixed share at random. **The spend-matched null.** |
| `fitted` | The estimator, against calibrated thresholds. The subject. |

`fitted` and `blend` **refuse to be built** without their calibration rather than
falling back. A gateway that silently degrades to always-cheapest looks healthy,
costs less, and answers worse.

## How it is kept honest

* **241 tests** across five layers — unit, integration, security, e2e, meta — at
  **92.9%** coverage, `ruff` clean and `mypy --strict` clean.
* **A gate that compares against a recorded measurement**, not an absolute
  threshold. Six ways to fail, including the two people leave out: an arm the
  baseline has that this run did not measure, and an arm this run measured that
  the baseline has never heard of.
* **A test layer whose only job is to watch the gate go red.** `tests/meta/`
  breaks one thing at a time; `scripts/check-gateway.py` does the same against
  the shipped binary in CI. A gate that has only ever been observed passing is
  indistinguishable from `exit 0`.
* **Replay determinism is a hard refusal.** The experiment will not publish a
  result whose replay does not reproduce — exit 2 rather than a number that
  looks like a measurement and is different each run.
* **Everything on a decision path is an integer.** Money in micro-cents,
  accuracy as a per-ten-thousand table, latency in microseconds, estimator
  weights quantised. Nothing calls `exp` or `log`; Python's salted `hash` is
  never used.

## Documentation

| | |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | The shape, and seventeen decisions with what each was chosen over. |
| [Routing](docs/routing.md) | What the router sees, IRLS, calibration, the null. |
| [Resilience](docs/resilience.md) | The three questions, and the two artefacts this sweep had to be rewritten to avoid. |
| [The simulator](docs/simulator.md) | The table, why it is all integers, and how far it sits from real models. |
| [Upstreams and cost](docs/upstreams.md) | The protocol, and why money is an integer. |
| [The HTTP surface](docs/http.md) | Endpoints, limits, and what it deliberately lacks. |
| [Reproducibility](docs/reproducibility.md) | The virtual clock, the digests, the baseline. |
| [CI](docs/ci.md) | What runs, what each thing catches, and what is not in CI. |
| [Threat model](THREAT-MODEL.md) | What this is and is not defended against. |

## Requirements

Python 3.12 (pinned `>=3.12,<3.13`), [uv](https://docs.astral.sh/uv/). No API
keys, no accounts, no network — the whole experiment runs offline. The optional
Ollama integration talks to a local server and needs no credentials either.

## License

MIT. See [LICENSE](LICENSE).
