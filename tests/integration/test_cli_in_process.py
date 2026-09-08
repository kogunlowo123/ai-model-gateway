"""The same CLI, driven in process.

The subprocess layer in ``tests/e2e`` sees what the operating system sees -- exit
codes, console encoding -- and **none of it is visible to coverage**, because a
subprocess is a different interpreter. A previous project in this series left its
CLI at 0% coverage for exactly that reason: it was the largest, least examined
module in the repository, and driving it in process found two real defects on the
first run.

So both layers exist, deliberately, and neither substitutes for the other. This
one can see which branch ran and read the exception; that one can see the code
the shell receives.
"""

from __future__ import annotations

import json

import pytest

from amg.cli import COMMANDS, build_parser, main
from amg.errors import EXIT_GATE_FAILED, EXIT_OK, EXIT_USAGE

pytestmark = pytest.mark.integration


class TestParser:
    def test_every_registered_subcommand_is_dispatchable(self):
        parser = build_parser()
        actions = [action for action in parser._actions if action.dest == "command"]
        assert actions, "no subparsers registered"
        choices = actions[0].choices
        assert choices is not None
        assert set(choices) == set(COMMANDS)

    def test_the_parser_can_be_built_twice(self):
        # argparse raises on a duplicate subparser registration at build time,
        # so this is really a check that nothing registers into a shared parser.
        assert set(build_parser()._actions[-1].choices or ()) == set(
            build_parser()._actions[-1].choices or ()
        )


class TestWorkloadCommands:
    def test_synth_writes_a_workload_and_prints_its_digest(self, tmp_path, capsys):
        out = tmp_path / "w.jsonl.gz"
        assert main(["synth", "--plan", "tiny", "--out", str(out)]) == EXIT_OK
        printed = capsys.readouterr().out
        assert "digest sha256:" in printed
        assert out.exists()

    def test_synth_can_exclude_another_workload(self, tmp_path, capsys):
        from amg.workload.corpus import read_corpus

        first = tmp_path / "a.jsonl.gz"
        second = tmp_path / "b.jsonl.gz"
        main(["synth", "--plan", "tiny", "--out", str(first)])
        main(
            [
                "synth",
                "--plan",
                "tiny",
                "--out",
                str(second),
                "--disjoint-from",
                str(first),
            ]
        )
        capsys.readouterr()
        left = {task.prompt for task in read_corpus(first)}
        right = {task.prompt for task in read_corpus(second)}
        assert not left & right

    def test_check_passes_a_freshly_generated_workload(self, tmp_path, capsys):
        out = tmp_path / "w.jsonl.gz"
        main(["synth", "--plan", "tiny", "--out", str(out)])
        assert main(["check", "--plan", "tiny", "--corpus", str(out)]) == EXIT_OK
        assert "matches plan" in capsys.readouterr().out

    def test_check_fails_an_edited_workload_and_prints_both_digests(self, tmp_path, capsys):
        from amg.workload.build import PLANS, generate
        from amg.workload.corpus import build_corpus

        out = tmp_path / "w.jsonl.gz"
        corpus = generate(PLANS["tiny"]).corpus
        build_corpus(corpus.tasks[:-1]).write(out)
        assert main(["check", "--plan", "tiny", "--corpus", str(out)]) == EXIT_GATE_FAILED
        captured = capsys.readouterr()
        assert "committed" in captured.err
        assert "rebuilt" in captured.err

    def test_models_lists_the_catalogue(self, capsys):
        assert main(["models"]) == EXIT_OK
        assert "nano" in capsys.readouterr().out

    def test_models_json_carries_the_skill_curve(self, capsys):
        assert main(["models", "--json"]) == EXIT_OK
        entries = json.loads(capsys.readouterr().out)
        assert all(len(entry["accuracy_per_10k"]) == 5 for entry in entries)


