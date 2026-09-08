"""Reading and writing a workload, and proving it is the one that was measured.

A corpus is JSON Lines, optionally gzipped, chosen by file suffix. It carries a
SHA-256 digest over its *content* rather than over its bytes, so a corpus stays
verifiable across a gzip implementation change or a re-compression.

Three details are load-bearing rather than incidental:

**The gzip member is written with `mtime=0` and an empty filename.** Two
separate traps, both of which put non-content bytes into the file. Without
``mtime=0`` the same corpus written twice differs by a timestamp. Without
``filename=""`` it differs by the *path it was written to*, because
:class:`gzip.GzipFile` takes the FNAME header field from the file object it was
handed -- so ``a.jsonl.gz`` and ``b.jsonl.gz`` holding identical corpora are not
identical files. Either one makes a committed byte-level digest uncheckable, and
the drift gate then becomes noise that everyone learns to ignore.

**The size limit applies to the decompressed stream.** Checking ``stat()``
alone accepts a 200 KB file that expands to gigabytes, and the runner is then
killed by the OOM killer -- which reaches an operator as "the gate is flaky"
rather than as a malformed input.

**The digest covers the fields a consumer reads**, in a fixed order, and not the
file's formatting. Two corpora that produce identical routing decisions have
identical digests even if one was written by a different json version.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from amg.errors import ConfigError
from amg.workload.tasks import MAX_DIFFICULTY, MIN_DIFFICULTY, Task

#: Refuse a workload larger than this once decompressed. Generous for anything
#: this project generates and small enough that a decompression bomb fails as an
#: error rather than as an out-of-memory kill.
MAX_CORPUS_BYTES: Final[int] = 256 * 1024 * 1024

#: Refuse a workload with more rows than this.
#:
#: **A byte limit is not a memory limit, and assuming it was is a bug this
#: project shipped and then measured.** `_Bounded` caps the decompressed
#: *stream*, but every row that gets past it becomes a `Task` object, and a
#: parsed `Task` costs several hundred bytes for a line of about seventy. A
#: bomb sized just under `MAX_CORPUS_BYTES` therefore allocates gigabytes of
#: objects and dies with `MemoryError` *before* the byte bound is ever reached.
#:
#: The failure looks like an out-of-memory kill rather than a refusal, which is
#: exactly the outcome the byte bound existed to prevent -- an operator reads it
#: as "the gate is flaky", not as "somebody handed us a malformed input".
#:
#: Two orders of magnitude above the largest shipped workload (4,800).
MAX_CORPUS_TASKS: Final[int] = 500_000

#: Difficulty at or above which a task counts as "hard". The workload-shift
#: parameter is defined against this boundary, so it lives here rather than
#: being spelt out at each site that measures the mix.
HARD_FROM: Final[int] = 4

#: The field order the digest is computed over. Adding a field to `Task` without
#: adding it here would leave the digest blind to it, so a test asserts the two
#: agree.
DIGEST_FIELDS: Final[tuple[str, ...]] = (
    "task_id",
    "family",
    "difficulty",
    "prompt",
    "answer",
)


class _Bounded(io.RawIOBase):
    """A reader that refuses to yield more than *limit* bytes.

    Wrapped around the *decompressed* stream, which is the only place the limit
    means anything.
    """

    def __init__(self, wrapped: IO[bytes], limit: int) -> None:
        self._wrapped = wrapped
        self._limit = limit
        self._seen = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        chunk = self._wrapped.read(len(buffer))
        if not chunk:
            return 0
        self._seen += len(chunk)
        if self._seen > self._limit:
            raise ConfigError(
                f"workload exceeds {self._limit} bytes once decompressed",
                remedy="Regenerate it with `amg synth`, or raise MAX_CORPUS_BYTES.",
            )
        buffer[: len(chunk)] = chunk
        return len(chunk)


@dataclass(frozen=True, slots=True)
class Corpus:
    """A workload, and the questions asked of it often enough to name."""

    tasks: tuple[Task, ...]

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self) -> Iterator[Task]:
        return iter(self.tasks)

    @property
    def families(self) -> tuple[str, ...]:
        """Every family present, sorted, so a report's row order is stable."""
        return tuple(sorted({task.family for task in self.tasks}))

    @property
    def hard_share(self) -> float:
        """The share of tasks at difficulty 4 or 5.

        The workload-shift parameter, measured off the corpus rather than
        carried alongside it -- a corpus that has been filtered or subsetted
        reports what it actually contains rather than what it was asked for.
        """
        if not self.tasks:
            return 0.0
        hard = sum(1 for task in self.tasks if task.difficulty >= HARD_FROM)
        return hard / len(self.tasks)

    def by_difficulty(self) -> dict[int, int]:
        """How many tasks sit at each level, including the empty levels."""
        counts = dict.fromkeys(range(MIN_DIFFICULTY, MAX_DIFFICULTY + 1), 0)
        for task in self.tasks:
            counts[task.difficulty] += 1
        return counts

    def digest(self) -> str:
        """SHA-256 over the content a consumer reads, not over the file bytes."""
        hasher = hashlib.sha256()
        for task in self.tasks:
            row = {field: getattr(task, field) for field in DIGEST_FIELDS}
            hasher.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
            hasher.update(b"\n")
        return f"sha256:{hasher.hexdigest()}"

    def write(self, path: Path) -> Path:
        """Write to *path*, gzipping when the suffix says to."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(
                {field: getattr(task, field) for field in DIGEST_FIELDS},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for task in self.tasks
        ).encode()
        if path.suffix == ".gz":
            # mtime=0 and filename="": both header fields would otherwise carry
            # something that is not the corpus -- the clock, and the path.
            with (
                path.open("wb") as raw,
                gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out,
            ):
                out.write(payload)
        else:
            path.write_bytes(payload)
        return path


def build_corpus(tasks: Iterable[Task]) -> Corpus:
    """Assemble a corpus, checking every task before it becomes one."""
    collected = tuple(tasks)
    for task in collected:
        if not MIN_DIFFICULTY <= task.difficulty <= MAX_DIFFICULTY:
            raise ConfigError(f"task {task.task_id!r} has difficulty {task.difficulty}")
    identifiers = {task.task_id for task in collected}
    if len(identifiers) != len(collected):
        raise ConfigError(
            "task ids must be unique",
            remedy=(
                "Every deterministic draw in this project is keyed on the task id, so "
                "two tasks sharing one would silently share an upstream's verdict."
            ),
        )
    return Corpus(tasks=collected)


def read_corpus(path: Path) -> Corpus:
    """Read a workload, bounding the decompressed stream."""
    if not path.exists():
        raise ConfigError(
            f"no workload at {path}",
            remedy="Generate one with `amg synth --out <path>`.",
        )
    tasks: list[Task] = []
    with path.open("rb") as raw:
        stream: IO[bytes] = (
            gzip.GzipFile(fileobj=raw, mode="rb")  # type: ignore[assignment]
            if path.suffix == ".gz"
            else raw
        )
        with io.BufferedReader(_Bounded(stream, MAX_CORPUS_BYTES)) as bounded:
            for number, line in enumerate(io.TextIOWrapper(bounded, encoding="utf-8"), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ConfigError(f"{path}:{number} is not valid JSON: {error}") from error
                missing = set(DIGEST_FIELDS) - set(row)
                if missing:
                    raise ConfigError(f"{path}:{number} is missing {sorted(missing)}")
                if len(tasks) >= MAX_CORPUS_TASKS:
                    raise ConfigError(
                        f"{path} exceeds {MAX_CORPUS_TASKS} tasks",
                        remedy=(
                            "Regenerate it with `amg synth`, or raise "
                            "MAX_CORPUS_TASKS. Checked per row rather than at "
                            "the end, because the point is to stop allocating."
                        ),
                    )
                tasks.append(
                    Task(
                        task_id=str(row["task_id"]),
                        family=str(row["family"]),
                        difficulty=int(row["difficulty"]),
                        prompt=str(row["prompt"]),
                        answer=str(row["answer"]),
                    )
                )
    return build_corpus(tasks)
