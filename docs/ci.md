# Continuous integration

Four workflows. The argument this repository makes about gates applies to its
own pipeline first: **every check here can fail, and one of them exists purely
to prove the others can.**

## What runs, and what each one catches

### `ci.yml`

| Job | Fails when |
| --- | --- |
| **quality** | `ruff check`, `ruff format --check` or `mypy --strict` finds anything, or the documentation site has a broken internal link. |
| **test** (5 legs) | Any of `unit`, `integration`, `security`, `e2e`, `meta` fails. One leg per declared marker — a layer that exists and is never selected is a layer nobody runs. |
| **coverage** | The full suite drops below 88% line-and-branch coverage. |
| **gates** | The measurement itself regresses. See below. |
| **examples** | Any of the three example scripts stops working. They are documentation that executes; one that breaks is a README that lies. |
| **build** | The wheel does not build, or builds and does not install, or installs and its console script does not run. |

The test matrix legs run **without** the coverage flag. A leg that inherited the
coverage gate would fail on every leg but the full one, for a reason that has
nothing to do with the layer it is reporting on.

### The `gates` job in detail

This is the one that makes CI mean something.

1. **`amg doctor`** — the installation works end to end, and a replay reproduces
   its digest.
2. **Every committed workload still matches its plan**, and every workload after
   the first is still disjoint from the fitting corpus. Not "regenerate and `git
   diff`": that compares bytes, and gzip carries header fields no consumer
   reads. This compares the digest over the task fields.
3. **The routing experiment and the resilience sweep**, gated against
   `examples/baseline.json`. Six ways to fail, listed in
   [reproducibility](reproducibility.md#the-baseline-and-what-it-gates). It also
   refuses to publish at all if the replay does not reproduce — exit 2 rather
   than a number that looks like a measurement and is different each run.
4. **`scripts/check-gateway.py`** — breaks one thing at a time against the
   shipped binary and fails if the exit code comes back green.

Step 4 is the point. A pipeline whose gates have only ever been observed passing
is indistinguishable from `exit 0`, and the difference is invisible until the
day something regresses. It runs a green control first, so a red result cannot
be a fact about a broken environment.

The full evaluation report is written to the job summary and uploaded as an
artefact on every run, pass or fail.

### `security.yml`

Bandit, pip-audit against the locked dependency set, gitleaks over the full
history, and CodeQL. CodeQL is guarded on the repository being public, because
the free tier does not run it otherwise and a permanently-skipped job is worse
than no job.

### `docker.yml`

Builds the image, scans it with Trivy at HIGH and CRITICAL, asserts it does not
run as root, and runs `scripts/smoke-test.sh` against it.

The smoke test is the only thing in the pipeline that tests **what actually
ships**. It exercises both faces of the image — the CLI as one-shot runs, the
HTTP surface as a container polled over a port — re-derives the shipped
workloads inside the image, and asserts the two policy refusals still refuse.

It exists because of a specific defect class every other gate is blind to: the
image builds, starts, and is broken because something the code needs is not in
it. The usual one is an editable install leaking into the runtime stage, where
the `.pth` file points at a `/build` directory that does not exist in the final
image. There is an explicit assertion for exactly that.

### `pages.yml`

Builds the documentation site from the Markdown already in the repository and
publishes it. There is exactly one copy of every sentence: the site has no
content of its own, because a published page that has drifted from the README is
worse than no published page.

The builder fails on a broken internal link, which is why it runs in the
`quality` job too — a renamed page fails the pull request rather than shipping a
404.

## Exit codes

The gateway distinguishes four, and the distinction is load-bearing for a
pipeline:

| Code | Meaning |
| --- | --- |
| 0 | The gate held. |
| 1 | Usage error — a bad flag, a missing file, a policy that cannot be built. |
| 2 | A gate failed. Something regressed. |
| 3 | Could not run. Nothing was measured. |

`argparse` exits **2** on a usage error by default, which collides with "a gate
failed". Without the `_Parser` override, a misspelt flag and a routing
regression would be the same number to a pipeline — and a pipeline that cannot
tell "you typed the path wrong" from "quality regressed" will eventually be
taught to ignore both. `tests/e2e` asserts the override from a real subprocess,
which is the only place an exit code is visible.

## Running the same thing locally

```
python tasks.py all
```

Runs what CI runs, in the order CI runs it: cheapest gate first, so a developer
who broke an import learns that before the suite has finished collecting.

```
python tasks.py            # every task, with what it does
python tasks.py lint
python tasks.py test
python tasks.py evaluate
python tasks.py check-gateway
python tasks.py smoke      # builds the image and smoke-tests it
python tasks.py security   # bandit and pip-audit
```

## What is not in CI, and why

* **`scripts/measure-ollama.py`.** It needs a local model server with specific
  models pulled, and its results move with quantisation and hardware. Wiring it
  in would produce a check that fails on other people's machines for reasons
  they cannot act on, which is how a suite gets ignored. It is run by hand and
  its output is committed to `examples/ollama/`.
* **A performance benchmark.** Every latency figure here comes from a virtual
  clock, so there is nothing meaningful for a runner to time.

## See also

* [Reproducibility](reproducibility.md) — what the gates are checking against.
* [CONTRIBUTING.md](../CONTRIBUTING.md) — what to run before opening a pull
  request.
