"""Turning replays into numbers, with the arithmetic stated rather than implied.

Three modules, three questions:

* :mod:`amg.evaluate.metrics` -- intervals, paired tests, and the cost-quality
  frontier. Every rate carries a confidence interval, and every comparison
  between two policies on the same traffic uses a **paired** test, because
  counterfactual replay produces matched samples and treating them as
  independent throws away most of the power.
* :mod:`amg.evaluate.experiment` -- the routing experiment: fit on one workload,
  report on disjoint ones, and say what moves.
* :mod:`amg.evaluate.resilience` -- the retry sweep, where a policy that helps
  at a low upstream failure rate stops helping at a high one.
"""
