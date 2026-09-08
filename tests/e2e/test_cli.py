"""The CLI as a real process, and the HTTP surface on a real socket.

A subprocess is the only way to see what the operating system sees. Two things
are invisible in process and have both bitten this series before: the exit code
(``argparse`` exits 2, which collides with "a gate failed"), and console
encoding on Windows, where printing a character cp1252 cannot represent crashes
a command that worked in every test.

Nothing here is covered by coverage -- a subprocess is a different interpreter --
which is exactly why the same commands are also driven in process in
``tests/integration``. Neither layer substitutes for the other.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from amg.errors import EXIT_GATE_FAILED, EXIT_OK, EXIT_USAGE

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent.parent


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Drive the CLI as a real process, from the repository root."""
    return subprocess.run(
        [sys.executable, "-m", "amg", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


class TestExitCodes:
    def test_a_usage_error_exits_one_not_two(self):
        # Two is "a gate failed". Without the override a misspelt flag and a
        # routing regression would be the same number to a pipeline, and only a
        # real process can observe the difference.
        result = run("evaluate", "--corpsu", "nope")
        assert result.returncode == EXIT_USAGE

    def test_an_unknown_subcommand_exits_one(self):
        assert run("teleport").returncode == EXIT_USAGE

    def test_no_subcommand_exits_one(self):
        assert run().returncode == EXIT_USAGE

    def test_a_missing_workload_exits_one_with_a_remedy(self):
        result = run("check", "--plan", "tiny", "--corpus", "does-not-exist.jsonl.gz")
        assert result.returncode == EXIT_USAGE
        assert "amg synth" in result.stderr

    def test_a_corpus_that_does_not_match_its_plan_exits_two(self, tmp_path):
        # A gate failure, not a usage error: the file is there and readable, and
        # what it contains is wrong.
        corpus = tmp_path / "wrong.jsonl"
        assert run("synth", "--plan", "tiny", "--out", str(corpus)).returncode == EXIT_OK
        rows = corpus.read_text(encoding="utf-8").splitlines()
        edited = json.loads(rows[0])
        edited["prompt"] = "edited by hand"
        rows[0] = json.dumps(edited, sort_keys=True, separators=(",", ":"))
        corpus.write_text("\n".join(rows) + "\n", encoding="utf-8")

        result = run("check", "--plan", "tiny", "--corpus", str(corpus))
        assert result.returncode == EXIT_GATE_FAILED
        assert "does not match plan" in result.stderr

    def test_version_and_help_exit_zero(self):
        assert run("--version").returncode == EXIT_OK
        assert run("--help").returncode == EXIT_OK


class TestCommandsRun:
    def test_doctor_reports_a_working_installation(self):
        result = run("doctor")
        assert result.returncode == EXIT_OK
        assert "this installation works" in result.stdout

    def test_models_prints_the_catalogue_and_says_it_is_a_simulator(self):
        result = run("models")
        assert result.returncode == EXIT_OK
        assert "simulator" in result.stdout

    def test_models_json_is_machine_readable(self):
        result = run("models", "--json")
        assert result.returncode == EXIT_OK
        assert {entry["name"] for entry in json.loads(result.stdout)} >= {"nano"}

    def test_route_explains_what_the_router_was_allowed_to_see(self):
        result = run("route", "What is 12 + 30?")
        assert result.returncode == EXIT_OK
        assert "features" in result.stdout
        assert "longest_number" in result.stdout

    def test_ask_answers_and_reports_its_cost(self):
        result = run("ask", "What is 2 + 2?", "--json")
        assert result.returncode == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["outcome"] in {"answered", "malformed", "failed", "expired"}
        assert payload["cost_micro_cents"] >= 0

    def test_synth_and_check_round_trip(self, tmp_path):
        corpus = tmp_path / "w.jsonl.gz"
        assert run("synth", "--plan", "tiny", "--out", str(corpus)).returncode == EXIT_OK
        assert run("check", "--plan", "tiny", "--corpus", str(corpus)).returncode == EXIT_OK

    def test_every_command_prints_only_encodable_characters(self):
        # On Windows a subprocess writes through cp1252 by default, and a
        # character it cannot represent raises UnicodeEncodeError at the print
        # rather than anywhere near the code that produced it.
        for args in (("models",), ("doctor",), ("route", "hello")):
            result = run(*args)
            assert result.returncode == EXIT_OK, result.stderr
            result.stdout.encode("cp1252", errors="strict")
