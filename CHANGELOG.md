# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-08

First release. A model gateway that measures whether its own routing and its own
retries are worth having.

### The findings

Three, all measured against nulls chosen so the answer could have come back
"no". Full numbers in `reports/evaluation.md`; the caveats that cap all three
are on the front page of the README.

* **The router's quality generalises; its budget does not.** The optimism gap
  between the fitting workload and a disjoint one is +0.46 points. The same
  thresholds, calibrated once to a 3x spend budget, realise 1.87x on easy
  traffic and 5.10x on hard. A threshold is a decision boundary, not a budget.
* **The cascade illusion.** A self-validating cascade answers 99.96% of requests
  and is correct on 77.38%. A gateway reporting success from response validity
  would claim near-perfect service while being wrong 22.58% of the time.
* **A circuit breaker is a failover mechanism, and failover needs capacity.**
  Under a total outage of the cheapest provider the same breaker serves 21.00%
  of traffic on a 24-slot pool and 99.62% on a 96-slot one. During a partial
  outage on the small pool it is actively harmful, costing up to 38.71 points
  against plain retries.

### Security

* Upgraded `fastapi` to 0.141 and pinned `starlette>=1.3.1` **directly**
  rather than through fastapi, which only asks for `>=0.46.0` and left a
  resolver free to keep 0.50.0 with five known advisories against it
  (PYSEC-2026-161, -248, -249, -2280, -2281).

### Added

* **Routing** — a logistic estimator over prompt features, fitted by IRLS, with
  weights quantised to fixed point. Threshold calibration to a spend budget by
  prefix-sum grid search. Five policies: `cheapest`, `best`, `cascade`, `blend`
  (the spend-matched null) and `fitted`.
* **Resilience** — retry with backoff, consecutive-failure circuit breakers with
  half-open probes, a bounded concurrency pool, and an end-to-end deadline that
  covers queueing as well as the call.
* **A discrete-event simulator** — integer virtual time, a total event order, a
  runaway guard, and slot abandonment when a queued request passes its deadline.
* **Workload generation** — six task families across five difficulty bands,
  content-addressed corpora, generation-time exclusion so held-out workloads are
  disjoint by construction rather than by filtering.
* **Evaluation** — counterfactual replay, McNemar's exact test on paired
  correctness, a paired bootstrap on cost, Wilson intervals, cost–quality
  dominance, and a two-fault three-arm two-capacity resilience sweep.
* **A baseline gate** with six failure modes, including an arm the baseline has
  that a run did not measure and an arm a run measured that the baseline has
  never heard of.
* **`amg` CLI** — `synth`, `check`, `models`, `fit`, `calibrate`, `route`, `ask`,
  `evaluate`, `resilience`, `doctor`, `serve`. Four distinct exit codes.
* **An OpenAI-compatible HTTP surface** with `/healthz`, `/readyz`, `/v1/models`
  and `/v1/chat/completions`, each completion carrying an `amg` provenance block.
* **An Ollama adapter** for measuring a real local model against the simulator's
  table, credential-free and deliberately off the experiment path. What it found
  is reported in `docs/simulator.md`, including that the generated difficulty
  scale separates a real small model far less sharply than it separates the
  simulated tiers — the one place the comparison argues against the table.
* **241 tests** across five layers at 92.9% coverage, including a `meta` layer
  whose only job is to watch each gate go red.
* **`scripts/check-gateway.py`** — breaks one thing at a time against the shipped
  binary and fails if the exit code comes back green.
* Container image, compose file with a read-only root and dropped capabilities,
  a smoke test covering both the CLI and the HTTP surface, four CI workflows, and
  a published documentation site.

### Notable decisions

Recorded in full in [ARCHITECTURE.md](ARCHITECTURE.md).

* A discrete-event simulation rather than `asyncio`, because resilience results
  are concurrency results and real sleeps make a replay unreproducible.
* Money as integer micro-cents, rounded up, parsed from decimal strings.
* Nothing on a decision path touches a float, and Python's salted `hash` is
  never used.
* `Attempt` carries no `correct` field, so a routing policy cannot read
  correctness by accident.
* The spend-matched null is sized once, on the fitting workload, and the realised
  spend ratio is published next to every difference.
* Policies refuse rather than degrade.

### Fixed during development

Kept because each was a real defect that a plausible implementation would ship.

* **A per-attempt timeout cannot see a queue.** With only `timeout_us`, the p99
  reached 9,023 ms against a 6,000 ms budget while the error rate stayed flat.
  Added an end-to-end deadline and slot abandonment.
* **A permanently wedged circuit breaker.** A half-open probe was granted, its
  call was never made because the request's deadline expired while queued, so
  `probe_in_flight` stayed set and every subsequent call was rejected for the
  rest of the run. Added `release_probe()` on the abandoned-slot path.
* **`extra="forbid"` does not catch a misspelt prefixed environment variable.**
  `AMG_CONCURENCY=48` constructed cleanly with concurrency still 24. Added
  `Settings.from_environment()`, and a test pinning the pydantic behaviour that
  makes it necessary.
* **gzip stores a filename in its header.** The same corpus written to two paths
  produced different bytes. Set `filename=""` alongside `mtime=0`.
* **The estimator did not converge** in 4,000 gradient-descent iterations.
  Replaced with IRLS: six Newton steps to a stated tolerance.
* **A breaker sweep with no survivor.** The first outage model took every
  provider down at once, leaving failover nowhere to go, and made breakers look
  useless. The outage now hits one provider.
* **A decompression bomb that OOM-killed the process instead of being refused.**
  The stream bound capped decompressed *bytes*, but the parsed `Task` objects
  cost several hundred bytes for a seventy-byte line, so a bomb sized just under
  the byte limit raised `MemoryError` before the bound was reached. Added
  `MAX_CORPUS_TASKS`, checked per row. Found by the project's own test, which
  had been passing on a machine with enough spare memory.
* **A documentation-site link checker that could not see a typo.** A path that
  matched no page became a well-formed GitHub URL to a file that does not exist,
  and the checker only looked at links that stayed relative. It now verifies the
  target exists in the repository.
* **A shadowed name in the resilience gate**, caught by `mypy --strict` once the
  `examples/` directory existed and mypy stopped exiting early on it.
* **Family-confounded difficulty bands** in the Ollama measurement. Taking the
  first N tasks of each band gave each band a different family mix, which showed
  a 3B model scoring 65% at difficulty 1 and 95% at difficulty 2 — a fact about
  the sample, not the model. The sampler now balances every (difficulty, family)
  cell, and the three confounded runs are not reported: one clean measurement is
  worth more than three that are not, and publishing them would have undercut
  the only thing that section is for.

[Unreleased]: https://github.com/kogunlowo123/ai-model-gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kogunlowo123/ai-model-gateway/releases/tag/v0.1.0
