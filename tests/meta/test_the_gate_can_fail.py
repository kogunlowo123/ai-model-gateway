"""The gate's own negative controls: break one thing, assert it goes red.

A gate that has only ever been observed passing is indistinguishable from
``true`` in a shell script. Every check in :mod:`amg.evaluate.baseline` and every
refusal in :mod:`amg.evaluate.experiment` gets a test here that makes it fire,
because the alternative is trusting that code nobody has watched fail will fail
when it matters.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from amg.errors import GateError, RefusalError
from amg.evaluate.baseline import Baseline, enforce
from amg.evaluate.experiment import run_experiment
from amg.evaluate.metrics import wilson
from amg.evaluate.resilience import PROVISIONED, UNDER_PROVISIONED, Measurement, Sweep
from amg.workload.build import PLANS, generate

pytestmark = pytest.mark.meta


@pytest.fixture(scope="module")
def small_experiment():
    """One real experiment over small workloads, reused by every test here."""
    from amg.upstream.simulated import BY_NAME

    plan = replace(PLANS["tiny"], size=300)
    fit_corpus = generate(plan).corpus
    exclude = frozenset(task.prompt for task in fit_corpus)
    control = generate(replace(plan, name="control", seed=plan.seed + 1), exclude=exclude).corpus
    shifted = generate(
        replace(plan, name="shift", seed=plan.seed + 2, hard_share=0.8), exclude=exclude
    ).corpus
    return run_experiment(fit_corpus, control, {"shift": shifted}, dict(BY_NAME))


def _sweep(*, under: float, provisioned: float) -> Sweep:
    """A Sweep carrying only the two figures the gate reads.

    Built by hand rather than measured. A real sweep takes minutes, and what is
    under test here is the gate's arithmetic, not the simulator's -- the
    simulator is exercised end to end by the resilience command's own tests.
    """
    from amg.upstream.simulated import SCALE

    return Sweep(
        policy="cascade",
        arrival_rate=40,
        measurements=tuple(
            Measurement(
                arm="retry+breaker",
                fault="outage",
                level=SCALE,
                concurrency=concurrency,
                answered=wilson(round(rate * 1_000), 1_000),
                expired=0,
                calls_per_request=1.0,
                cost_total=0,
                p50_latency_us=0,
                p99_latency_us=0,
                breaker_rejections=0,
                peak_queue_depth=0,
            )
            for concurrency, rate in (
                (UNDER_PROVISIONED, under),
                (PROVISIONED, provisioned),
            )
        ),
    )


class TestTheGatePasses:
    def test_a_baseline_recorded_from_a_run_passes_that_same_run(self, small_experiment):
        # The control. Without a green control the red results below could be
        # facts about the gate rather than about the change.
        baseline = Baseline.from_experiment(small_experiment)
        assert enforce(small_experiment, baseline) == []


class TestTheGateFires:
    def test_a_correctness_regression_fails(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment)
        inflated = {
            workload: {policy: min(1.0, value + 0.20) for policy, value in policies.items()}
            for workload, policies in baseline.correctness.items()
        }
        with pytest.raises(GateError, match="correctness fell"):
            enforce(small_experiment, replace(baseline, correctness=inflated))

    def test_cost_growth_fails(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment)
        cheaper = {
            workload: {policy: value / 2 for policy, value in policies.items()}
            for workload, policies in baseline.cost_total.items()
        }
        with pytest.raises(GateError, match="cost grew"):
            enforce(small_experiment, replace(baseline, cost_total=cheaper))

    def test_a_shrinking_estimator_advantage_fails(self, small_experiment):
        # The number that says the router is worth having at all. A router that
        # stops beating a coin flip at the same price has no reason to exist,
        # whatever its raw correctness.
        baseline = Baseline.from_experiment(small_experiment)
        raised = {name: value + 0.20 for name, value in baseline.estimator_advantage.items()}
        with pytest.raises(GateError, match="advantage over the spend-matched null fell"):
            enforce(small_experiment, replace(baseline, estimator_advantage=raised))

    def test_a_workload_that_stopped_being_measured_fails(self, small_experiment):
        # A silently dropped arm is the classic way a gate goes green while
        # measuring less than it used to.
        baseline = Baseline.from_experiment(small_experiment)
        with_extra = {**baseline.correctness, "vanished": {"cheapest": 0.5}}
        with pytest.raises(GateError, match="did not measure"):
            enforce(small_experiment, replace(baseline, correctness=with_extra))

    def test_a_workload_the_baseline_never_heard_of_fails(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment)
        trimmed = {name: value for name, value in baseline.correctness.items() if name != "shift"}
        with pytest.raises(GateError, match="never heard of"):
            enforce(small_experiment, replace(baseline, correctness=trimmed))

    def test_a_policy_that_stopped_being_measured_fails(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment)
        with_extra = {
            workload: {**policies, "clairvoyant": 0.99}
            for workload, policies in baseline.correctness.items()
        }
        with pytest.raises(GateError, match="were not measured"):
            enforce(small_experiment, replace(baseline, correctness=with_extra))

    def test_a_missing_baseline_says_how_to_record_one(self, tmp_path):
        from amg.errors import ConfigError

        with pytest.raises(ConfigError) as caught:
            Baseline.load(tmp_path / "absent.json")
        assert "--update-baseline" in (caught.value.remedy or "")

    def test_a_baseline_from_a_future_format_is_refused(self, small_experiment, tmp_path):
        import json

        from amg.errors import ConfigError

        path = tmp_path / "baseline.json"
        document = json.loads(Baseline.from_experiment(small_experiment).to_json())
        document["format"] = 99
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ConfigError, match="format 99"):
            Baseline.load(path)


class TestTheExperimentRefuses:
    def test_measuring_the_router_on_its_own_fitting_workload_is_refused(self):
        from amg.upstream.simulated import BY_NAME

        corpus = generate(replace(PLANS["tiny"], size=120)).corpus
        with pytest.raises(RefusalError, match="control workload is the fitting workload"):
            run_experiment(corpus, corpus, {}, dict(BY_NAME))

    def test_an_overlapping_control_is_refused_rather_than_filtered(self):
        # Dropping the overlap afterwards removes samples unevenly across
        # difficulties, so the corpus stops having the mix it claims -- and the
        # mix is the thing this experiment varies.
        from amg.upstream.simulated import BY_NAME

        plan = replace(PLANS["tiny"], size=120)
        fit_corpus = generate(plan).corpus
        overlapping = generate(replace(plan, name="overlap", seed=plan.seed)).corpus
        with pytest.raises(RefusalError, match="appear in both"):
            run_experiment(fit_corpus, overlapping, {}, dict(BY_NAME))

    def test_a_baseline_round_trip_through_disk_preserves_the_verdict(
        self, small_experiment, tmp_path
    ):
        path = Baseline.from_experiment(small_experiment).write(tmp_path / "b.json")
        assert enforce(small_experiment, Baseline.load(path)) == []


class TestTheResilienceFiguresAreGated:
    """The two front-page resilience numbers, and the gate that re-measures them.

    They were published before they were gated, which is the failure this
    series is about: a number nothing re-measures is a number that goes stale
    without anyone noticing it went stale.
    """

    def test_a_baseline_with_no_sweep_records_no_figures(self, small_experiment):
        assert Baseline.from_experiment(small_experiment).breaker_under_total_outage == {}

    def test_a_recorded_sweep_passes_its_own_figures(self, small_experiment):
        sweep = _sweep(under=0.21, provisioned=0.996)
        baseline = Baseline.from_experiment(small_experiment, sweep)
        assert baseline.breaker_under_total_outage == {
            str(UNDER_PROVISIONED): pytest.approx(0.21, abs=1e-9),
            str(PROVISIONED): pytest.approx(0.996, abs=1e-9),
        }
        assert enforce(small_experiment, baseline, sweep) == []

    def test_a_fall_in_the_provisioned_figure_fails(self, small_experiment):
        # The headline claim is that a breaker plus capacity serves almost
        # everything through a total outage. This is the test that notices when
        # it stops being true.
        baseline = Baseline.from_experiment(small_experiment, _sweep(under=0.21, provisioned=0.996))
        with pytest.raises(GateError, match="resilience/pool"):
            enforce(small_experiment, baseline, _sweep(under=0.21, provisioned=0.60))

    def test_a_rise_passes_and_a_move_inside_the_tolerance_passes(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment, _sweep(under=0.21, provisioned=0.90))
        assert enforce(small_experiment, baseline, _sweep(under=0.30, provisioned=0.99)) == []
        assert enforce(small_experiment, baseline, _sweep(under=0.20, provisioned=0.89)) == []

    def test_a_run_that_swept_nothing_fails_rather_than_skipping_the_check(self, small_experiment):
        # The quiet failure this replaces: `enforce` sees no sweep, checks
        # nothing, and reports a clean run against a baseline it never read.
        baseline = Baseline.from_experiment(small_experiment, _sweep(under=0.21, provisioned=0.996))
        with pytest.raises(GateError, match="--with-resilience"):
            enforce(small_experiment, baseline)

    def test_a_capacity_that_stopped_being_swept_fails(self, small_experiment):
        baseline = Baseline.from_experiment(small_experiment, _sweep(under=0.21, provisioned=0.996))
        narrowed = replace(
            baseline,
            breaker_under_total_outage={**baseline.breaker_under_total_outage, "999": 0.5},
        )
        with pytest.raises(GateError, match="not swept"):
            enforce(small_experiment, narrowed, _sweep(under=0.21, provisioned=0.996))

    def test_the_figures_survive_a_round_trip_through_disk(self, small_experiment, tmp_path):
        sweep = _sweep(under=0.21, provisioned=0.996)
        path = Baseline.from_experiment(small_experiment, sweep).write(tmp_path / "b.json")
        assert enforce(small_experiment, Baseline.load(path), sweep) == []


class TestTheSpendMatchIsReported:
    """Whether a row's difference is attribution or budget.

    The blend is sized once against the fitted router's spend on the fitting
    workload, so the match is exact there and drifts off it. A reader who takes
    a shifted row as "the estimator is worth this much" is reading a number
    that is partly extra money, and the only defence is publishing the ratio
    next to it.
    """

    def test_the_match_is_exact_on_the_workload_it_was_sized_against(self, small_experiment):
        assert small_experiment.fit_workload.spend_match == pytest.approx(1.0, abs=0.15)

    def test_every_workload_reports_a_ratio(self, small_experiment):
        assert all(w.spend_match > 0 for w in small_experiment.all_workloads())

    def test_the_ratio_reaches_the_report_and_the_json(self, small_experiment, tmp_path):
        import json

        from amg.evaluate.report import write_reports

        write_reports(
            small_experiment,
            None,
            json_out=tmp_path / "r.json",
            markdown_out=tmp_path / "r.md",
            junit_out=None,
        )
        document = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
        assert all("spend_versus_null" in row for row in document["workloads"])
        markdown = (tmp_path / "r.md").read_text(encoding="utf-8")
        assert "Spend vs null" in markdown
        assert "Read the last column before reading the difference column" in markdown
