"""The task table. Run through ``python tasks.py <name>``.

Standard library only, because a task runner that needs its own dependency
installed before it can install dependencies is a bootstrap problem nobody
asked for.
"""

from __future__ import annotations

from collections.abc import Sequence

IMAGE = "ai-model-gateway"

EXAMPLES = "examples"
FIT = f"{EXAMPLES}/fit.jsonl.gz"
CONTROL = f"{EXAMPLES}/control.jsonl.gz"
SHIFTS = tuple(f"{EXAMPLES}/shift-{share}.jsonl.gz" for share in (10, 50, 70, 90))
ESTIMATOR = f"{EXAMPLES}/estimator.json"
BASELINE = f"{EXAMPLES}/baseline.json"


def _run(*args: str) -> list[str]:
    return ["uv", "run", *args]


def _amg(*args: str) -> list[str]:
    return _run("python", "-m", "amg", *args)


_EVALUATE = (
    "evaluate",
    "--corpus",
    FIT,
    "--control",
    CONTROL,
    *[argument for path in SHIFTS for argument in ("--shift", path)],
    "--baseline",
    BASELINE,
)


TASKS: dict[str, tuple[str, list[Sequence[str]]]] = {
    "setup": (
        "Install the project and its development tooling.",
        [["uv", "sync", "--locked", "--group", "dev", "--group", "docs"]],
    ),
    "fmt": ("Format.", [_run("ruff", "format", ".")]),
    "lint": (
        "Lint and check formatting.",
        [_run("ruff", "check", "."), _run("ruff", "format", "--check", ".")],
    ),
    "typecheck": ("Type check under mypy --strict.", [_run("mypy")]),
    "test": (
        "Run the whole test suite with the coverage gate.",
        [_run("pytest", "--cov", "--cov-report=term-missing", "--cov-fail-under=88")],
    ),
    "test-unit": ("Unit tests only.", [_run("pytest", "-m", "unit")]),
    "test-integration": ("Integration tests only.", [_run("pytest", "-m", "integration")]),
    "test-security": (
        "Adversarial tests only. A failure here is a security regression.",
        [_run("pytest", "-m", "security")],
    ),
    "test-e2e": (
        "End-to-end tests only: the CLI and the HTTP surface as real processes.",
        [_run("pytest", "-m", "e2e")],
    ),
    "test-meta": (
        "The gate's own negative controls: break one thing, assert it goes red.",
        [_run("pytest", "-m", "meta")],
    ),
    # -- the shipped artefacts, rebuilt from nothing ------------------------
    "workloads": (
        "Regenerate every workload. All but the fitting one exclude it.",
        [
            _amg("synth", "--plan", "fit", "--out", FIT),
            _amg("synth", "--plan", "measure", "--out", CONTROL, "--disjoint-from", FIT),
            *[
                _amg(
                    "synth",
                    "--plan",
                    f"shift-{share}",
                    "--out",
                    f"{EXAMPLES}/shift-{share}.jsonl.gz",
                    "--disjoint-from",
                    FIT,
                )
                for share in (10, 50, 70, 90)
            ],
        ],
    ),
    "workloads-check": (
        "Do the committed workloads still match their plans? The CI check.",
        [
            _amg("check", "--plan", "fit", "--corpus", FIT),
            _amg("check", "--plan", "measure", "--corpus", CONTROL, "--disjoint-from", FIT),
            *[
                _amg(
                    "check",
                    "--plan",
                    f"shift-{share}",
                    "--corpus",
                    f"{EXAMPLES}/shift-{share}.jsonl.gz",
                    "--disjoint-from",
                    FIT,
                )
                for share in (10, 50, 70, 90)
            ],
        ],
    ),
    "fit": (
        "Fit the difficulty estimator on the fitting workload.",
        [_amg("fit", "--corpus", FIT, "--out", ESTIMATOR)],
    ),
    "calibrate": (
        "Calibrate routing thresholds and the spend-matched null's shares.",
        [_amg("calibrate", "--corpus", FIT, "--estimator", ESTIMATOR)],
    ),
    "evaluate": (
        "The routing experiment, gated against the committed baseline.",
        [
            _amg(
                *_EVALUATE,
                "--with-resilience",
                "--json-out",
                "reports/evaluation.json",
                "--markdown-out",
                "reports/evaluation.md",
                "--junit-out",
                "reports/evaluation.xml",
            )
        ],
    ),
    "baseline": (
        "Re-record the committed baseline. Commit the result on its own.",
        [_amg(*_EVALUATE, "--update-baseline", "--with-resilience")],
    ),
    "resilience": (
        "Sweep faults, arms and capacity; print where each one stops helping.",
        [_amg("resilience", "--corpus", CONTROL)],
    ),
    "models": ("The upstream catalogue.", [_amg("models")]),
    "doctor": ("Check that this installation works end to end.", [_amg("doctor")]),
    "check-gateway": (
        "Assert the gates fire: the shipped binary against the shipped workloads.",
        [_run("python", "scripts/check-gateway.py")],
    ),
    "examples": (
        "Run every example. They are documentation that executes.",
        [
            _run("python", "examples/quickstart.py"),
            _run("python", "examples/routing_holdout_demo.py"),
            _run("python", "examples/resilience_demo.py"),
        ],
    ),
    "site": (
        "Build the documentation site into _site.",
        [_run("--only-group", "docs", "python", "scripts/build_site.py", "--output", "_site")],
    ),
    "security": (
        "Local security scans.",
        [
            _run("bandit", "-c", "pyproject.toml", "-r", "src", "-f", "screen"),
            [
                "uv",
                "export",
                "--locked",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                "requirements.audit.txt",
            ],
            [
                "uv",
                "tool",
                "run",
                "pip-audit",
                "--strict",
                "--no-deps",
                "--requirement",
                "requirements.audit.txt",
            ],
        ],
    ),
    "docker-build": (
        "Build the container image.",
        [["docker", "build", "-t", f"{IMAGE}:local", "."]],
    ),
    "smoke": (
        "Build the image and run the smoke test against it.",
        [
            ["docker", "build", "-t", f"{IMAGE}:local", "."],
            ["bash", "scripts/smoke-test.sh", f"{IMAGE}:local"],
        ],
    ),
}

#: The order CI runs things in, cheapest gate first. Formatting and typing fail
#: in seconds; the evaluation takes minutes. A developer who broke an import
#: should learn that before the suite has finished collecting.
ALL = (
    "lint",
    "typecheck",
    "test",
    "workloads-check",
    "evaluate",
    "check-gateway",
    "examples",
    "site",
)

# Expanded here rather than special-cased in the runner: tasks.py runs whatever
# command list it finds, and a task carrying an empty one would print nothing
# and exit 0. Nothing about that looks wrong on a terminal, which is what makes
# it worth catching.
TASKS["all"] = (
    "Everything CI runs, in the order CI runs it.",
    [step for name in ALL for step in TASKS[name][1]],
)
