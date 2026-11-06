"""Replay the shape of the 2026-10-25 run against the queue policy.

The run had forty Cybench tasks, three remote slots and six hours.  Three
targets held every slot for 246, 184 and 300 minutes (the last one never
finished), the queue went 150 minutes without starting anything, and sixteen
tasks never got a container at all.  This is that board without Docker or the
platform: only the queue policy is under test, and the question is how much of
the set it manages to start in the same window.

The solve times are the reference run's, rounded.  They are synthetic on
purpose: this test must never depend on a live platform, and it is the ordering
and the slot recycling that are being pinned, not the exact minute a task ends.
Because the model has no build, no VPN flap and no slow model turn, it starts
more tasks than the real run did under either policy -- which is why the
assertion is the delta plus the plan's threshold, never an exact replay.
"""

from datetime import datetime, timedelta, timezone

from backend.competition.policy import (
    Challenge,
    _priority,
    expected_minutes,
    phase_for,
    value_density,
)

START = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)
HORIZON = 360
SLOTS = 3
SOFT = 45

_LEGACY_RANK = {"easy": 0, "medium": 1, "hard": 2}


def board():
    """Forty tasks with the reference run's band mix and solve times."""

    tasks = []
    for index, solve in enumerate([246, 184, 10_000], start=1):
        tasks.append({"id": f"long-{index}", "band": "medium", "score": 500.0, "solve": solve})
    for index, solve in enumerate([11, 15, 24, 28, 39, 58, 30, 30, 30], start=1):
        tasks.append({"id": f"mid-{index}", "band": "medium", "score": 400.0, "solve": solve})
    for index in range(1, 21):
        tasks.append({"id": f"easy-{index}", "band": "easy", "score": 250.0, "solve": 6})
    for index in range(1, 9):
        tasks.append({"id": f"hard-{index}", "band": "hard", "score": 800.0, "solve": 10_000})
    return tasks


def _challenge(task, now, attempts=0):
    return Challenge(
        task["id"], "misc", START, remote=True,
        state="ready", difficulty=task["band"], score=task["score"], attempts=attempts,
    )


def replay(*, park: bool) -> int:
    """Run the queue for six hours and return how many tasks ever started."""

    tasks = {task["id"]: task for task in board()}
    ready = {task_id: 0 for task_id in tasks}
    attempts = {task_id: 0 for task_id in tasks}
    running: dict[str, tuple[int, int]] = {}
    started: set[str] = set()

    for minute in range(HORIZON):
        now = START + timedelta(minutes=minute)
        ends = START + timedelta(minutes=HORIZON)

        for task_id, (_started, solve_at) in list(running.items()):
            if minute >= solve_at:
                del running[task_id]

        if park:
            for task_id, (started_at, solve_at) in list(running.items()):
                if solve_at > minute and minute - started_at >= SOFT:
                    del running[task_id]
                    attempts[task_id] += 1
                    ready[task_id] = minute + min(10 * 2 ** (attempts[task_id] - 1), 40)

        while len(running) < SLOTS:
            phase = phase_for(now, START, ends)
            pool = [
                task for task in tasks.values()
                if task["id"] not in running
                and ready[task["id"]] <= minute
                and (task["id"] not in started or attempts[task["id"]] > 0)
            ]
            if not pool:
                break
            if park:
                sweep = [task for task in pool if phase == "assault" or task["band"] != "hard"]
                pool = sweep or pool
                pool.sort(
                    key=lambda task: (
                        -_priority(_challenge(task, now, attempts[task["id"]]), now, phase),
                        task["id"],
                    )
                )
            else:
                pool.sort(key=lambda task: (_LEGACY_RANK.get(task["band"], 3), task["id"]))
            task = pool[0]
            running[task["id"]] = (minute, minute + task["solve"])
            started.add(task["id"])
    return len(started)


def test_parked_slots_start_more_of_the_set():
    """Without parking the long targets hold the only three containers."""

    fifo = replay(park=False)
    parked = replay(park=True)
    assert parked >= 32, parked
    assert parked > fifo + 4, (fifo, parked)


def test_expected_minutes_prefers_cheap_points():
    cheap = Challenge("a", "misc", START, difficulty="easy", score=300.0)
    expensive = Challenge("b", "misc", START, difficulty="hard", score=900.0)
    now = START + timedelta(minutes=10)
    assert value_density(cheap, now) > value_density(expensive, now)
    assert expected_minutes(cheap) < expected_minutes(expensive)
