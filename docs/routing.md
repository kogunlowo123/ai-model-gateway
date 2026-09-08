# Routing and the estimator

The router's job is to answer one question per request, before the request is
served: **will the cheapest model get this right?** If the answer is confidently
yes, send it to the cheap one. If confidently no, skip straight to the capable
one. In between, take the middle tier.

That framing is deliberate and it is not the obvious one. The obvious framing is
"how hard is this task", which sounds like the same question and is not: task
difficulty is a property of the task, and what a gateway needs is a property of
the *pairing* between the task and the model it is about to pay for.

## What the router is allowed to see

The estimator scores the prompt string. Nothing else.

```
$ amg route "What is 478 + 626? Reply with JSON only."
policy   cascade
ladder   nano -> flagship
reason   cheapest first, escalate on malformed output
features (everything the router is allowed to see):
  chars           40
  words            6
  digits           6
  longest_number   3
  distinct_words   6
  punctuation      2
  braces           0
  quotes           0
```

Every feature is a clipped integer count over the prompt. There is no lookup of
the correct answer, no field carried over from the corpus, and no access to what
any upstream did. `Attempt` has no `correct` field for the same reason: a
routing policy that could read correctness at request time would look brilliant
and be undeployable.

That constraint is what makes `amg route` worth having as a command. It prints
the features it extracted, so the question "what did the router actually know
here" has an answer that is not a guess.

## Fitting: IRLS, not gradient descent

The model is plain logistic regression, predicting whether `nano` answers
correctly, fitted by **iteratively reweighted least squares** — Newton's method
on the log-likelihood.

The first implementation was heavy-ball gradient descent and it did not
converge: at 4,000 iterations the gradient norm was still 3.9e-6 at a learning
rate of 0.5, and higher rates oscillated. Sweeping learning rates to rescue a
convex problem is a sign of using the wrong solver, not of needing more
patience. IRLS reaches the same optimum in **six Newton steps and about a fifth
of a second**, and it converges to a stated tolerance rather than to an
iteration budget — which means "converged" is a fact rather than a hope.

Ridge regularisation is applied to the slopes only, never to the intercept.
Penalising the intercept shifts the base rate, which is not a thing that needs
shrinking.

The fitted float weights are then **quantised to fixed point** (`WEIGHT_SCALE =
1 << 20`) so scoring is integer arithmetic. See
[the simulator](simulator.md#everything-on-the-decision-path-is-an-integer) for
why nothing on the decision path may touch a float.

## Calibration: the thresholds are a budget, not a boundary

`amg calibrate` chooses the two score thresholds that answer the most requests
correctly while spending no more than a stated multiple of what always-cheapest
spends. It does this by sorting scores and sweeping a prefix sum, so the result
is the exact optimum over the grid rather than a hill-climb.

It also prints a warning about its own number:

```
$ amg calibrate --corpus examples/fit.jsonl.gz --estimator examples/estimator.json
calibrated at 3x the cheapest policy's spend
  AMG_THRESHOLD_HIGH=779933
  AMG_THRESHOLD_LOW=-2676792
  AMG_BLEND_TO_CHEAP=8847
  AMG_BLEND_TO_MIDDLE=625
expected on the calibration workload: 82.02% correct
That figure is optimistic: it is measured on the workload it was fitted to.
```

That warning is the first finding in miniature. **The thresholds are calibrated
to hit a spend target on one difficulty mix, and a threshold is a decision
boundary, not a budget.** Hand the same boundary a harder mix and more requests
land above it, so the realised spend runs past the target that nobody
re-checked:

| Workload | Hard share | Realised spend vs cheapest |
| --- | --- | --- |
| `fit` | 30% | 2.76x |
| `control` | 31% | 2.72x |
| `shift-10` | 9% | 1.87x |
| `shift-50` | 50% | 3.62x |
| `shift-70` | 69% | 4.33x |
| `shift-90` | 91% | **5.10x** |

All six rows used the *same* thresholds, calibrated once to a 3x budget. The
router's **quality** transfers — the optimism gap between `fit` and `control` is
only +0.46 points, which is what a well-behaved fitted model looks like. Its
**budget** does not.

## The spend-matched null

A router that spends 2.7x what the cheapest policy spends should be more correct
than the cheapest policy. That comparison is not interesting. The question is
whether it is more correct than **spending 2.7x at random**.

`blend` is that null: route a fixed share of traffic to each tier by an
integer-probability draw, with the shares sized so the total spend matches. Same
money, no estimator. Every headline difference in this repository is measured
against it, by McNemar's exact test on paired per-request correctness.

On the control workload the fitted router beats it by **+7.83 points**
(p = 1.5e-24) while spending **0.91x** what it spends — so on held-out traffic
from the same distribution, the estimator is worth about eight points of
correctness *and* a small saving.

The null's shares are sized **once**, against the fitted router's spend on the
fitting workload. That is what a real deployment does: both are set from the
same month of logs and neither is retuned afterwards. It also means the spend
match is exact on that distribution and drifts off it — on `shift-90` the fitted
router spends 1.67x what the null spends, so part of that row's +25.08 points is
simply the extra money. Every report prints the ratio next to the difference for
exactly that reason, and rows above 1.00x are not clean attribution claims.

Recalibrating the null per workload would pin every ratio at one and break
something worse: the null would be a different policy on every row, and nothing
could be compared across rows.

## The refusals

`build()` refuses to construct a `fitted` policy without an estimator and
calibrated thresholds, and a `blend` without calibrated shares. Both refusals
are also asserted by `scripts/check-gateway.py` against the shipped binary and
by the container smoke test.

The reason is narrow and worth stating. A gateway that silently degrades to
"always cheapest" when its estimator fails to load **looks healthy, costs less,
and answers worse**, and the only symptom is a quality number nobody is
watching. Falling back is the failure mode that presents as a cost saving.

## See also

* [The simulator, and its limits](simulator.md) — what the accuracy numbers are.
* [Retries, breakers, capacity](resilience.md) — the same gateway under load.
* `examples/routing_holdout_demo.py` — the finding above, in under a minute.
