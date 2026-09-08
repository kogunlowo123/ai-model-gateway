"""The workload: generation, disjointness, reproducibility, and its file format."""

from __future__ import annotations

import gzip
from dataclasses import fields, replace

import pytest

from amg.errors import ConfigError
from amg.workload.build import PLANS, Plan, difficulty_histogram, generate, plan_named
from amg.workload.corpus import (
    DIGEST_FIELDS,
    MAX_CORPUS_BYTES,
    MAX_CORPUS_TASKS,
    Corpus,
    build_corpus,
    read_corpus,
)
from amg.workload.tasks import (
    FAMILY_NAMES,
    MAX_DIFFICULTY,
    Task,
    make_task,
    parse_answer,
    render,
)

pytestmark = pytest.mark.integration


class TestGeneration:
    def test_the_same_plan_produces_the_same_workload(self):
        assert generate(PLANS["tiny"]).corpus.digest() == generate(PLANS["tiny"]).corpus.digest()

    def test_a_different_seed_produces_a_different_workload(self):
        other = replace(PLANS["tiny"], seed=PLANS["tiny"].seed + 1)
        assert generate(PLANS["tiny"]).corpus.digest() != generate(other).corpus.digest()

    def test_exclusion_makes_the_second_workload_disjoint_by_construction(self):
        first = generate(PLANS["tiny"]).corpus
        second = generate(
            replace(PLANS["tiny"], name="other", seed=99),
            exclude=frozenset(task.prompt for task in first),
        ).corpus
        assert not {t.prompt for t in first} & {t.prompt for t in second}

    def test_the_hard_share_lands_near_what_the_plan_asked_for(self):
        corpus = generate(replace(PLANS["tiny"], size=600, hard_share=0.7)).corpus
        assert 0.6 < corpus.hard_share < 0.8

    def test_every_family_appears(self):
        corpus = generate(PLANS["tiny"]).corpus
        assert set(corpus.families) == set(FAMILY_NAMES)

    def test_collisions_stay_low_at_the_shipped_scale(self):
        # A family that exhausts its grammar silently stops matching the
        # difficulty mix its plan claims, so this is a property of the corpus
        # rather than a performance nicety.
        result = generate(PLANS["measure"])
        assert result.collision_rate < 0.05

    def test_a_plan_smaller_than_its_family_count_is_refused(self):
        with pytest.raises(ConfigError, match="at least"):
            Plan(name="x", seed=1, size=2, hard_share=0.3)

    def test_a_hard_share_outside_zero_to_one_is_refused(self):
        with pytest.raises(ConfigError, match="proportion"):
            Plan(name="x", seed=1, size=60, hard_share=1.5)

    def test_an_unknown_family_is_refused(self):
        with pytest.raises(ConfigError, match="unknown task families"):
            Plan(name="x", seed=1, size=60, hard_share=0.3, families=("telepathy",))

    def test_an_unknown_plan_lists_the_known_ones(self):
        with pytest.raises(ConfigError) as caught:
            plan_named("nonexistent")
        assert "Known plans" in (caught.value.remedy or "")

    def test_the_histogram_covers_every_level(self):
        text = difficulty_histogram(generate(PLANS["tiny"]).corpus)
        assert all(f"{level}:" in text for level in range(1, MAX_DIFFICULTY + 1))


class TestTasks:
    def test_every_family_agrees_with_its_own_answer(self):
        # A family whose stated answer does not satisfy its own parser would
        # make one whole family unanswerable, and the result would read as a
        # routing finding rather than as a corpus bug.
        corpus = generate(replace(PLANS["tiny"], size=600)).corpus
        for task in corpus:
            assert task.is_correct(render(task.answer))

    def test_a_difficulty_outside_the_scale_is_refused(self):
        import random

        from amg.workload.tasks import BY_NAME

        with pytest.raises(ValueError, match="difficulty must lie"):
            make_task(BY_NAME["arithmetic"], 9, random.Random(1), "x")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ('{"answer": "42"}', "42"),
            ('{"answer": 42}', "42"),
            ('{"answer": 42.0}', "42"),
            ('{"answer": 42.5}', "42.5"),
            ('{"answer": true}', "true"),
            ('{"answer": "  42 "}', "42"),
        ],
    )
    def test_answers_are_normalised_across_json_types(self, response, expected):
        # A real model returns a number as a JSON number about as often as a
        # string, and `{"answer": 47.0}` and `{"answer": 47}` are the same
        # answer to everyone except a pedantic parser.
        assert parse_answer(response) == expected

    @pytest.mark.parametrize("response", ["", "the answer is 42", '{"result": 42}', "[42]", "null"])
    def test_malformed_responses_parse_to_none(self, response):
        assert parse_answer(response) is None


