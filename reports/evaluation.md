# Gateway evaluation

## The router's quality generalises; its budget does not

Thresholds were calibrated on the fitting workload to spend **3x** what always-cheapest spends.

* Fitting optimism: **+0.46 points** of correctness between the workload the router was fitted on and a disjoint one from the same distribution.
* Worst realised spend across the shifted workloads: **5.10x** the cheapest policy, against a 3x budget.

**Read the last column before reading the difference column.** The spend-matched null's traffic shares are sized once, against the fitted router's spend on the fitting workload, because that is what a real deployment does: both are set from the same month of logs and neither is retuned afterwards. So the match is exact on that distribution and drifts off it, and how it drifts decides how a row may be read.

* **At or below 1.00x** the fitted router bought its advantage with the same money or less, and the difference is attributable to the estimator. Below one it is a lower bound on what the estimator is worth, not a higher one.
* **Above 1.00x** the fitted router also spent more, and part of the difference is simply the extra money. Those rows say the router escalates harder on harder traffic -- which is the first finding -- and they are not clean attribution claims.

Recalibrating the null per workload would pin every ratio at one and break something worse: the null would be a different policy on every row, and nothing could be compared across rows.

| Workload | Hard | Fitted | Null | Difference | p | Spend vs cheapest | Spend vs null |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `fit` | 30% | 81.42% | 73.42% | +8.00p | 1.0e-50 | 2.76x | 0.99x |
| `control` | 31% | 80.96% | 73.12% | +7.83p | 1.5e-24 | 2.72x | 0.91x |
| `shift-10` | 9% | 85.88% | 83.79% | +2.08p | 7.7e-05 | 1.87x | 0.70x |
| `shift-50` | 50% | 76.33% | 62.75% | +13.58p | 8.8e-46 | 3.62x | 1.28x |
| `shift-70` | 69% | 67.96% | 52.29% | +15.67p | 2.1e-46 | 4.33x | 1.49x |
| `shift-90` | 91% | 65.17% | 40.08% | +25.08p | 1.0e-87 | 5.10x | 1.67x |

## Every policy, on the control workload

| Policy | Correct | 95% interval | Answered | Answered but wrong | Cost | Dominated by |
| --- | --- | --- | --- | --- | --- | --- |
| `cheapest` | 70.83% | 68.98% - 72.62% | 92.96% | 22.12% | $0.01392170 | - |
| `best` | 96.08% | 95.23% - 96.79% | 99.50% | 3.42% | $0.42572700 | - |
| `cascade` | 77.38% | 75.66% - 79.00% | 99.96% | 22.58% | $0.04596470 | `fitted` |
| `blend` | 73.12% | 71.32% - 74.86% | 93.58% | 20.46% | $0.04178100 | `fitted` |
| `fitted` | 80.96% | 79.34% - 82.48% | 96.71% | 15.75% | $0.03782290 | - |

**The cascade illusion.** `cascade` returns a well-formed answer to 99.96% of requests and a *correct* one to 77.38%. A gateway reporting success from response validity alone would claim near-perfect service while being wrong 22.58% of the time: self-validation recovers only the failures it can detect.

## A circuit breaker is a failover mechanism, and failover needs capacity

Under a total outage of the cheapest provider, the same breaker serves **21.00%** of requests on a pool sized for the fast provider and **99.62%** on one sized for the slow one.

During a *partial* outage on the small pool the breaker is actively harmful, costing up to **-38.71 points** against plain retries: it fails traffic over to a provider that cannot absorb it, and the gateway then dies on its deadline rather than on errors.

| Fault | Level | Pool | single | retry | retry+breaker |
| --- | --- | --- | --- | --- | --- |
| flaky | 0% | 24 | 99.88% | 99.96% | 99.96% |
| flaky | 10% | 24 | 98.12% | 99.96% | 99.96% |
| flaky | 20% | 24 | 94.71% | 99.46% | 99.46% |
| flaky | 40% | 24 | 60.33% | 83.83% | 83.62% |
| flaky | 60% | 24 | 39.58% | 61.67% | 57.21% |
| outage | 0% | 24 | 99.83% | 99.96% | 99.96% |
| outage | 10% | 24 | 99.62% | 99.83% | 74.42% |
| outage | 25% | 24 | 95.75% | 95.62% | 74.42% |
| outage | 50% | 24 | 71.00% | 79.62% | 40.92% |
| outage | 100% | 24 | 3.54% | 2.12% | 21.00% |
| flaky | 0% | 96 | 99.88% | 99.96% | 99.96% |
| flaky | 10% | 96 | 98.12% | 99.96% | 99.96% |
| flaky | 20% | 96 | 94.71% | 99.46% | 99.46% |
| flaky | 40% | 96 | 82.25% | 96.54% | 96.33% |
| flaky | 60% | 96 | 62.38% | 85.67% | 81.04% |
| outage | 0% | 96 | 99.83% | 99.96% | 99.96% |
| outage | 10% | 96 | 99.54% | 99.88% | 99.88% |
| outage | 25% | 96 | 99.25% | 99.67% | 99.75% |
| outage | 50% | 96 | 98.83% | 99.46% | 99.71% |
| outage | 100% | 96 | 98.00% | 99.25% | 99.62% |

## What this is a measurement of

Both the workload and the model that answers it are **simulated**, and the accuracy figures are a property of the table in `amg/upstream/simulated.py`. What is being measured is the routing and resilience arithmetic -- how much a fitted router's advantage survives held-out traffic, what a self-validating cascade can recover, what a breaker is worth at each capacity -- not the quality of any real model.

