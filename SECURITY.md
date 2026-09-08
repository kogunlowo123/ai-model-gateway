# Security policy

## Reporting a vulnerability

Report privately through GitHub's [Security
Advisories](https://github.com/kogunlowo123/ai-model-gateway/security/advisories/new)
form. Please do not open a public issue for anything that would give someone
else a working exploit before there is a fix.

Include what you did, what happened, and what you expected. For anything
involving an input that causes unbounded work, the exact bytes are worth more
than a description — a size, a timing, and the command you ran.

Expect an acknowledgement within 5 working days and an assessment within 15.
This is a portfolio project maintained by one person, so those are honest
targets rather than a commercial SLA.

## What this project holds

Stated first, because it changes what a vulnerability here can be.

**No secrets.** No API keys, no tokens, no credentials, no secret store. The
simulated upstreams need none; the optional Ollama adapter talks to a local
server that has none. `.env.example` contains no credential-shaped variable, and
the container smoke test asserts none is baked into the image.

**No personal data.** Every prompt in every shipped workload is generated from a
fixed grammar. Nothing is scraped and nothing is user-contributed.

**No proprietary material.** The simulator's accuracy table is stipulated by
this project.

So there is nothing here to exfiltrate. What remains worth attacking is the
**integrity of the published numbers** and the **availability of a deployed
instance**.

## What counts as a vulnerability here

**In scope:**

* **Anything that makes a gate pass while the thing it gates is broken.** This
  is the most valuable class. A way to make `amg check` accept a corpus that
  does not match its plan; a way to make the baseline gate go green after a real
  regression; a path where `enforce` silently checks less than it claims.
* **Unbounded work from a bounded input.** The HTTP surface accepts
  attacker-supplied text, and `read_corpus` accepts attacker-supplied files. An
  input that drives time or memory superlinearly past the declared limits —
  `MAX_PROMPT_CHARS`, `MAX_CORPUS_BYTES` on the *decompressed* stream,
  `MAX_EVENTS` on the simulation — is a real finding. So is a way around any of
  those limits.
* **A path traversal or arbitrary write** through a corpus path, an output path,
  or an asset name.
* **Anything that makes the gateway execute or interpret model output.** A
  response is parsed as JSON and read for one key; it should never reach an
  evaluator, a template, a path, or a shell.
* **Container issues:** a way to run as root, to write to `/app`, or to escape
  the dropped capabilities in `docker-compose.yml`.
* **A secret committed anywhere in the history.** There should be none;
  gitleaks runs over the full history on every push and weekly. A finding would
  be a real one.

**Out of scope:**

* **The gateway has no authentication.** That is documented, deliberate, and
  bound to loopback by default — see [docs/http.md](docs/http.md) and
  [THREAT-MODEL.md](THREAT-MODEL.md) T5. "The endpoint is unauthenticated" is not
  a finding; a way to *bypass* something that is supposed to authenticate would
  be, and there is nothing here that claims to.
* **The absence of rate limiting**, for the same reason.
* **The simulator being a simulator.** That every published accuracy figure is a
  property of a stipulated table is the first thing the README says.
* **Ordinary evasion of nothing.** This project does not filter or detect
  anything; it is not a security control.
* **Findings from a tool run with no analysis.** A scanner's output is a
  starting point, not a report.

## What is scanned, and when

| Tool | Scope | When |
| --- | --- | --- |
| `gitleaks` | Working tree **and full git history** | Every push, every PR, weekly |
| `bandit` | `src/` | Every push, every PR, weekly |
| `pip-audit` | The exported locked dependency set | Every push, every PR, weekly |
| CodeQL | Python | Every push, every PR, weekly (public repositories only) |
| Trivy | The built container image, HIGH and CRITICAL | Every push, every PR |

Documented exceptions live in [`security/audit-exceptions.md`](security/audit-exceptions.md),
each with the finding, the reason, and what would make it stop being an
exception. An exception with no expiry condition is a finding nobody dealt with.

## Supported versions

The `main` branch. This is a portfolio project; there are no maintained release
branches.

## Deploying this safely

If you run the HTTP surface anywhere but a laptop:

* keep the loopback bind, and put an authenticating reverse proxy in front;
* keep `read_only`, `cap_drop: ALL` and `no-new-privileges` from
  `docker-compose.yml`;
* set `AMG_DEADLINE_US` and `AMG_CONCURRENCY` deliberately — they are what bound
  the work one client can cause;
* add rate limiting at the proxy. There is none here.
