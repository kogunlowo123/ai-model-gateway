"""Adversarial cases. A failure here is a security regression, not a style nit.

A gateway sits in front of a paid API and is reachable by whoever can reach the
application. That makes three things security properties rather than robustness
niceties:

* an unbounded request turns directly into an unbounded bill;
* a crash on the request path converts a routing problem into an outage;
* a credential anywhere in this repository is a credential in every clone.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from amg.api.app import MAX_PROMPT_CHARS, create_app
from amg.errors import ConfigError
from amg.gateway import serve_one
from amg.routing import features
from amg.settings import Settings
from amg.workload.build import PLANS, generate
from amg.workload.tasks import Task, parse_answer

pytestmark = pytest.mark.security

#: Shapes that would be a leaked credential if one ever appeared in a workload,
#: a report or an example. The generated corpora are the one place in this
#: repository where writing something that looks like a key would seem natural.
SECRET_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})"
)


class TestNoCredentials:
    def test_no_generated_workload_contains_anything_secret_shaped(self):
        for plan in ("tiny", "measure"):
            corpus = generate(PLANS[plan]).corpus
            for task in corpus:
                assert not SECRET_SHAPES.search(task.prompt)
                assert not SECRET_SHAPES.search(task.answer)

    def test_settings_has_no_field_that_looks_like_a_key(self):
        # This package authenticates to nothing. A field named like a credential
        # appearing here would be a design change, not a configuration change.
        forbidden = ("key", "secret", "token", "password", "credential")
        for name in Settings.model_fields:
            assert not any(word in name.lower() for word in forbidden), name

    def test_a_misspelt_environment_variable_fails_at_startup(self, monkeypatch):
        # A carefully chosen AMG_CONCURENCY that does nothing for six months is
        # worse than an error on the first run.
        monkeypatch.setenv("AMG_CONCURENCY", "48")
        with pytest.raises(ConfigError) as caught:
            Settings.from_environment()
        assert "AMG_CONCURENCY" in str(caught.value)

    def test_pydantic_alone_does_not_catch_it(self, monkeypatch):
        # The measurement behind `unknown_variables`. `extra="forbid"` rejects
        # unknown keys passed to the constructor, but pydantic-settings walks
        # from known field names *to* the environment, so a prefixed variable
        # that matches no field is never looked at. This test exists so that a
        # future pydantic release fixing it shows up as a failure here rather
        # than leaving dead code in place.
        monkeypatch.setenv("AMG_CONCURENCY", "48")
        # `_env_file` is a pydantic-settings init kwarg rather than a field, so
        # it is invisible to the generated signature; passing None keeps a
        # developer's local .env out of a test about the environment.
        assert Settings(_env_file=None).concurrency != 48  # type: ignore[call-arg]


class TestUnboundedInput:
    def test_an_oversized_prompt_is_refused_before_any_upstream_is_called(self):
        client = TestClient(create_app(Settings(policy="cheapest")))
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x" * (MAX_PROMPT_CHARS + 1)}]},
        )
        assert response.status_code == 413

    def test_an_empty_message_list_is_refused(self):
        client = TestClient(create_app(Settings(policy="cheapest")))
        response = client.post("/v1/chat/completions", json={"messages": []})
        assert response.status_code == 422

    def test_an_unknown_role_is_refused(self):
        client = TestClient(create_app(Settings(policy="cheapest")))
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "root", "content": "hello"}]},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{" * 20_000, id="unbalanced-braces"),
            pytest.param('"' * 20_000, id="quotes"),
            pytest.param("9" * 20_000, id="one-enormous-number"),
            pytest.param("\x00" * 1_000, id="nulls"),
            pytest.param("\u200b" * 20_000, id="zero-width"),
            pytest.param("a" * 20_000, id="one-long-word"),
        ],
    )
    def test_feature_extraction_is_linear_and_never_raises(self, payload):
        # These run on attacker-supplied text before any routing decision. A
        # quadratic regex here would be a denial-of-service primitive that
        # arrived through the front door.
        started = time.perf_counter()
        vector = features.extract(payload)
        elapsed = time.perf_counter() - started
        assert len(vector) == len(features.FEATURE_NAMES)
        # A generous tripwire for quadratic behaviour, not a benchmark.
        assert elapsed < 1.0

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param("", id="empty"),
            pytest.param("null", id="json-null"),
            pytest.param("[]", id="array"),
            pytest.param('{"answer": null}', id="null-answer"),
            pytest.param('{"answer": {"nested": 1}}', id="object-answer"),
            pytest.param('{"answer": [1, 2]}', id="array-answer"),
            pytest.param('{"other": 1}', id="wrong-key"),
            pytest.param("{" * 5_000, id="unbalanced-braces"),
            pytest.param('{"answer": "' + "x" * 50_000 + '"}', id="enormous-answer"),
        ],
    )
    def test_answer_parsing_never_raises_on_hostile_input(self, response):
        # `parse_answer` is the gateway's only runtime validation, so it runs on
        # whatever an upstream returns -- including an upstream that has been
        # compromised or is simply broken.
        assert parse_answer(response) is None or isinstance(parse_answer(response), str)


class TestRequestPathNeverCrashes:
    def test_an_upstream_that_returns_nonsense_does_not_crash_the_gateway(self, cascade, upstreams):
        task = Task(
            task_id="hostile",
            family="adhoc",
            difficulty=1,
            prompt="\x00﻿" + "?" * 500,
            answer="",
        )
        served = serve_one(task, cascade, upstreams)
        assert served.outcome in {"answered", "malformed", "failed", "expired"}

    def test_the_http_surface_reports_an_upstream_failure_rather_than_a_traceback(
        self, monkeypatch
    ):
        from dataclasses import replace

        from amg.upstream.simulated import CATALOGUE, SCALE

        dead = {upstream.name: replace(upstream, failure_rate=SCALE) for upstream in CATALOGUE}
        monkeypatch.setattr("amg.api.app.BY_NAME", dead)
        client = TestClient(create_app(Settings(policy="cheapest")), raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "anything"}]},
        )
        assert response.status_code in (502, 504)
        assert "outcome" in response.json()["detail"]


class TestCostIsBounded:
    def test_a_single_request_cannot_call_an_unbounded_number_of_times(self, cascade, upstreams):
        from amg.gateway import GatewayConfig
        from amg.resilience.retry import RetryPolicy

        config = GatewayConfig(retry=RetryPolicy(max_attempts=3))
        task = Task(task_id="bounded", family="adhoc", difficulty=5, prompt="hard?", answer="x")
        served = serve_one(task, cascade, upstreams, config)
        # Two rungs, three attempts each: the ceiling is arithmetic, and this
        # asserts the gateway respects it rather than looping.
        assert len(served.attempts) <= 2 * config.retry.max_attempts
