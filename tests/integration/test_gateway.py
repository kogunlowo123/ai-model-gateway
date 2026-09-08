"""The gateway end to end: escalation, retries, breakers, deadlines, cost."""

from __future__ import annotations

from dataclasses import replace

import pytest

from amg.clock import Pool, Simulation
from amg.errors import ConfigError
from amg.gateway import ANSWERED, EXPIRED, FAILED, GatewayConfig, serve, serve_one
from amg.resilience.breaker import Breakers, CircuitBreaker, State
from amg.resilience.retry import RetryPolicy
from amg.upstream.base import Outcome, Upstream
from amg.upstream.simulated import BY_NAME, CATALOGUE, SCALE
from amg.workload.tasks import Task, render

pytestmark = pytest.mark.integration


def _task(answer: str = "42", prompt: str = "What is 40 + 2?") -> Task:
    return Task(task_id="t:1", family="arithmetic", difficulty=1, prompt=prompt, answer=answer)


class _Scripted:
    """An upstream whose outcomes are given, for testing the gateway's logic.

    Not a mock of the simulator -- a second, simpler implementation of the same
    protocol. That is what lets these tests state a scenario ("fail twice then
    succeed") without reverse-engineering a hash.
    """

    def __init__(self, name: str, script: list[tuple[Outcome, str | None, int]]) -> None:
        self.name = name
        self.script = script
        self.calls = 0
        self.input_price_per_1k = 100
        self.output_price_per_1k = 200

    def attempt(self, task, *, nonce=0, at_us=0):
        from amg.upstream.base import Attempt

        index = min(self.calls, len(self.script) - 1)
        outcome, response, latency = self.script[index]
        self.calls += 1
        return Attempt(
            upstream=self.name,
            outcome=outcome,
            response=response,
            latency_us=latency,
            input_tokens=10,
            output_tokens=10 if response else 0,
            cost_micro_cents=3,
        )


class TestEscalation:
    def test_a_malformed_answer_escalates_to_the_next_rung(self, cascade):
        task = _task()
        upstreams = {
            "nano": _Scripted("nano", [(Outcome.OK, "not json at all", 1_000)]),
            "flagship": _Scripted("flagship", [(Outcome.OK, render("42"), 1_000)]),
        }
        served = serve_one(task, cascade, upstreams)  # type: ignore[arg-type]
        assert served.escalated
        assert served.upstream == "flagship"
        assert served.outcome == ANSWERED

    def test_a_well_formed_wrong_answer_does_not_escalate(self, cascade):
        # The gap this whole project measures: a gateway cannot know the answer
        # is wrong, so a cascade cannot recover it.
        task = _task(answer="42")
        upstreams = {
            "nano": _Scripted("nano", [(Outcome.OK, render("41"), 1_000)]),
            "flagship": _Scripted("flagship", [(Outcome.OK, render("42"), 1_000)]),
        }
        served = serve_one(task, cascade, upstreams)  # type: ignore[arg-type]
        assert not served.escalated
        assert served.upstream == "nano"
        assert served.outcome == ANSWERED
        assert not task.is_correct(served.response or "")

    def test_a_transport_failure_also_escalates(self, cascade):
        upstreams = {
            "nano": _Scripted("nano", [(Outcome.ERROR, None, 1_000)]),
            "flagship": _Scripted("flagship", [(Outcome.OK, render("42"), 1_000)]),
        }
        served = serve_one(_task(), cascade, upstreams)  # type: ignore[arg-type]
        assert served.upstream == "flagship"

    def test_a_single_rung_policy_cannot_escalate(self, cheapest):
        upstreams = {"nano": _Scripted("nano", [(Outcome.ERROR, None, 1_000)])}
        served = serve_one(_task(), cheapest, upstreams)  # type: ignore[arg-type]
        assert served.outcome == FAILED
        assert not served.escalated

    def test_routing_to_an_unknown_upstream_is_a_configuration_error(self, cheapest):
        with pytest.raises(ConfigError, match="unknown upstream"):
            serve_one(_task(), cheapest, {})


