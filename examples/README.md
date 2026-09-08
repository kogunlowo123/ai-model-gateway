# Examples and shipped artefacts

Two kinds of thing live here: **scripts that execute**, and **data the project
publishes numbers about**. Both are gated in CI, for different reasons.

## The scripts

They are documentation that runs. One that stops working is a README that lies,
so `ci.yml` runs all three on every push.

| Script | What it shows | Runtime |
| --- | --- | --- |
| `quickstart.py` | One request through an idle gateway, then the same corpus one-at-a-time, then the same corpus arriving at 40/second against 24 slots. The gap between the second and third tables is why every resilience number here is a loaded number. | ~1 min |
| `routing_holdout_demo.py` | Finding 1, small. Fit a router, calibrate it to a 3x budget, then measure it on disjoint and shifted traffic. Prints the spend ratio against the null next to every difference. | ~1 min |
| `resilience_demo.py` | Finding 3, small. One provider goes out for a share of every cycle, at two pool sizes. The 100% rows are the finding. | ~3 min |

Run any of them directly:

```
uv run python examples/quickstart.py
```

Their figures are close to, but not identical to, the ones in
`reports/evaluation.md` — they use smaller corpora so they finish while you
watch. **Quote the report, not the demos.**

## The workloads

| File | Plan | Size | Hard share | Role |
| --- | --- | --- | --- | --- |
| `fit.jsonl.gz` | `fit` | 4,800 | 30% | What the estimator and thresholds are fitted on. |
| `control.jsonl.gz` | `measure` | 2,400 | 30% | Held out. Same distribution, disjoint prompts. |
| `shift-10.jsonl.gz` | `shift-10` | 2,400 | 10% | Easier traffic. |
| `shift-50.jsonl.gz` | `shift-50` | 2,400 | 50% | |
| `shift-70.jsonl.gz` | `shift-70` | 2,400 | 70% | |
| `shift-90.jsonl.gz` | `shift-90` | 2,400 | 90% | Harder traffic. |

Every workload after `fit` is generated with the fitting corpus's prompts
**excluded at generation time**, not filtered afterwards. Filtering removes
samples unevenly across difficulties, so the corpus stops having the mix its
plan claims — and the difficulty mix is the thing the whole experiment varies.

They are checked, not diffed:

```
$ amg check --plan measure --corpus examples/control.jsonl.gz \
      --disjoint-from examples/fit.jsonl.gz
```

This re-derives the corpus from its plan and compares a digest over the task
fields. Comparing bytes would fail on gzip header fields no consumer reads;
comparing nothing would let a hand-edited prompt through, which is the fastest
way to make a baseline pass.

## The artefacts

| File | What it is |
| --- | --- |
| `estimator.json` | The fitted estimator: quantised integer weights, the target it predicts, and the digest of the corpus it was fitted on. |
| `baseline.json` | What this gateway actually did, per workload and per policy, plus the two resilience figures. The thing CI compares against. |
| `ollama/*.json` | A real local model measured on the same tasks, with 36 tasks in every (difficulty, family) cell. Evidence about the simulator's table; **not** part of any gate. |

`baseline.json` is re-recorded with `python tasks.py baseline` and should be
committed **on its own**, so the diff shows exactly what moved and why.

## What these numbers are, and are not

Both halves of the experiment are synthetic: the workload is generated and so is
the model that answers it. What is measured is the **routing and resilience
arithmetic**, not the quality of any real model. Every accuracy figure here is a
property of the table in `amg/upstream/simulated.py`.

See [docs/simulator.md](../docs/simulator.md), which records how far that table
sits from a real local model answering exactly these tasks -- including the one
place that comparison argues against the table rather than for it.
