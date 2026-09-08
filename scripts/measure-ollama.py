#!/usr/bin/env python
"""Measure real local models on the generated tasks, and diff them against the table.

This is the script that stops :mod:`amg.upstream.simulated` being an unfalsifiable
claim. It asks real models the same generated tasks the simulator answers,
scores them with the same parser, and prints the two accuracy curves next to
each other.

**It is deliberately not in CI**, and not because it is slow. It needs a local
Ollama server with specific models pulled, its results move with quantisation
and hardware, and a real model is not reproducible enough for a gate. Wiring it
into the pipeline would produce a check that fails on other people's machines
for reasons they cannot act on -- which is how a suite gets ignored.

So it is run by hand, its output is committed to ``examples/ollama/``, and
``docs/simulator.md`` reads from what it found. If the table is ever changed,
this is what should be re-run.

Usage::

    uv run --extra ollama python scripts/measure-ollama.py
    uv run --extra ollama python scripts/measure-ollama.py --models qwen2.5:3b --size 60
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amg.upstream.ollama import (
    DEFAULT_BASE_URL,
    available,
    measure,
    require,
    to_json,
)
from amg.upstream.simulated import BY_NAME
from amg.workload.build import PLANS, generate
from amg.workload.tasks import FAMILY_NAMES, MAX_DIFFICULTY

#: What to compare each real model against. The simulator's three tiers, in the
#: order a cascade climbs them.
TIERS = ("nano", "mini", "flagship")

OUT_DIR = ROOT / "examples" / "ollama"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=None, help="Ollama tags to measure.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--size",
        type=int,
        default=150,
        help="Tasks per model. Small on purpose: a real model is slow, and this "
        "is a sanity check on a stipulated table rather than a benchmark.",
    )
    args = parser.parse_args()

    tags = args.models or list(available(args.base_url))
    if not tags:
        print(
            f"no Ollama server with models at {args.base_url}.\n"
            "Start one with `ollama serve` and pull a model, e.g. `ollama pull qwen2.5:3b`.",
            file=sys.stderr,
        )
        return 3

    # An equal number of tasks in every (difficulty, family) cell, rather than
    # the shipped hard share.
    #
    # Two reasons, and the second one cost a whole measurement run. What is
    # under test is the *shape* of the skill curve, and the shipped mix would
    # put most of a small sample into one band. But taking the first N of each
    # band is not enough either: the generated corpus is grouped by family, so
    # a slice gives each band a different family mix, and a model that is bad
    # at one family then looks bad at whichever difficulty that family happened
    # to land in. The first run of this script showed a 3B model scoring 65% at
    # difficulty 1 and 95% at difficulty 2, which was a fact about the sample
    # rather than about the model.
    plan = replace(PLANS["measure"], name="ollama", size=max(args.size * 10, 600), hard_share=0.5)
    generated = generate(plan).corpus
    cells = len(FAMILY_NAMES) * MAX_DIFFICULTY
    per_cell = max(1, args.size // cells)
    corpus = [
        task
        for difficulty in range(1, MAX_DIFFICULTY + 1)
        for family in FAMILY_NAMES
        for task in [
            candidate
            for candidate in generated
            if candidate.difficulty == difficulty and candidate.family == family
        ][:per_cell]
    ]
    print(
        f"{len(corpus)} tasks: {per_cell} in each of {cells} "
        f"(difficulty, family) cells, {per_cell * len(FAMILY_NAMES)} per band"
    )
    print()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tag in tags:
        upstream = require(tag, args.base_url)
        print(f"--- {tag} " + "-" * (56 - len(tag)))
        observed = measure(upstream, corpus)
        path = OUT_DIR / f"{tag.replace(':', '-').replace('/', '-')}.json"
        path.write_text(to_json(upstream, observed) + "\n", encoding="utf-8")

        print(f"{'diff':>5}{'asked':>7}{'correct':>9}{'malformed':>11}{'errors':>8}", end="")
        print("".join(f"{name:>10}" for name in TIERS))
        for row in observed:
            simulated = "".join(
                f"{BY_NAME[name].accuracy[row.difficulty - 1] / 100:>9.1f}%" for name in TIERS
            )
            print(
                f"{row.difficulty:>5}{row.asked:>7}"
                f"{row.accuracy_per_10k / 100:>8.1f}%{row.malformed:>11}{row.errors:>8}"
                f"{simulated}"
            )
        print(f"wrote {path.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
