#!/usr/bin/env python
"""Assert the gates fire. Break one thing at a time and demand a red build.

Everything else in CI checks that this gateway *works*. This checks that the
checks work, by driving the shipped command-line binary as a real process
against the shipped workloads, breaking one thing, and failing if the exit code
comes back green.

That is the whole argument of this repository restated as a script. A pipeline
whose gates have only ever been observed passing is indistinguishable from
``exit 0``, and the difference between the two is invisible until the day
something actually regresses.

Each case below names the specific way a real pipeline goes quietly green:

* a **workload edited by hand** -- the fastest way to make a baseline pass is to
  change the corpus it was recorded against;
* a **missing workload**, which must be a *usage* error rather than a gate
  failure, because a pipeline that cannot tell "you typed the path wrong" from
  "quality regressed" will eventually be taught to ignore both;
* a **fitted policy with no estimator**, which every gateway in production is
  one bad deploy away from, and which must refuse rather than silently fall back
  to always-cheapest -- the failure mode that looks like a cost saving;
* a **blend with no calibrated shares**, the same refusal on the null's side;
* a **baseline recorded elsewhere**, which must not be accepted just because it
  parses.

Run it::

    python scripts/check-gateway.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GATE_FAILED = 2


def amg(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped binary as a real process, from the repository root."""
    # S603: the argument vector is a literal built here, never a shell string
    # and never anything a caller supplies. `sys.executable` is this very
    # interpreter. Running the binary as a real process is the whole point:
    # exit codes are what a pipeline reads, and they are invisible in process.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "amg", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


class Checks:
    """Runs the cases and remembers which ones did not do what they promised."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def expect(
        self,
        description: str,
        args: Sequence[str],
        *,
        code: int,
        says: str | None = None,
    ) -> None:
        """Assert the binary exits with *code*, and mentions *says* if given."""
        result = amg(*args)
        output = result.stdout + result.stderr
        if result.returncode != code:
            self.failures.append(
                f"{description}: expected exit {code}, got {result.returncode}\n"
                f"    {output.strip().splitlines()[-1] if output.strip() else '(no output)'}"
            )
            return
        if says is not None and says not in output:
            self.failures.append(f"{description}: exit {code} as expected, but never said {says!r}")
            return
        self.passed += 1
        print(f"  ok  {description}")

    def report(self) -> int:
        print()
        if self.failures:
            print(
                f"{len(self.failures)} of {self.passed + len(self.failures)} checks did not fire:"
            )
            for failure in self.failures:
                print(f"  - {failure}")
            print(
                "\nA gate that does not fire is worse than no gate: it is a gate\n"
                "everybody trusts. Fix the check before shipping."
            )
            return EXIT_GATE_FAILED
        print(f"all {self.passed} gates fired as specified")
        return EXIT_OK


def main() -> int:
    if not (EXAMPLES / "fit.jsonl.gz").exists():
        print(
            "no shipped workloads to check against.",
            "Run `python tasks.py workloads` first.",
            sep="\n",
            file=sys.stderr,
        )
        return 3

    checks = Checks()
    print("the gates, one at a time\n")

    # The green control. Without it, every red result below could be a fact
    # about a broken environment rather than about the gate.
    checks.expect(
        "the shipped workload matches its plan",
        ["check", "--plan", "fit", "--corpus", "examples/fit.jsonl.gz"],
        code=EXIT_OK,
        says="matches plan",
    )

    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)

        # An edited workload. The digest is over the task fields, so changing a
        # single prompt has to be enough.
        edited = scratch / "edited.jsonl"
        source = amg("synth", "--plan", "tiny", "--out", str(edited))
        if source.returncode != EXIT_OK:
            print(f"could not synthesise a workload to break: {source.stderr}", file=sys.stderr)
            return 3
        rows = edited.read_text(encoding="utf-8").splitlines()
        first = json.loads(rows[0])
        first["prompt"] = "edited by hand"
        rows[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        edited.write_text("\n".join(rows) + "\n", encoding="utf-8")
        checks.expect(
            "a workload edited by hand fails its plan check",
            ["check", "--plan", "tiny", "--corpus", str(edited)],
            code=EXIT_GATE_FAILED,
            says="does not match plan",
        )

        # A missing file is a usage error, not a gate failure. argparse would
        # exit 2 here by default, which is the same code as "quality
        # regressed" -- so this case is really pinning the override.
        checks.expect(
            "a missing workload is a usage error, not a regression",
            ["check", "--plan", "tiny", "--corpus", str(scratch / "absent.jsonl.gz")],
            code=EXIT_USAGE,
            says="amg synth",
        )

        # A baseline recorded against different corpora. It parses; it is still
        # not a measurement of this run.
        foreign = scratch / "foreign.json"
        baseline = json.loads((EXAMPLES / "baseline.json").read_text(encoding="utf-8"))
        baseline["correctness"]["a-workload-that-never-existed"] = {"cheapest": 0.5}
        foreign.write_text(json.dumps(baseline), encoding="utf-8")
        checks.expect(
            "a baseline naming a workload this run did not measure fails",
            [
                "evaluate",
                "--corpus",
                "examples/fit.jsonl.gz",
                "--control",
                "examples/control.jsonl.gz",
                "--baseline",
                str(foreign),
                "--quiet",
            ],
            code=EXIT_GATE_FAILED,
            says="did not measure",
        )

    # The two refusals. Both are the same shape: a policy that cannot be built
    # as asked must say so, because the alternative is a gateway that looks
    # healthy, costs less, and answers worse.
    checks.expect(
        "a fitted policy with no estimator refuses instead of degrading",
        ["route", "What is 2 + 2?", "--policy", "fitted"],
        code=EXIT_USAGE,
        says="estimator",
    )
    checks.expect(
        "a blend with no calibrated shares refuses instead of guessing",
        ["route", "What is 2 + 2?", "--policy", "blend"],
        code=EXIT_USAGE,
        says="calibrated",
    )
    checks.expect(
        "an unknown plan lists the plans that do exist",
        ["synth", "--plan", "no-such-plan", "--out", "-"],
        code=EXIT_USAGE,
        says="Known plans",
    )

    return checks.report()


if __name__ == "__main__":
    raise SystemExit(main())