class TestRetries:
    def test_a_retry_is_a_different_draw_not_a_repeat(self, cheapest):
        # Without the nonce a retry is bit-identical to the attempt that just
        # failed, every retry fails too, and the measured value of retrying is
        # exactly zero -- an artefact that would read as a finding.
        upstream = BY_NAME["nano"]
        first = upstream.attempt(_task(), nonce=0)
        second = upstream.attempt(_task(), nonce=1)
        assert (first.outcome, first.response) != (second.outcome, second.response) or (
            first.latency_us != second.latency_us
        )

    def test_retrying_recovers_a_transient_failure(self, cheapest):
        upstreams = {
            "nano": _Scripted(
                "nano",
                [(Outcome.ERROR, None, 1_000), (Outcome.OK, render("42"), 1_000)],
            )
        }
        config = GatewayConfig(retry=RetryPolicy(max_attempts=2))
        served = serve_one(_task(), cheapest, upstreams, config)  # type: ignore[arg-type]
        assert served.outcome == ANSWERED
        assert len(served.attempts) == 2

    def test_one_attempt_means_one_attempt(self, cheapest):
        upstreams = {
            "nano": _Scripted(
                "nano",
                [(Outcome.ERROR, None, 1_000), (Outcome.OK, render("42"), 1_000)],
            )
        }
        config = GatewayConfig(retry=RetryPolicy(max_attempts=1))
        served = serve_one(_task(), cheapest, upstreams, config)  # type: ignore[arg-type]
        assert served.outcome == FAILED
        assert len(served.attempts) == 1

    def test_backoff_is_zero_before_the_first_attempt(self):
        policy = RetryPolicy()
        assert policy.backoff_us("k", 0) == 0

    def test_backoff_grows_and_stays_inside_its_window(self):
        policy = RetryPolicy(backoff_base_us=1_000, backoff_ceiling_us=8_000)
        for attempt in range(1, 6):
            window = min(1_000 * (1 << (attempt - 1)), 8_000)
            assert 0 <= policy.backoff_us("key", attempt) <= window

    def test_backoff_is_deterministic_for_the_same_key(self):
        policy = RetryPolicy()
        assert policy.backoff_us("same", 2) == policy.backoff_us("same", 2)

    def test_zero_attempts_is_not_a_policy(self):
        with pytest.raises(ConfigError, match="at least one"):
            RetryPolicy(max_attempts=0)


class TestCost:
    def test_a_failed_attempt_is_still_billed(self, cheapest):
        # A transport failure still consumed the input tokens on most providers,
        # and pretending otherwise would make every retry policy look free.
        upstreams = {
            "nano": _Scripted(
                "nano",
                [(Outcome.ERROR, None, 1_000), (Outcome.OK, render("42"), 1_000)],
            )
        }
        config = GatewayConfig(retry=RetryPolicy(max_attempts=2))
        served = serve_one(_task(), cheapest, upstreams, config)  # type: ignore[arg-type]
        assert served.cost_micro_cents == 6

    def test_cost_is_the_sum_of_every_attempt(self, cascade):
        upstreams = {
            "nano": _Scripted("nano", [(Outcome.OK, "garbage", 1_000)]),
            "flagship": _Scripted("flagship", [(Outcome.OK, render("42"), 1_000)]),
        }
        served = serve_one(_task(), cascade, upstreams)  # type: ignore[arg-type]
        assert served.cost_micro_cents == sum(a.cost_micro_cents for a in served.attempts)


class TestDeadline:
    def test_a_slow_call_times_out_and_is_still_billed(self, cheapest):
        upstreams = {"nano": _Scripted("nano", [(Outcome.OK, render("42"), 9_000_000)])}
        config = GatewayConfig(retry=RetryPolicy(max_attempts=1, timeout_us=1_000_000))
        served = serve_one(_task(), cheapest, upstreams, config)  # type: ignore[arg-type]
        assert served.attempts[0].outcome == Outcome.TIMEOUT
        assert served.cost_micro_cents > 0

    def test_the_deadline_bounds_end_to_end_latency(self, cheapest):
        upstreams = {"nano": _Scripted("nano", [(Outcome.OK, render("42"), 9_000_000)])}
        config = GatewayConfig(
            retry=RetryPolicy(max_attempts=3, timeout_us=1_000_000),
            deadline_us=2_500_000,
        )
        served = serve_one(_task(), cheapest, upstreams, config)  # type: ignore[arg-type]
        assert served.latency_us <= config.deadline_us
        assert served.outcome == EXPIRED

    def test_a_deadline_under_the_call_timeout_is_refused(self):
        with pytest.raises(ConfigError, match="below the per-attempt timeout"):
            GatewayConfig(retry=RetryPolicy(timeout_us=5_000_000), deadline_us=1_000_000)

    def test_a_queued_request_gives_up_rather_than_waiting_out_the_queue(self):
        # Without this the deadline cannot see queueing at all, and a saturated
        # gateway degrades into unbounded latency rather than visible errors.
        from amg.routing.policies import Cheapest

        sim = Simulation()
        pool = Pool(1)
        config = GatewayConfig(
            retry=RetryPolicy(max_attempts=1, timeout_us=1_400_000),
            deadline_us=1_500_000,
        )
        # Annotated because dict is invariant in its value type, so a
        # dict[str, _Scripted] is not a dict[str, Upstream].
        upstreams: dict[str, Upstream] = {
            "nano": _Scripted("nano", [(Outcome.OK, render("42"), 1_300_000)])
        }
        breakers = config.breakers()
        results = []
        policy = Cheapest()

        def one(task_id: str):
            task = replace(_task(), task_id=task_id)
            served = yield from serve(
                sim,
                task,
                policy,
                upstreams,
                config,
                breakers,
                pool,
            )
            results.append(served)

        sim.start(one("a"))
        sim.start(one("b"))
        sim.run()
        assert any(served.outcome == EXPIRED for served in results)
        assert all(served.latency_us <= config.deadline_us for served in results)


