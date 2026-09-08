# Contributing

Thanks for taking the time to contribute. This document describes the local
workflow, the quality bar enforced in CI, and the one rule that is specific to
this project.

## The rule that is specific to this project

**A change that moves a published number must move the baseline in its own
commit, with the reason in the message.**

`examples/baseline.json` is not a lockfile to be regenerated when it goes red.
It is the record of what this gateway did, and the gate exists so that a change
in behaviour is something a human decided to accept. If your change legitimately
improves a figure, re-record and say so. If it legitimately makes one worse, say
that instead — this repository publishes its own bad results on the front page,
and a pull request that quietly widens a tolerance is the failure mode the whole
thing argues against.

## Prerequisites

- Python 3.12 (the project pins `>=3.12,<3.13`)
- [uv](https://docs.astral.sh/uv/) 0.10 or newer
- Docker (optional, only needed for container work)
- Ollama (optional, only for `scripts/measure-ollama.py`)

`make` is convenient on Linux and macOS but is not required. `python tasks.py`
is the cross-platform entry point and the source of truth; the `Makefile`
delegates to it.

## Getting set up

```bash
uv sync --group dev --group docs     # or: python tasks.py setup
cp .env.example .env
```

Never commit a populated `.env`. `.gitignore` excludes it and CI runs a secret
scan over both the working tree and the full git history.

## Development loop

| Task | Command |
| --- | --- |
| Format | `python tasks.py fmt` |
| Lint and format check | `python tasks.py lint` |
| Types | `python tasks.py typecheck` |
| Tests with the coverage gate | `python tasks.py test` |
| One test layer | `python tasks.py test-unit` (also `-integration`, `-security`, `-e2e`, `-meta`) |
| The gated experiment | `python tasks.py evaluate` |
| Watch the gates fire | `python tasks.py check-gateway` |
| Run the examples | `python tasks.py examples` |
| Build the docs site | `python tasks.py site` |
| Local security scans | `python tasks.py security` |
| Build and smoke-test the image | `python tasks.py smoke` |
| **Everything CI runs, in order** | `python tasks.py all` |

`python tasks.py` with no argument lists every task and what it does.

Run `python tasks.py all` before opening a pull request. It runs the cheapest
gate first, so a broken import fails in seconds rather than after the evaluation.

## The test layers

Five markers, and each exists because the others cannot see what it sees.

| Layer | What only it can see |
| --- | --- |
| `unit` | Pure logic. Money arithmetic, the clock's ordering, the statistics. |
| `integration` | Several components against real temporary files. Also the CLI **in process**, which is the only way coverage sees it. |
| `security` | Adversarial inputs: oversized prompts, decompression bombs, misspelt configuration. A failure here is a security regression. |
| `e2e` | The CLI and the HTTP surface as **real processes**. Exit codes and console encoding are invisible in process. |
| `meta` | The gate's own negative controls. Break one thing, assert it goes red. |

`e2e` and `integration` deliberately drive the same CLI commands. Neither
substitutes for the other: a subprocess is a different interpreter, so coverage
cannot see it, and an in-process call cannot see the exit code the shell
receives. An earlier project in this series left its CLI at 0% coverage for
exactly that reason.

## What CI enforces

* `ruff check`, `ruff format --check`, `mypy --strict` — all clean, no
  exceptions without a comment saying why.
* Every test layer, then the full suite at **88%** coverage minimum.
* The committed workloads still match their plans and are still disjoint.
* The routing experiment and the resilience sweep, gated against the baseline.
* `scripts/check-gateway.py` — the gates still fire.
* All three examples still run.
* The wheel builds, installs, and its console script works.
* The documentation site builds with no broken internal link.

See [docs/ci.md](docs/ci.md) for what each of those catches.

## Style

* **Comments say why, not what.** A comment that restates the line above it is
  noise; a comment explaining why the obvious approach was rejected is the most
  valuable thing in the file. Several here record a measurement that changed a
  decision — keep that shape.
* **Docstrings on everything public**, Google convention, enforced by ruff.
* **No new floats on a decision path.** See
  [ADR-003](ARCHITECTURE.md#adr-003--nothing-on-a-decision-path-touches-a-float).
  If you need one, the change needs an ADR.
* **No `random` and no `hash` for anything reproducible.** Key it on BLAKE2b.
* **Refuse rather than degrade.** If a component cannot do what it was asked,
  raise a `GatewayError` with a `remedy` that says what to run. Falling back to
  something that works is how a gateway ends up healthy, cheap and wrong.

## Adding a policy, an upstream, or a fault model

Each has one place to change and one place to prove it:

* **A policy** — implement `Policy` in `amg/routing/policies.py`, add it to
  `build()` and `POLICY_NAMES`. It must refuse to be constructed without
  whatever calibration it needs. Add it to the experiment only after deciding
  what it is a null *for*.
* **An upstream** — implement the `Upstream` protocol. If it is not
  deterministic, it does not go on the experiment path; see
  [ADR-015](ARCHITECTURE.md#adr-015--the-ollama-adapter-exists-and-is-not-on-the-experiment-path).
* **A fault model** — add it to `amg/evaluate/resilience.py`. State what it
  models that the existing two do not, because "independent errors" and "one
  provider down" are already different enough to give opposite answers about
  breakers.

## Commits and pull requests

Conventional-commit prefixes (`feat`, `fix`, `docs`, `test`, `build`, `ci`,
`refactor`, `perf`, `chore`). Keep the baseline in its own commit.

In the pull request, say what you measured. "Should be faster" is not a claim
this repository can accept from itself, and it will not accept it from a
contributor either.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
