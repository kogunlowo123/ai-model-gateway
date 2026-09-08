# Threat model

This is a **measurement harness with an HTTP surface**, not a security product.
Being explicit about that is the point of this document: a repository that
publishes a threat model implies defences, and the honest answer for most of the
usual boxes here is "not applicable, and here is why".

## What this system is

* A CLI that generates workloads, fits a router, replays corpora through a
  simulated gateway, and writes a gated report.
* An OpenAI-compatible HTTP surface over the same routing core.
* An optional adapter to a **local** Ollama server.

## What it holds

**No secrets.** There are no API keys, no tokens, no credentials, and no secret
store wired in. The simulated upstreams need none, and Ollama is local and
unauthenticated. `.env.example` contains no credential-shaped variable, and the
container smoke test asserts no credential-shaped variable is baked into the
image.

**No personal data.** Every prompt in every shipped workload is generated from a
fixed grammar of arithmetic, string and date puzzles. Nothing is scraped,
nothing is user-contributed, and no corpus here has ever contained a real
person's text.

**No proprietary material.** The simulator's accuracy table is stipulated by
this project, not derived from any provider's benchmark.

## Assets worth protecting

Ranked by what an attacker would actually gain.

| Asset | Why it matters |
| --- | --- |
| **The integrity of the published numbers** | The whole value of this repository. A number that is wrong and trusted is worse than no number. |
| **The host running the gateway** | Ordinary process and container hygiene. |
| **The availability of a deployed instance** | Only if somebody deploys it, which the documentation advises against doing unguarded. |

Note what is *not* on that list: confidentiality of the data flowing through.
There is nothing confidential in it.

## Trust boundaries

```
   operator (trusted)             any HTTP client (untrusted)
        |                                    |
     amg CLI                          amg.api.app
        |                                    |
        +--------- amg.gateway --------------+
                        |
        +---------------+----------------+
        |                                |
  simulated upstreams          local Ollama server
  (in process, trusted)        (untrusted output)
```

The only untrusted inputs are **an HTTP request body** and **a model's
response**. Both are treated as such.

## Threats, and what is done about each

### T1 — A tampered corpus makes the gate pass

**The most realistic attack on this repository, and the one it is built
against.** The fastest way to make a baseline pass is to edit the corpus it was
recorded against.

*Mitigated.* Every corpus is content-addressed by a digest over the task fields,
and `amg check` re-derives it from its plan. `DIGEST_FIELDS` is asserted equal to
the fields of `Task`, so a new field cannot quietly escape the digest.
`scripts/check-gateway.py` edits a corpus on purpose every CI run and fails if
the gate stays green.

### T2 — A silently weakened gate

A check that stops checking — an arm renamed, an arm dropped, a tolerance
widened — leaves a pipeline green while measuring less than it used to.

*Mitigated.* The baseline gate fails on an arm it records that this run did not
measure **and** on an arm this run measured that it has never heard of.
`tests/meta/` makes each of the six checks fire, after a green control.

### T3 — Unbounded work from one request

A gateway in front of a paid API is exactly where an unbounded request body
turns into an unbounded bill.

*Mitigated, after getting it wrong once.* `MAX_PROMPT_CHARS = 32,000`, refused
with `413` before any routing happens. The simulation carries a `MAX_EVENTS`
guard so a runaway feedback loop fails loudly instead of hanging a runner.

Corpus reads are bounded **twice**, and the second bound exists because the
first was not enough. `MAX_CORPUS_BYTES` caps the *decompressed stream*, since
checking `stat()` alone accepts a small file that expands to gigabytes. But **a
byte limit is not a memory limit**: every row that gets past the stream bound
becomes a `Task` object costing several hundred bytes for a line of about
seventy, so a bomb sized just under the byte limit allocated gigabytes of
objects and died with `MemoryError` *before* the byte bound was reached. The
symptom was precisely what the byte bound existed to prevent — an out-of-memory
kill that an operator reads as "the gate is flaky" rather than as a malformed
input.

`MAX_CORPUS_TASKS` now caps the row count, checked per row so the point is to
stop allocating rather than to report afterwards. Two tests hold the pair open:
one bomb with many small rows, which only the row limit catches, and one with
few enormous rows, which only the byte limit catches.

### T4 — Malicious model output

*Mitigated by construction.* A response is parsed as JSON and read for one key.
It is never evaluated, never rendered as HTML, never used to build a path, and
never logged unescaped. `parse_answer` returns `None` on anything unexpected,
and returning `None` is an ordinary measured outcome rather than an error path.

### T5 — Denial of service against a deployed instance

*Partly mitigated, and stated rather than claimed.* The prompt limit and the
end-to-end deadline bound per-request work, and the bounded pool bounds
concurrent work. There is **no rate limiting and no authentication**: this
server binds to loopback by default and the documentation says to put it behind
something that provides both. See [docs/http.md](docs/http.md).

### T6 — Supply chain

*Mitigated.* Dependencies are pinned by `uv.lock` and CI installs with
`--locked`. `pip-audit` runs against the exported locked set on every push and
weekly. Dependabot is configured, with a documented ignore rule for major and
minor bumps of the `python` base image, since the project pins
`requires-python = ">=3.12,<3.13"` and such a bump produces an image the locked
set refuses to install.

### T7 — Secrets committed by accident

*Mitigated.* gitleaks runs over the working tree **and the full git history** on
every push and weekly. There are no secrets to leak, which makes this a guard
against future mistakes rather than a current need.

### T8 — Container escape or privilege escalation

*Mitigated.* The image runs as a non-root user with no login shell and no home
directory. `docker-compose.yml` sets `read_only`, drops all capabilities, and
sets `no-new-privileges`. Trivy scans the image at HIGH and CRITICAL on every
push. The smoke test asserts the container does not run as root and cannot
write to `/app`.

### T9 — A dishonest result from the maintainer

The threat this repository takes most seriously, because it is the one a reader
cannot check by running the code once.

*Mitigated by design rather than by policy.* The subject is compared against a
**spend-matched null** that can beat it. Realised spend ratios are published
next to every difference so a reader can see which rows are clean attribution
claims and which are not. The optimism gap between fitted and held-out traffic
is published rather than only the better number. Replay non-determinism is a
refusal, not a warning. The simulator's limits, including the two that cap every
finding, are on the front page in the same section as the headline.

## Explicit non-goals

* **This is not a security gateway.** It does not filter prompts, detect
  injection, or enforce policy on model output. A different project in this
  series does that.
* **It does not authenticate anyone.** See T5.
* **It does not protect against a malicious operator.** Anyone who can edit
  `examples/baseline.json` and the corpora together can publish any number they
  like. The gates make that a deliberate act with a visible diff rather than an
  accident.

## Reporting

See [SECURITY.md](SECURITY.md).
