"""One routing core, shared by the served path and the replayed one.

This file exists because of a specific failure: a project publishes a
counterfactual table computed by a replay harness that has quietly drifted from
the code actually serving traffic, and the numbers then describe a gateway
nobody is running. The assertion is cheap and the failure is invisible without
it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from amg import replay
from amg.api.app import create_app
from amg.errors import RefusalError
from amg.gateway import serve_one
from amg.routing.policies import POLICY_NAMES, build
from amg.settings import Settings
from amg.workload.corpus import build_corpus

pytestmark = pytest.mark.integration


class TestOneCore:
    def test_every_policy_decides_identically_however_it_is_reached(
        self, tiny, fitted, calibrated_shares
    ):
        for name in POLICY_NAMES:
            policy = build(
                name,
                estimator=fitted.estimator,
                thresholds=(fitted.high, fitted.low),
                shares=calibrated_shares,
            )
            direct = [policy.decide(task.prompt) for task in tiny]
            through_replay = replay.run(
                tiny,
                policy,
                dict(__import__("amg.upstream.simulated", fromlist=["BY_NAME"]).BY_NAME),
            )
            for decision, record in zip(direct, through_replay.records, strict=True):
                # The first rung is what the policy chose; the record's upstream
                # may differ only when the gateway escalated, which it reports.
                if not record.escalated and record.upstream is not None:
                    assert record.upstream == decision.first

    def test_the_http_surface_routes_the_same_way_as_the_library(self, upstreams):
        settings = Settings(policy="cascade")
        client = TestClient(create_app(settings))
        prompt = "What is 21 + 21? Reply with JSON."

        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": prompt}]},
        )
        assert response.status_code == 200
        served_over_http = response.json()["amg"]

        from amg.api.app import hash_prompt
        from amg.workload.tasks import Task

        task = Task(
            task_id=f"http:{hash_prompt(prompt)}",
            family="http",
            difficulty=1,
            prompt=prompt,
            answer="",
        )
        served_directly = serve_one(task, settings.build_policy(), upstreams, settings.gateway())
        assert served_over_http["upstream"] == served_directly.upstream
        assert served_over_http["reason"] == served_directly.reason
        assert served_over_http["cost_micro_cents"] == served_directly.cost_micro_cents


class TestReplayDeterminism:
    def test_a_replay_reproduces_its_own_digest(self, tiny, cascade, upstreams):
        digest = replay.verify_determinism(tiny, cascade, upstreams)
        assert digest == replay.run(tiny, cascade, upstreams).digest()

    def test_loaded_mode_reproduces_too(self, tiny, cascade, upstreams):
        digest = replay.verify_determinism(tiny, cascade, upstreams, arrival_rate=40)
        assert digest.startswith("sha256:")

    def test_a_non_deterministic_policy_is_refused_rather_than_averaged(self, tiny, upstreams):
        # The refusal exists because every counterfactual figure this project
        # publishes assumes a policy re-run on the same traffic does the same
        # thing. A policy that quietly does not would produce a regret table
        # made of noise.
        from amg.routing.policies import Decision
        from amg.upstream.simulated import BY_PRICE

        class Coin:
            name = "coin"

            def __init__(self) -> None:
                self.calls = 0

            def decide(self, prompt: str) -> Decision:
                self.calls += 1
                return Decision((BY_PRICE[self.calls % 2],), reason="unstable")

        # An odd number of tasks, deliberately. With an even count the counter
        # lands on the same parity at the start of the second run and the two
        # runs agree by accident -- which is a neat illustration of why the
        # determinism check compares digests rather than trusting a code review.
        odd = build_corpus(tiny.tasks[:5])
        with pytest.raises(RefusalError, match="did not reproduce"):
            replay.verify_determinism(odd, Coin(), upstreams)

    def test_an_empty_workload_replays_to_an_empty_result(self, cascade, upstreams):
        empty = build_corpus([])
        result = replay.run(empty, cascade, upstreams)
        assert len(result) == 0
        assert result.total_cost == 0
        assert result.quantile_us(0.95) == 0