class TestRoutingCommands:
    def test_fit_then_calibrate(self, tmp_path, capsys):
        corpus = tmp_path / "w.jsonl.gz"
        estimator = tmp_path / "e.json"
        main(["synth", "--plan", "measure", "--out", str(corpus)])
        assert main(["fit", "--corpus", str(corpus), "--out", str(estimator)]) == EXIT_OK
        assert "converged" in capsys.readouterr().out

        assert (
            main(
                [
                    "calibrate",
                    "--corpus",
                    str(corpus),
                    "--estimator",
                    str(estimator),
                    "--json",
                ]
            )
            == EXIT_OK
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["threshold_high"] >= payload["threshold_low"]
        assert 0 <= payload["blend_to_cheap"] <= 10_000

    def test_calibrate_says_its_own_number_is_optimistic(self, tmp_path, capsys):
        corpus = tmp_path / "w.jsonl.gz"
        estimator = tmp_path / "e.json"
        main(["synth", "--plan", "measure", "--out", str(corpus)])
        main(["fit", "--corpus", str(corpus), "--out", str(estimator)])
        capsys.readouterr()
        main(["calibrate", "--corpus", str(corpus), "--estimator", str(estimator)])
        assert "optimistic" in capsys.readouterr().out

    def test_route_names_the_ladder_and_the_reason(self, capsys):
        assert main(["route", "What is 4 + 4?"]) == EXIT_OK
        printed = capsys.readouterr().out
        assert "ladder" in printed
        assert "reason" in printed

    def test_route_refuses_a_fitted_policy_with_no_estimator(self, capsys):
        assert main(["route", "hello", "--policy", "fitted"]) == EXIT_USAGE
        assert "estimator" in capsys.readouterr().err

    def test_ask_returns_a_verdict(self, capsys):
        assert main(["ask", "What is 3 + 3?"]) == EXIT_OK
        assert "cost" in capsys.readouterr().out


class TestMeasurementCommands:
    def test_evaluate_records_then_enforces_a_baseline(self, tmp_path, capsys):
        fit_corpus = tmp_path / "fit.jsonl.gz"
        control = tmp_path / "control.jsonl.gz"
        baseline = tmp_path / "baseline.json"
        main(["synth", "--plan", "tiny", "--out", str(fit_corpus)])
        main(
            [
                "synth",
                "--plan",
                "tiny",
                "--out",
                str(control),
                "--disjoint-from",
                str(fit_corpus),
            ]
        )
        capsys.readouterr()

        common = [
            "evaluate",
            "--corpus",
            str(fit_corpus),
            "--control",
            str(control),
            "--baseline",
            str(baseline),
            "--quiet",
        ]
        assert main([*common, "--update-baseline"]) == EXIT_OK
        assert baseline.exists()
        capsys.readouterr()

        assert main(common) == EXIT_OK
        assert "no regression" in capsys.readouterr().out

    def test_evaluate_writes_every_report_it_is_asked_for(self, tmp_path, capsys):
        fit_corpus = tmp_path / "fit.jsonl.gz"
        control = tmp_path / "control.jsonl.gz"
        main(["synth", "--plan", "tiny", "--out", str(fit_corpus)])
        main(
            [
                "synth",
                "--plan",
                "tiny",
                "--out",
                str(control),
                "--disjoint-from",
                str(fit_corpus),
            ]
        )
        capsys.readouterr()
        assert (
            main(
                [
                    "evaluate",
                    "--corpus",
                    str(fit_corpus),
                    "--control",
                    str(control),
                    "--quiet",
                    "--json-out",
                    str(tmp_path / "r.json"),
                    "--markdown-out",
                    str(tmp_path / "r.md"),
                    "--junit-out",
                    str(tmp_path / "r.xml"),
                ]
            )
            == EXIT_OK
        )
        assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["workloads"]
        assert "cascade illusion" in (tmp_path / "r.md").read_text(encoding="utf-8")
        assert "<testsuite" in (tmp_path / "r.xml").read_text(encoding="utf-8")

    def test_update_baseline_without_a_baseline_path_is_a_usage_error(self, tmp_path, capsys):
        fit_corpus = tmp_path / "fit.jsonl.gz"
        control = tmp_path / "control.jsonl.gz"
        main(["synth", "--plan", "tiny", "--out", str(fit_corpus)])
        main(
            [
                "synth",
                "--plan",
                "tiny",
                "--out",
                str(control),
                "--disjoint-from",
                str(fit_corpus),
            ]
        )
        capsys.readouterr()
        assert (
            main(
                [
                    "evaluate",
                    "--corpus",
                    str(fit_corpus),
                    "--control",
                    str(control),
                    "--quiet",
                    "--update-baseline",
                ]
            )
            == EXIT_USAGE
        )

    def test_resilience_prints_both_capacity_arms(self, tmp_path, capsys):
        corpus = tmp_path / "w.jsonl.gz"
        main(["synth", "--plan", "tiny", "--out", str(corpus)])
        capsys.readouterr()
        assert (
            main(
                [
                    "resilience",
                    "--corpus",
                    str(corpus),
                    "--json-out",
                    str(tmp_path / "s.json"),
                ]
            )
            == EXIT_OK
        )
        printed = capsys.readouterr().out
        assert "under-provisioned" in printed
        assert "outage" in printed
        assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))

    def test_doctor_checks_the_installation(self, capsys):
        assert main(["doctor"]) == EXIT_OK
        assert "reproduces" in capsys.readouterr().out