class TestCorpusFile:
    def test_a_round_trip_preserves_the_digest(self, tmp_path):
        corpus = generate(PLANS["tiny"]).corpus
        path = corpus.write(tmp_path / "w.jsonl.gz")
        assert read_corpus(path).digest() == corpus.digest()

    def test_plain_json_lines_round_trip_too(self, tmp_path):
        corpus = generate(PLANS["tiny"]).corpus
        path = corpus.write(tmp_path / "w.jsonl")
        assert read_corpus(path).digest() == corpus.digest()

    def test_the_gzip_member_carries_no_bytes_that_are_not_the_corpus(self, tmp_path):
        # Two separate header traps. Without mtime=0 the same corpus written
        # twice differs by a timestamp; without filename="" it differs by the
        # *path*, because GzipFile takes the FNAME field from the file object
        # it is handed. Writing to two different names catches the second.
        corpus = generate(PLANS["tiny"]).corpus
        first = corpus.write(tmp_path / "a.jsonl.gz").read_bytes()
        second = corpus.write(tmp_path / "b.jsonl.gz").read_bytes()
        assert first == second

    def test_the_digest_covers_every_field_a_consumer_reads(self):
        # A field added to Task without being added to DIGEST_FIELDS would leave
        # the digest blind to it, and the drift gate would stop seeing changes.
        assert set(DIGEST_FIELDS) == {field.name for field in fields(Task)}

    def test_a_missing_file_says_how_to_make_one(self, tmp_path):
        with pytest.raises(ConfigError) as caught:
            read_corpus(tmp_path / "absent.jsonl.gz")
        assert "amg synth" in (caught.value.remedy or "")

    def test_a_malformed_line_names_its_line_number(self, tmp_path):
        path = tmp_path / "w.jsonl"
        # A valid first line, so the failure reported is the one on line 2
        # rather than a missing field on line 1.
        good = '{"answer":"a","difficulty":1,"family":"f","prompt":"p","task_id":"a"}'
        path.write_text(good + "\nnot json\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=":2 is not valid JSON"):
            read_corpus(path)

    def test_a_line_missing_a_field_says_which(self, tmp_path):
        path = tmp_path / "w.jsonl"
        path.write_text('{"task_id": "a"}\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="is missing"):
            read_corpus(path)

    def test_blank_lines_are_skipped(self, tmp_path):
        corpus = generate(PLANS["tiny"]).corpus
        path = corpus.write(tmp_path / "w.jsonl")
        path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        assert len(read_corpus(path)) == len(corpus)

    def test_a_decompression_bomb_is_refused_rather_than_exhausting_memory(self, tmp_path):
        # Checking stat() alone accepts a small file that expands to gigabytes,
        # and the runner is then killed by the OOM killer -- which reaches an
        # operator as "the gate is flaky" rather than as a malformed input.
        #
        # The row limit is what actually catches this one, and finding that out
        # was the point. A byte limit is not a memory limit: every row that
        # gets past `_Bounded` becomes a Task object costing several hundred
        # bytes for a line of about seventy, so a bomb sized just under
        # MAX_CORPUS_BYTES raised MemoryError before the byte bound was
        # reached. This test wrote enough rows to do exactly that.
        path = tmp_path / "bomb.jsonl.gz"
        line = b'{"task_id":"x","family":"f","difficulty":1,"prompt":"p","answer":"a"}\n'
        with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
            out.write(line * (MAX_CORPUS_TASKS + 10))
        assert path.stat().st_size < MAX_CORPUS_BYTES
        with pytest.raises(ConfigError, match="exceeds"):
            read_corpus(path)

    def test_the_byte_bound_still_fires_on_rows_too_long_to_count(self, tmp_path):
        # The other half of the pair. A file with few rows but enormous ones
        # never reaches the row limit, so the byte bound has to be the thing
        # that stops it -- and a test that only exercised one of the two would
        # leave the other free to be deleted.
        path = tmp_path / "wide.jsonl.gz"
        row = b'{"task_id":"x","family":"f","difficulty":1,"answer":"a","prompt":"'
        with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
            out.write(row)
            for _ in range(MAX_CORPUS_BYTES // 1_000_000 + 2):
                out.write(b"p" * 1_000_000)
            out.write(b'"}\n')
        assert path.stat().st_size < MAX_CORPUS_BYTES
        with pytest.raises(ConfigError, match="bytes once decompressed"):
            read_corpus(path)

    def test_duplicate_task_ids_are_refused(self):
        # Every deterministic draw is keyed on the task id, so two tasks sharing
        # one would silently share an upstream's verdict.
        task = Task(task_id="dup", family="f", difficulty=1, prompt="p", answer="a")
        with pytest.raises(ConfigError, match="unique"):
            build_corpus([task, replace(task, prompt="q")])

    def test_a_task_outside_the_difficulty_scale_is_refused(self):
        with pytest.raises(ConfigError, match="difficulty"):
            build_corpus([Task(task_id="a", family="f", difficulty=99, prompt="p", answer="a")])

    def test_an_empty_corpus_reports_zero_rather_than_dividing_by_it(self):
        empty = Corpus(tasks=())
        assert empty.hard_share == 0.0
        assert empty.families == ()