class TestBreaker:
    def test_it_opens_after_consecutive_failures(self):
        breaker = CircuitBreaker(name="nano", failure_threshold=3, cooldown_us=1_000)
        for _ in range(3):
            breaker.record_failure(0)
        assert breaker.state(0) is State.OPEN
        assert not breaker.allows(0)

    def test_a_success_forgets_the_history(self):
        breaker = CircuitBreaker(name="nano", failure_threshold=3)
        breaker.record_failure(0)
        breaker.record_failure(0)
        breaker.record_success(0)
        breaker.record_failure(0)
        assert breaker.state(0) is State.CLOSED

    def test_it_half_opens_after_the_cooldown_and_admits_one_probe(self):
        breaker = CircuitBreaker(name="nano", failure_threshold=1, cooldown_us=1_000)
        breaker.record_failure(0)
        assert breaker.state(1_000) is State.HALF_OPEN
        assert breaker.allows(1_000)
        # Exactly one: admitting several is how a breaker becomes a thundering
        # herd against an upstream that has not recovered.
        assert not breaker.allows(1_000)

    def test_a_failed_probe_reopens_immediately(self):
        breaker = CircuitBreaker(name="nano", failure_threshold=5, cooldown_us=1_000)
        breaker.record_failure(0)
        breaker.opened_at = 0
        assert breaker.allows(1_000)
        breaker.record_failure(1_000)
        assert breaker.state(1_000) is State.OPEN

    def test_an_abandoned_probe_hands_its_permission_back(self):
        # A half-open probe that never makes its call would otherwise leave the
        # breaker waiting for a result nobody is going to send, rejecting every
        # subsequent call for the rest of the run.
        breaker = CircuitBreaker(name="nano", failure_threshold=1, cooldown_us=1_000)
        breaker.record_failure(0)
        assert breaker.allows(1_000)
        breaker.release_probe()
        assert breaker.allows(1_000)

    def test_a_disabled_set_always_allows(self):
        breakers = Breakers(enabled=False)
        for _ in range(50):
            breakers.record_failure("nano", 0)
        assert breakers.allows("nano", 0)
        assert breakers.rejections == 0

    def test_an_open_breaker_sheds_traffic_from_the_gateway(self, cheapest):
        upstreams = {"nano": _Scripted("nano", [(Outcome.ERROR, None, 1_000)])}
        config = GatewayConfig(
            retry=RetryPolicy(max_attempts=1), breaker_threshold=1, breaker_enabled=True
        )
        breakers = config.breakers()
        pool = Pool(4)
        seen = []
        for index in range(4):
            sim = Simulation()
            task = replace(_task(), task_id=f"t:{index}")
            done = sim.start(
                serve(sim, task, cheapest, upstreams, config, breakers, pool)  # type: ignore[arg-type]
            )
            sim.run()
            seen.append(done.value)
        assert breakers.rejections > 0
        assert seen[-1].rejected_by_breaker > 0


class TestOutage:
    def test_an_upstream_in_an_outage_window_always_fails(self):
        upstream = replace(
            CATALOGUE[0], outage_period_us=1_000, outage_length_us=400, failure_rate=0
        )
        assert upstream.is_out(0)
        assert upstream.is_out(399)
        assert not upstream.is_out(400)
        assert upstream.attempt(_task(), at_us=100).outcome is Outcome.ERROR
        assert upstream.attempt(_task(), at_us=900).outcome is Outcome.OK

    def test_a_flaky_upstream_fails_independently(self):
        upstream = replace(CATALOGUE[0], failure_rate=SCALE)
        assert upstream.attempt(_task()).outcome is Outcome.ERROR
