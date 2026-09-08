# Reproducibility

Every number in this repository can be re-derived from the code and the
committed workloads, on any machine, without a network, a model server, or an
API key. This page is how, and what the guarantee does and does not cover.

```
$ python tasks.py workloads-check   # the corpora still match their plans
$ python tasks.py evaluate          # re-measure, gated against the baseline
$ python tasks.py check-gateway     # the gates themselves still fire
```

## A virtual clock, not `asyncio`

The resilience results are concurrency results, and concurrency measured against
a real clock is not reproducible. Two runs of the same `asyncio` program
interleave differently, so a queueing number becomes a property of the machine,
the scheduler and the load next to it.

So `amg.clock` is a **discrete-event simulation**. Time is an integer count of
microseconds; events live in a heap keyed `(time, priority, sequence)`, which is
a **total** order — no two events can tie, and therefore no two runs can order
them differently. `Simulation` carries a `MAX_EVENTS` guard so a runaway
feedback loop fails loudly rather than hanging a CI runner.

The consequence worth stating: **these are not wall-clock benchmarks.** A
latency figure here is what the model of the system says, not what a laptop did.
What it buys is that the queueing behaviour under retry amplification — the
thing actually under study — is identical everywhere.

## Nothing on a decision path touches a float

libm's transcendental functions differ by one unit in the last place between
platforms. This simulation makes ordering decisions on latency and threshold
decisions on accuracy, so a single flipped comparison changes which upstream won
a race, which changes the whole event schedule after it.

Accuracy is an integer table; latency is integer microseconds; estimator weights
are quantised to fixed point; money is integer micro-cents. Nothing calls `exp`
or `log` on the decision path. See
[the simulator](simulator.md#everything-on-the-decision-path-is-an-integer).

Floats appear in exactly two places, both off the decision path: fitting the
estimator (whose output is then quantised), and the statistics in the report.

## Every draw is keyed on BLAKE2b

**Python's `hash` is never used.** It is salted per process, so a corpus scored
in one interpreter and replayed in another would take different branches and
produce different numbers, with nothing raising.

An upstream's response is a pure function of `(task, upstream, nonce, at_us)`
through a BLAKE2b digest. That is what makes **counterfactual replay** possible:
any request can be re-served under a different policy, and the answer the other
policy *would* have received is recoverable. Every paired statistic in the
report — McNemar's test on correctness, the paired bootstrap on cost — depends
on that property.

## The replay gate

`amg.replay.verify_determinism` serves a workload twice and compares digests.
The digest covers what the gateway **chose** and what it **spent**, not latency,
so a change to the arrival process shows up as a different schedule rather than
as a spurious routing difference.

`run_experiment` refuses to publish a result whose replay does not reproduce.
That refusal is a hard failure, not a warning: a non-reproducing experiment is
not a slightly weaker experiment, it is a different one each time it runs.

`amg doctor` runs the same check, which is why it is also the container's
`HEALTHCHECK`.

## The workloads are content-addressed

Each committed corpus carries a digest over the task fields a consumer actually
reads, and `amg check` re-derives the corpus from its plan and compares. Editing
a prompt by hand fails the check — which matters, because **the fastest way to
make a baseline pass is to change the corpus it was recorded against**.
`scripts/check-gateway.py` breaks a corpus on purpose every run and fails if the
gate stays green.

`DIGEST_FIELDS` is asserted equal to the fields of `Task`, so adding a field
without adding it to the digest is a test failure rather than a silent blind
spot.

### The gzip header is part of the bytes

Two traps, both hit here:

* without `mtime=0`, the same corpus written twice differs by a timestamp;
* without `filename=""`, it differs by the **path**, because `GzipFile` takes the
  `FNAME` header from the file object it is handed. The same corpus written to
  `a.jsonl.gz` and `b.jsonl.gz` produced different bytes.

Both are set, and a test writes the same corpus to two different names and
compares bytes.

## The baseline, and what it gates

`examples/baseline.json` holds what this gateway actually did, per workload and
per policy, on a specific pair of corpora at a specific budget. Six things fail
the build:

1. correctness regressed on any workload for any policy;
2. cost grew on any workload for any policy;
3. the estimator's advantage over the spend-matched null shrank;
4. a workload or policy in the baseline that this run **did not measure** — a
   silently dropped arm;
5. a workload or policy this run measured that the baseline has **never heard
   of** — a renamed arm that stopped being gated;
6. the recorded resilience figures moved down.

Items 4 and 5 are the ones people leave out, and they are why a gate that has
only ever been observed passing is indistinguishable from `true`.

`tests/meta/test_the_gate_can_fail.py` breaks one thing at a time and asserts
each check goes red, with a green control run first so a red result cannot be a
fact about a broken environment.

## What is *not* reproducible, and is not meant to be

* **`amg.upstream.ollama`.** Real models are not bit-reproducible across builds,
  quantisations or drivers. Nothing measured through it is in any baseline; see
  [the simulator](simulator.md#how-far-the-table-sits-from-real-models).
* **Wall-clock timings** of the tooling itself.
* **The grammar.** The tasks come from a fixed generator, and the estimator's
  features are the ones that generator makes predictive. Held-out *traffic* is
  measured here; a held-out *grammar* is not, and cannot be. That is the ceiling
  on the whole evaluation and it is stated on the front page.

## See also

* [Continuous integration](ci.md) — which of these run on every push.
* [The simulator, and its limits](simulator.md)
