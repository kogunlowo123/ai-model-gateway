"""The virtual clock: ordering, fairness, abandonment, and the runaway guard."""

from __future__ import annotations

import pytest

from amg.clock import Pool, ProcessGenerator, Simulation, first_of
from amg.errors import RefusalError

pytestmark = pytest.mark.unit


class TestSimulation:
    def test_time_only_moves_when_something_waits(self):
        sim = Simulation()

        def process(sim: Simulation) -> ProcessGenerator:
            yield sim.timeout(1_500)

        sim.start(process(sim))
        sim.run()
        assert sim.now == 1_500

    def test_a_process_that_waits_for_nothing_finishes_inside_start(self):
        sim = Simulation()
        finished: list[int] = []

        def process(sim: Simulation) -> ProcessGenerator:
            finished.append(sim.now)
            # A process that finishes without ever waiting. `yield from ()`
            # rather than an unreachable bare `yield`: it makes this a
            # generator function without writing a statement no checker can
            # believe in.
            yield from ()

        sim.start(process(sim))
        assert finished == [0]

    def test_events_fire_in_time_order(self):
        sim = Simulation()
        order: list[str] = []

        def process(sim: Simulation, name: str, delay: int) -> ProcessGenerator:
            yield sim.timeout(delay)
            order.append(name)

        sim.start(process(sim, "late", 300))
        sim.start(process(sim, "early", 100))
        sim.start(process(sim, "middle", 200))
        sim.run()
        assert order == ["early", "middle", "late"]

    def test_ties_break_on_insertion_order_not_on_the_payload(self):
        # Without the sequence tie-breaker the heap would compare Event objects,
        # which are not orderable. The determinism of every measurement in this
        # project rests on this being a total order.
        sim = Simulation()
        order: list[int] = []

        def process(sim: Simulation, index: int) -> ProcessGenerator:
            yield sim.timeout(50)
            order.append(index)

        for index in range(8):
            sim.start(process(sim, index))
        sim.run()
        assert order == list(range(8))

    def test_the_same_scenario_twice_produces_the_same_interleaving(self):
        def build() -> list[str]:
            sim = Simulation()
            seen: list[str] = []
            pool = Pool(2)

            def worker(name: str, delay: int) -> ProcessGenerator:
                with (yield from pool.hold(sim)):
                    yield sim.timeout(delay)
                    seen.append(name)

            for index, delay in enumerate((30, 10, 20, 40, 5)):
                sim.start(worker(f"w{index}", delay))
            sim.run()
            return seen

        assert build() == build()

    def test_running_until_a_time_leaves_the_rest_queued(self):
        sim = Simulation()
        done: list[str] = []

        def process(sim: Simulation) -> ProcessGenerator:
            yield sim.timeout(5_000)
            done.append("finished")

        sim.start(process(sim))
        sim.run(until=1_000)
        assert sim.now == 1_000
        assert done == []
        sim.run()
        assert done == ["finished"]

    def test_a_runaway_process_is_refused_rather_than_hung(self):
        # A retry policy that never settles produces an unbounded event stream.
        # Hanging on it reaches an operator as "the gate is flaky".
        sim = Simulation(max_events=100)

        def spinner(sim: Simulation) -> ProcessGenerator:
            while True:
                yield sim.timeout(1)

        sim.start(spinner(sim))
        with pytest.raises(RefusalError, match="more than 100 events"):
            sim.run()

    def test_scheduling_into_the_past_is_refused(self):
        sim = Simulation()
        with pytest.raises(ValueError, match="in the past"):
            sim.timeout(-1)


class TestPool:
    def test_capacity_is_respected(self):
        sim = Simulation()
        pool = Pool(2)
        peak: list[int] = []

        def worker(sim: Simulation) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                peak.append(pool.in_use)
                yield sim.timeout(100)

        for _ in range(6):
            sim.start(worker(sim))
        sim.run()
        assert max(peak) == 2

    def test_waiters_are_served_first_in_first_out(self):
        sim = Simulation()
        pool = Pool(1)
        order: list[int] = []

        def worker(sim: Simulation, index: int) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                order.append(index)
                yield sim.timeout(10)

        for index in range(5):
            sim.start(worker(sim, index))
        sim.run()
        assert order == [0, 1, 2, 3, 4]

    def test_a_pool_needs_at_least_one_slot(self):
        with pytest.raises(ValueError, match="at least one slot"):
            Pool(0)

    def test_a_waiter_can_abandon_its_place_when_its_deadline_passes(self):
        sim = Simulation()
        pool = Pool(1)
        outcomes: list[str] = []

        def hog(sim: Simulation) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                yield sim.timeout(10_000)

        def impatient(sim: Simulation) -> ProcessGenerator:
            slot = yield from pool.hold_until(sim, 500)
            outcomes.append("got it" if slot else "gave up")

        sim.start(hog(sim))
        sim.start(impatient(sim))
        sim.run()
        assert outcomes == ["gave up"]
        assert pool.abandoned == 1

    def test_releasing_into_an_abandoned_waiter_does_not_lose_the_slot(self):
        # A slot handed to a process that has already given up is lost for the
        # rest of the run, and a pool that quietly shrinks deadlocks later in a
        # way that looks nothing like its cause.
        sim = Simulation()
        pool = Pool(1)
        served: list[str] = []

        def hog(sim: Simulation) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                yield sim.timeout(1_000)

        def impatient(sim: Simulation) -> ProcessGenerator:
            slot = yield from pool.hold_until(sim, 100)
            if slot is None:
                served.append("gave up")

        def patient(sim: Simulation) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                served.append("served")
                yield sim.timeout(10)

        sim.start(hog(sim))
        sim.start(impatient(sim))
        sim.start(patient(sim))
        sim.run()
        assert served == ["gave up", "served"]
        assert pool.in_use == 0

    def test_a_zero_deadline_gives_up_without_queueing(self):
        sim = Simulation()
        pool = Pool(1)
        outcomes: list[object] = []

        def hog(sim: Simulation) -> ProcessGenerator:
            with (yield from pool.hold(sim)):
                yield sim.timeout(1_000)

        def instant(sim: Simulation) -> ProcessGenerator:
            outcomes.append((yield from pool.hold_until(sim, 0)))

        sim.start(hog(sim))
        sim.start(instant(sim))
        sim.run()
        assert outcomes == [None]

    def test_releasing_a_slot_nobody_held_is_a_bug_not_a_silent_no_op(self):
        pool = Pool(1)
        with pytest.raises(RuntimeError, match="never held"):
            pool.release()


class TestFirstOf:
    def test_yields_the_index_of_whichever_fires_first(self):
        sim = Simulation()
        won: list[int] = []

        def racer(sim: Simulation) -> ProcessGenerator:
            index = yield from first_of(sim, [sim.timeout(500), sim.timeout(100)])
            won.append(index)

        sim.start(racer(sim))
        sim.run()
        assert won == [1]

    def test_a_later_event_cannot_change_the_answer(self):
        sim = Simulation()
        won: list[int] = []

        def racer(sim: Simulation) -> ProcessGenerator:
            index = yield from first_of(sim, [sim.timeout(10), sim.timeout(20)])
            won.append(index)

        sim.start(racer(sim))
        sim.run()
        assert won == [0]
