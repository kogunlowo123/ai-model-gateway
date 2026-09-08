"""Shared fixtures.

Everything here is small on purpose. The shipped workloads have thousands of
tasks and the suite has to stay fast enough that people run it, so the fixtures
use the ``tiny`` plan and the tests that genuinely need scale say so.
"""

from __future__ import annotations

import pytest

from amg.gateway import GatewayConfig
from amg.routing.calibrate import calibrate, calibrate_blend
from amg.routing.estimator import fit
from amg.routing.policies import Blend, Cascade, Cheapest, Fitted
from amg.upstream.base import Upstream
from amg.upstream.simulated import BY_NAME, CATALOGUE
from amg.workload.build import PLANS, generate
from amg.workload.corpus import Corpus


@pytest.fixture
def upstreams() -> dict[str, Upstream]:
    """The shipped catalogue, typed as the protocol."""
    return dict(BY_NAME)


@pytest.fixture
def tiny() -> Corpus:
    """Sixty tasks across six families. Fast enough for a unit test."""
    return generate(PLANS["tiny"]).corpus


@pytest.fixture
def disjoint_pair() -> tuple[Corpus, Corpus]:
    """A fitting workload and a disjoint control, both small.

    Built with ``exclude=`` rather than filtered afterwards, which is the same
    guarantee the shipped workloads have and the one the experiment refuses to
    run without.
    """
    from dataclasses import replace

    plan = replace(PLANS["tiny"], size=240)
    first = generate(plan).corpus
    second = generate(
        replace(plan, name="tiny-control", seed=plan.seed + 1),
        exclude=frozenset(task.prompt for task in first),
    ).corpus
    return first, second


@pytest.fixture
def config() -> GatewayConfig:
    """Default gateway settings, named so a test can vary one thing."""
    return GatewayConfig()


@pytest.fixture
def cheapest() -> Cheapest:
    return Cheapest()


@pytest.fixture
def cascade() -> Cascade:
    return Cascade()


@pytest.fixture
def blend() -> Blend:
    """A blend with fixed shares, for tests that do not need it calibrated."""
    return Blend(to_cheap=6_000, to_middle=2_000)


@pytest.fixture
def fitted(disjoint_pair, upstreams) -> Fitted:
    """A fitted policy, calibrated on the fitting half of `disjoint_pair`."""
    fit_corpus, _ = disjoint_pair
    estimator = fit(fit_corpus, upstreams[CATALOGUE[0].name])
    thresholds = calibrate(fit_corpus, estimator, upstreams)
    return Fitted(estimator=estimator, high=thresholds.high, low=thresholds.low)


@pytest.fixture
def calibrated_shares(disjoint_pair, upstreams, fitted) -> tuple[int, int]:
    """Blend shares sized to the fitted policy's spend, as the experiment does."""
    from amg.replay import run

    fit_corpus, _ = disjoint_pair
    spend = run(fit_corpus, fitted, upstreams).total_cost
    return calibrate_blend(fit_corpus, upstreams, spend)
