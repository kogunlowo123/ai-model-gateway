"""Deciding where a request goes, and fitting the thing that decides.

The split matters: :mod:`amg.routing.policies` is pure and sees only prompt
text, :mod:`amg.routing.features` defines exactly what "only prompt text" means
in practice, and :mod:`amg.routing.estimator` is the fitted model whose optimism
this repository is about.
"""
