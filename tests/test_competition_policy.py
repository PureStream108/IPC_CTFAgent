from datetime import datetime, timedelta, timezone

import pytest

from backend.competition.policy import Challenge, plan_assignments
from backend.core.config import MEMBER_NAMES

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def task(index, **kwargs):
    return Challenge(str(index), kwargs.pop("category", "misc"), NOW, **kwargs)


def test_remote_quota_and_ten_global_slots_with_wp():
    tasks = [task(i, remote=True, category="web") for i in range(5)]
    tasks += [task(i) for i in range(5, 14)]
    assignments = plan_assignments(tasks, occupied_members={"opal"}, remote_capacity=2, now=NOW)
    assert len(assignments) == 9
    assert sum(a.needs_instance for a in assignments) == 2
    assert len({a.member for a in assignments}) == 9
    assert all(a.member != "opal" for a in assignments)


def test_large_platform_quota_does_not_start_unstaffed_instances():
    assignments = plan_assignments([task(i, remote=True) for i in range(20)],
                                   occupied_members=set(), remote_capacity=20, now=NOW)
    assert len(assignments) == len(MEMBER_NAMES) == 10


def test_helpers_oldest_first_and_max_two_per_challenge():
    tasks = [task(i, members=(MEMBER_NAMES[i],), state="solving",
                  first_assigned_at=NOW-timedelta(hours=i), deadline_at=NOW+timedelta(hours=1)) for i in range(4)]
    assignments = plan_assignments(tasks, occupied_members=set(), remote_capacity=0, now=NOW)
    assert [a.challenge_id for a in assignments] == ["3", "2", "1", "0"]
    assert all(a.role == "helper" for a in assignments)
    assert len(assignments) == 4


def test_new_task_precedes_helpers_and_expired_tasks_are_excluded():
    tasks = [task(1, members=("amber",), first_assigned_at=NOW-timedelta(hours=5), deadline_at=NOW),
             task(2, members=("agate",), first_assigned_at=NOW-timedelta(hours=2), deadline_at=NOW+timedelta(hours=3)),
             task(3)]
    assignments = plan_assignments(tasks, occupied_members=set(), remote_capacity=0, now=NOW)
    assert [(a.challenge_id, a.role) for a in assignments] == [("3", "primary"), ("2", "helper")]


def test_web_first_with_ready_instances_and_local_resource_limit():
    tasks = [task(1, remote=True, category="pwn"), task(2, remote=True, category="web"), task(3)]
    assignments = plan_assignments(tasks, occupied_members=set(), remote_capacity=5, now=NOW, local_capacity=1)
    assert [a.challenge_id for a in assignments] == ["2"]


def test_naive_clock_rejected():
    with pytest.raises(ValueError, match="timezone"):
        plan_assignments([], occupied_members=set(), remote_capacity=0, now=NOW.replace(tzinfo=None))


def test_twelve_challenges_respect_ten_seats_remote_quota_and_web_priority():
    tasks = [
        task("web-1", category="web", remote=True),
        task("web-2", category="web", remote=True),
        task("web-3", category="web", remote=True),
        task("pwn-remote", category="pwn", remote=True),
    ] + [task(f"local-{index}") for index in range(8)]
    assignments = plan_assignments(
        tasks,
        occupied_members=set(),
        remote_capacity=3,
        now=NOW,
        available_members=MEMBER_NAMES,
    )
    assert len(assignments) == 10
    assert len({item.member for item in assignments}) == 10
    assert sum(item.needs_instance for item in assignments) == 3
    assert {item.challenge_id for item in assignments[:3]} == {"web-1", "web-2", "web-3"}
    assert all(item.role == "primary" for item in assignments)
    assert "pwn-remote" not in {item.challenge_id for item in assignments}


def test_deadline_is_not_reset_after_clock_moves_past_original_assignment():
    assigned_at = NOW - timedelta(hours=4, minutes=59)
    challenge = task("deadline", first_assigned_at=assigned_at, deadline_at=assigned_at + timedelta(hours=5), state="solving")
    assert plan_assignments([challenge], occupied_members=set(), remote_capacity=0, now=NOW)
    assert plan_assignments(
        [challenge], occupied_members=set(), remote_capacity=0,
        now=assigned_at + timedelta(hours=5, seconds=1),
    ) == []
