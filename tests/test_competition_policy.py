from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.competition.policy import Challenge, plan_assignments
from backend.core.config import MEMBER_NAMES

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def task(index, **kwargs):
    return Challenge(str(index), kwargs.pop("category", "misc"), NOW, **kwargs)


def test_writeups_are_optional_so_a_scored_run_keeps_every_seat(monkeypatch):
    """IPC_WP_JOBS=0 stops writeup dispatch before it claims a Member seat.

    A scored benchmark gets nothing from a writeup: the platform scores flags,
    while the job spends a model call and holds one seat the scheduler would
    otherwise use as a helper.
    """

    from backend.competition.service import writeups_enabled

    monkeypatch.delenv("IPC_WP_JOBS", raising=False)
    assert writeups_enabled() is True
    for value in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("IPC_WP_JOBS", value)
        assert writeups_enabled() is False
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("IPC_WP_JOBS", value)
        assert writeups_enabled() is True


def test_park_digest_is_optional_and_defaults_on(monkeypatch):
    """The cooldown handoff runs by default but must be switchable."""

    from backend.competition.service import park_digests_enabled

    monkeypatch.delenv("IPC_PARK_DIGEST", raising=False)
    assert park_digests_enabled() is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("IPC_PARK_DIGEST", value)
        assert park_digests_enabled() is False
    for value in ("1", "true", "yes"):
        monkeypatch.setenv("IPC_PARK_DIGEST", value)
        assert park_digests_enabled() is True


def test_fallback_park_digest_lists_recent_commands():
    """Without a model the handoff still names what was already tried."""

    from backend.competition.service import _fallback_park_digest

    events = [
        {"payload": {"arguments": {"command": "curl -sS http://10.0.0.5/ | head"}}},
        {"payload": {"arguments": {"command": "ls -la /workspace/shared\ncd /tmp"}}},
        {"payload": {"content": "no command here"}},
    ]
    note = _fallback_park_digest({"external_id": "a-18"}, events, 2, 20)
    assert "a-18" in note
    assert "第 2 次" in note
    assert "20" in note
    assert "curl -sS http://10.0.0.5/" in note
    assert "ls -la /workspace/shared cd /tmp" in note
    assert note.count("/workspace/shared") >= 1


def test_fallback_park_digest_without_events_points_at_the_workspace():
    from backend.competition.service import _fallback_park_digest

    note = _fallback_park_digest({"external_id": "e3-04"}, [], 1, 10)
    assert "e3-04" in note
    assert "/workspace/shared" in note


def test_park_digest_writes_a_handoff_fact_and_metadata(monkeypatch):
    """The park hook must persist a fact the next attempt will read.

    Competition runs the digest after closing the container, so the write path
    has to work without a sandbox: a fake connection is enough to prove the
    fact and the challenge metadata are both written.
    """


    from backend.competition.service import CompetitionService

    executed = []

    class FakeCursor:
        def fetchone(self):
            return {"value": 7}

    class FakeConnection:
        def execute(self, sql, params=None):
            executed.append((sql.strip().split(None, 3)[0], " ".join(str(sql).split())[:60]))
            return FakeCursor()

    class FakeDb:
        def connect(self):
            connection = FakeConnection()

            class Ctx:
                def __enter__(self_inner):
                    return connection

                def __exit__(self_inner, *exc):
                    return False

            return Ctx()

    service = CompetitionService.__new__(CompetitionService)
    service.state = SimpleNamespace(db=FakeDb(), config=SimpleNamespace(member=None))
    service.park_digests_enabled = True
    service._digest_futures = {}
    events = [{"payload": {"arguments": {"command": "curl -sS http://10.0.0.5/"}}}]
    recorded = []
    service.store = SimpleNamespace(
        events=lambda session_id, limit=200: events,
        append_run_event=lambda run_id, kind, payload: recorded.append((kind, payload)),
    )
    service._latest_session_id = lambda run_id, challenge_id: "s1"

    service._run_park_digest({"id": "run_1"}, {"id": "ch_1", "project_id": "proj_001", "external_id": "a-18"}, 2, 20)

    statements = " ".join(sql for _, sql in executed)
    assert "INSERT INTO facts" in statements
    assert "UPDATE competition_challenges" in statements
    assert any(kind == "challenge.park_digest" for kind, _ in recorded)
    payload = next(payload for kind, payload in recorded if kind == "challenge.park_digest")
    assert payload["attempts"] == 2
    assert payload["source"] == "fallback"


def test_park_measures_the_soft_deadline_from_the_current_attempt():
    """A retried challenge must get a fresh window, not an instant re-park.

    first_assigned_at anchors the platform deadline and is immutable, so the
    scheduler used to measure every retry from the very first assignment: the
    container was opened and closed again within a tick.
    """

    from backend.competition.policy import park_candidates

    long_ago = NOW - timedelta(minutes=120)
    retry_started = NOW - timedelta(minutes=2)
    challenge = Challenge(
        "c-01", "web", long_ago,
        remote=True, members=("amber", "agate"),
        state="solving", first_assigned_at=long_ago,
        attempt_started_at=retry_started,
    )
    assert park_candidates([challenge], now=NOW, per_challenge_minutes=45) == []

    stale = Challenge(
        "c-02", "web", long_ago,
        remote=True, members=("opal",),
        state="solving", first_assigned_at=long_ago,
        attempt_started_at=NOW - timedelta(minutes=46),
    )
    assert park_candidates([stale], now=NOW, per_challenge_minutes=45) == ["c-02"]


def test_challenge_without_an_attempt_anchor_falls_back_to_the_first_assignment():
    from backend.competition.policy import park_candidates

    legacy = Challenge(
        "c-03", "misc", NOW - timedelta(minutes=200),
        remote=True, members=("jade",), state="solving",
        first_assigned_at=NOW - timedelta(minutes=60),
    )
    assert park_candidates([legacy], now=NOW, per_challenge_minutes=45) == ["c-03"]


def test_hint_is_bought_at_the_second_park_and_only_once(monkeypatch):
    """A parked challenge buys one hint, at the configured park, once."""


    from backend.competition.service import CompetitionService

    monkeypatch.setenv("IPC_HINT_AT_ATTEMPT", "2")
    written = []
    events = []

    class FakeCursor:
        def fetchone(self):
            return {"value": 3}

    class FakeConnection:
        def execute(self, sql, params=None):
            written.append(" ".join(str(sql).split())[:60])
            return FakeCursor()

    class FakeDb:
        def connect(self):
            connection = FakeConnection()

            class Ctx:
                def __enter__(self_inner):
                    return connection

                def __exit__(self_inner, *exc):
                    return False

            return Ctx()

    class FakePlatform:
        def __init__(self):
            self.calls = []
            self.answer = {"unique_code": "a-18", "hint": "试试表单解析差异"}

        def hint(self, external_id):
            self.calls.append(external_id)
            return self.answer

    service = CompetitionService.__new__(CompetitionService)
    service.state = SimpleNamespace(db=FakeDb(), config=SimpleNamespace(member=None))
    service.hint_at_attempt = 2
    service.hint_min_remaining_minutes = 0
    service._run_ends_at = lambda run: None
    service.store = SimpleNamespace(append_run_event=lambda rid, kind, payload: events.append((kind, payload)))
    platform = FakePlatform()

    row = {"id": "ch_1", "project_id": "proj_001", "external_id": "a-18", "metadata": {}}
    service._maybe_buy_hint({"id": "run_1"}, row, 1, platform)
    assert platform.calls == []

    service._maybe_buy_hint({"id": "run_1"}, row, 2, platform)
    assert platform.calls == ["a-18"]
    assert any(kind == "challenge.hint_bought" for kind, _ in events)
    assert any("INSERT INTO facts" in sql for sql in written)

    row2 = {"id": "ch_1", "project_id": "proj_001", "external_id": "a-18",
            "metadata": {"hint_bought": True}}
    service._maybe_buy_hint({"id": "run_1"}, row2, 3, platform)
    assert platform.calls == ["a-18"]


def test_hint_purchase_skips_when_the_window_is_almost_over():

    from backend.competition.service import CompetitionService

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def hint(self, external_id):
            self.calls.append(external_id)
            return {"hint": "late"}

    service = CompetitionService.__new__(CompetitionService)
    service.hint_at_attempt = 2
    service.hint_min_remaining_minutes = 45
    service._run_ends_at = lambda run: None
    service._remaining_minutes = lambda run: 10.0
    service.store = SimpleNamespace(append_run_event=lambda *a, **k: None)
    platform = FakePlatform()
    service._maybe_buy_hint({"id": "run_1"},
                            {"id": "ch_1", "project_id": None, "external_id": "a-18", "metadata": {}},
                            2, platform)
    assert platform.calls == []


def test_compaction_limit_ends_the_attempt(monkeypatch):
    """A second compaction in one attempt hands the seat back.

    f2-05 burned 204 calls because compaction rewrote the history and the same
    Member kept walking the same dead end; the scheduler never saw a seat to
    rotate because the session stayed alive.
    """


    from backend.agent.runtime import AgentRuntime
    from backend.agent.session import SessionNeedsRebuild

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.max_compactions = 1
    runtime.compactions = 1
    runtime._writer_epoch = lambda: "e1"
    append = []
    runtime._append = lambda key, kind, payload: append.append((kind, payload))
    runtime._needs_compaction = lambda messages: (True, {"tokens": 1})
    runtime._maybe_write_state = lambda detail: None
    runtime.compressor = None
    runtime.keep_recent_messages = 2
    runtime.context_bytes = 4096
    runtime.context_token_limit = 0
    runtime.calibration = 1.0
    runtime.writer = SimpleNamespace(
        record_compaction=lambda *a, **k: None,
        save_checkpoint=lambda *a, **k: None,
    )
    runtime._state_written_ratio = 0.0

    try:
        runtime._compact([{"role": "user", "content": "x"}], turn=3)
    except SessionNeedsRebuild as exc:
        assert exc.reason == "compaction_limit"
    else:
        raise AssertionError("the runtime kept compacting past its limit")
    assert any(kind == "context_compacted" for kind, _ in append)
    assert runtime.compactions == 2


def test_state_handoff_still_fires_when_only_bytes_are_configured():
    """Byte-driven compaction must still write the STATE handoff.

    context_token_limit defaults to 0, and the old gate returned early on that,
    so an operator who only set context_bytes lost every compaction handoff
    without any log line.
    """

    from backend.agent.runtime import AgentRuntime

    runtime = AgentRuntime.__new__(AgentRuntime)
    written = []
    runtime.state_writer = written.append
    runtime.context_token_limit = 0
    runtime.context_bytes = 1_000_000
    runtime.state_write_ratio = 0.85
    runtime._last_estimate = 0
    runtime._state_written_ratio = 0.0

    runtime._maybe_write_state({"reason": "byte ceiling", "bytes": 900_000, "tokens": 220_000})
    assert written, "the handoff did not fire for a byte-triggered compaction"
    assert written[0]["ratio"] >= 0.85
    assert written[0]["limit"] == 1_000_000

    runtime._state_written_ratio = 0.0
    runtime._maybe_write_state({"reason": "byte ceiling", "bytes": 500_000, "tokens": 120_000})
    assert len(written) == 1, "a below-threshold ratio must not write again"


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


def test_container_challenges_precede_local_ones_at_equal_solve_counts():
    """Ordering no longer singles out web; the container quota leads instead.

    The previous first key was "remote web challenges first", which ignored how
    many teams had already solved a challenge. Category is now irrelevant: a
    container challenge is preferred over a local one only to request the
    limited instance quota early.
    """
    tasks = [task(1, remote=True, category="pwn"), task(2, remote=True, category="web"), task(3)]
    assignments = plan_assignments(tasks, occupied_members=set(), remote_capacity=5, now=NOW, local_capacity=1)
    assert [a.challenge_id for a in assignments] == ["1", "2", "3"]


def test_most_solved_challenges_are_scheduled_first():
    """The cheapest remaining points go first (nextchange.md F5)."""
    tasks = [
        task("few", solve_count=3),
        task("many", solve_count=120),
        task("some", solve_count=40),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=0, now=NOW, local_capacity=3,
    )
    assert [a.challenge_id for a in assignments] == ["many", "some", "few"]


def test_solve_count_beats_the_container_preference():
    """A widely solved local challenge outranks an unsolved container one."""
    tasks = [
        task("local-popular", solve_count=90),
        task("container-hard", remote=True, solve_count=1),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=2, now=NOW, local_capacity=2,
    )
    assert [a.challenge_id for a in assignments] == ["local-popular", "container-hard"]


def test_an_unreported_solve_count_sorts_after_every_reported_one():
    """A missing count is unknown, not zero: it must not jump the queue.

    Sorting ``None`` as 0 would place an unknown challenge ahead of one the
    platform reports as solved three times, inverting the intended order.
    """
    tasks = [
        task("unknown"),
        task("solved-thrice", solve_count=3),
        task("solved-never", solve_count=0),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=0, now=NOW, local_capacity=3,
    )
    assert [a.challenge_id for a in assignments] == [
        "solved-thrice", "solved-never", "unknown",
    ]


def test_difficulty_precedes_solve_count_and_container_preference():
    """Easy and medium work is drained before hard targets."""
    tasks = [
        task("hard-popular", solve_count=100, difficulty="hard", remote=True),
        task("medium", solve_count=1, difficulty="medium", remote=True),
        task("easy-late", solve_count=0, difficulty="easy"),
        task("unknown", solve_count=50),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=3, now=NOW, local_capacity=4,
    )
    assert [a.challenge_id for a in assignments] == [
        "easy-late", "medium", "hard-popular", "unknown",
    ]


def test_saturated_remote_capacity_turns_idle_members_into_helpers():
    """When the platform has no free container slot, idle Members help instead."""
    solving = [
        task(f"s{index}", remote=True, state="solving",
             members=(MEMBER_NAMES[index],),
             first_assigned_at=NOW, deadline_at=NOW + timedelta(hours=1))
        for index in range(3)
    ]
    ready = [task(f"r{index}", remote=True) for index in range(10)]
    assignments = plan_assignments(
        solving + ready, occupied_members=set(), remote_capacity=3, now=NOW,
    )
    helpers = [item for item in assignments if item.role == "helper"]
    assert len(helpers) == 3
    assert {item.challenge_id for item in helpers} == {"s0", "s1", "s2"}
    assert len({item.member for item in helpers}) == 3


def test_pentest_second_seat_is_filled_before_ordinary_helpers():
    """A pentest board expects two solvers; give it a Member before plain helpers."""
    solving = [
        task(f"n{index}", remote=True, state="solving",
             members=(MEMBER_NAMES[index],),
             first_assigned_at=NOW, deadline_at=NOW + timedelta(hours=1))
        for index in range(7)
    ]
    solving.append(
        task("pentest", category="pentest", remote=True, state="solving",
             members=(MEMBER_NAMES[7],),
             first_assigned_at=NOW + timedelta(minutes=5), deadline_at=NOW + timedelta(hours=1))
    )
    ready = [task(f"r{index}", remote=True) for index in range(10)]
    assignments = plan_assignments(
        solving + ready, occupied_members=set(), remote_capacity=3, now=NOW,
    )
    helpers = [item for item in assignments if item.role == "helper"]
    assert len(helpers) == 2
    assert helpers[0].challenge_id == "pentest"
    assert len({item.challenge_id for item in helpers}) == 2


def test_container_challenges_are_serialised_inside_the_instance_quota():
    """With a quota of one, only one container challenge starts per pass."""
    tasks = [
        task("c1", remote=True, solve_count=50),
        task("c2", remote=True, solve_count=49),
        task("c3", remote=True, solve_count=48),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=1, now=NOW, local_capacity=10,
    )
    assert [a.challenge_id for a in assignments] == ["c1"]
    assert sum(a.needs_instance for a in assignments) == 1

    ready = [
        task("c1", remote=True, instance_ready=True, solve_count=50),
        task("c2", remote=True, solve_count=49),
    ]
    both = plan_assignments(
        ready, occupied_members=set(), remote_capacity=1, now=NOW, local_capacity=10,
    )
    assert [a.challenge_id for a in both] == ["c1"]
    assert [a.needs_instance for a in both] == [False]


def test_naive_clock_rejected():
    with pytest.raises(ValueError, match="timezone"):
        plan_assignments([], occupied_members=set(), remote_capacity=0, now=NOW.replace(tzinfo=None))


def test_twelve_challenges_respect_ten_seats_and_the_remote_quota():
    """Ten seats and a quota of three containers, ordered by solve count.

    This replaces the old "web first" expectation: the first three seats go to
    the three most-solved container challenges regardless of category, and the
    fourth container challenge is skipped because the quota is exhausted.
    """
    tasks = [
        task("web-1", category="web", remote=True, solve_count=80),
        task("web-2", category="web", remote=True, solve_count=70),
        task("pwn-remote", category="pwn", remote=True, solve_count=75),
        task("web-3", category="web", remote=True, solve_count=10),
    ] + [task(f"local-{index}", solve_count=5) for index in range(8)]
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
    assert [item.challenge_id for item in assignments[:3]] == [
        "web-1", "pwn-remote", "web-2",
    ]
    assert all(item.role == "primary" for item in assignments)
    assert "web-3" not in {item.challenge_id for item in assignments}


def test_deadline_is_not_reset_after_clock_moves_past_original_assignment():
    assigned_at = NOW - timedelta(hours=4, minutes=59)
    challenge = task("deadline", first_assigned_at=assigned_at, deadline_at=assigned_at + timedelta(hours=5), state="solving")
    assert plan_assignments([challenge], occupied_members=set(), remote_capacity=0, now=NOW)
    assert plan_assignments(
        [challenge], occupied_members=set(), remote_capacity=0,
        now=assigned_at + timedelta(hours=5, seconds=1),
    ) == []



def test_value_density_beats_arrival_order():
    """A 600-point medium outranks a 400-point one regardless of arrival."""
    tasks = [
        task("cheap", difficulty="medium", score=400.0, remote=True),
        task("rich", difficulty="medium", score=600.0, remote=True),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=2, now=NOW,
    )
    assert [a.challenge_id for a in assignments] == ["rich", "cheap"]


def test_hard_target_waits_while_sweep_work_is_queued():
    """A 900-point hard target must not take the slot a medium would use."""
    tasks = [
        task("hard", difficulty="hard", score=900.0, remote=True),
        task("medium", difficulty="medium", score=400.0, remote=True),
    ]
    sweep = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=1, now=NOW, phase="sweep",
    )
    assert [a.challenge_id for a in sweep] == ["medium"]


def test_hard_target_opens_in_the_assault_window():
    """Without the sweep penalty a 900-point hard target outranks a 300 one."""
    tasks = [
        task("hard", difficulty="hard", score=900.0, remote=True),
        task("medium", difficulty="medium", score=300.0, remote=True),
    ]
    assault = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=1, now=NOW,
        phase="assault", remaining_seconds=3 * 3600,
    )
    assert [a.challenge_id for a in assault] == ["hard"]


def test_a_challenge_that_was_parked_is_not_immediately_reassigned():
    """The backoff is what stops a slow target from re-taking its own slot."""
    tasks = [
        task("parked", difficulty="medium", score=600.0, remote=True,
             retry_at=NOW + timedelta(minutes=10)),
        task("next", difficulty="medium", score=300.0, remote=True),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=2, now=NOW,
    )
    assert [a.challenge_id for a in assignments] == ["next"]


def test_repeated_attempts_lose_priority():
    """A target that already burned a slot returns less than a fresh one."""
    tasks = [
        task("burned", difficulty="medium", score=600.0, remote=True, attempts=2),
        task("fresh", difficulty="medium", score=400.0, remote=True),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=1, now=NOW,
    )
    assert [a.challenge_id for a in assignments] == ["fresh"]


def test_boundaries():
    from backend.competition.policy import phase_for

    start = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)
    ends = start + timedelta(hours=6)
    assert phase_for(start + timedelta(hours=1), start, ends) == "sweep"
    assert phase_for(ends - timedelta(hours=2, minutes=7), start, ends) == "sweep"
    assert phase_for(ends - timedelta(hours=2, minutes=6), start, ends) == "assault"
    assert phase_for(ends - timedelta(hours=2, minutes=5), start, ends) == "assault"
    assert phase_for(start, None, None) == "sweep"


def test_park_candidates_only_counts_work_that_holds_a_slot():
    from backend.competition.policy import park_candidates

    now = NOW + timedelta(minutes=60)
    tasks = [
        task("over", members=("amber",), state="solving",
             first_assigned_at=NOW - timedelta(minutes=10)),
        task("fresh", members=("agate",), state="solving",
             first_assigned_at=now - timedelta(minutes=5)),
        task("idle"),
    ]
    parked = park_candidates(tasks, now=now, per_challenge_minutes=45)
    assert parked == ["over"]


def test_park_never_outlives_the_platform_deadline():
    from backend.competition.policy import park_candidates

    now = NOW + timedelta(minutes=5)
    tasks = [
        task("expiring", members=("amber",), state="solving",
             first_assigned_at=NOW - timedelta(hours=5),
             deadline_at=NOW),
    ]
    assert park_candidates(tasks, now=now, per_challenge_minutes=45) == ["expiring"]



def test_a_parked_challenge_is_not_parked_again_inside_its_backoff():
    """Parking every tick would inflate attempts and never let it retry."""
    from backend.competition.policy import park_candidates

    now = NOW + timedelta(hours=1)
    tasks = [
        task("parked", members=("amber",), state="ready",
             first_assigned_at=NOW - timedelta(hours=2),
             retry_at=now + timedelta(minutes=5)),
    ]
    assert park_candidates(tasks, now=now, per_challenge_minutes=45) == []


def test_fresh_work_is_never_scheduled_behind_parked_work():
    """A retried target waits until every untried target has had a seat."""
    tasks = [
        task("parked-valuable", difficulty="easy", score=900.0, parked_rank=1, attempts=1),
        task("fresh-hard", difficulty="hard", score=50.0),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=0, now=NOW, local_capacity=1,
    )
    assert [a.challenge_id for a in assignments] == ["fresh-hard"]


def test_parked_work_is_served_in_park_order():
    tasks = [
        task("parked-second", parked_rank=2),
        task("parked-third", parked_rank=3),
        task("parked-first", parked_rank=1),
    ]
    assignments = plan_assignments(
        tasks, occupied_members=set(), remote_capacity=0, now=NOW, local_capacity=3,
    )
    assert [a.challenge_id for a in assignments] == [
        "parked-first", "parked-second", "parked-third",
    ]


def test_park_backoff_starts_at_five_minutes_and_caps_at_forty():
    from backend.competition.policy import park_backoff_minutes

    assert [park_backoff_minutes(n) for n in range(1, 6)] == [5, 10, 20, 40, 40]
    assert park_backoff_minutes(0) == 5
    assert park_backoff_minutes(12) == 40


def test_the_tail_opens_a_parked_challenge_inside_its_backoff():
    """When only parked work is left and the window still fits it, wait no more."""
    from backend.competition.policy import park_backoff_minutes

    parked = task(
        "parked", difficulty="easy", score=10.0, parked_rank=1,
        retry_at=NOW + timedelta(minutes=park_backoff_minutes(1)),
    )
    opened = plan_assignments(
        [parked], occupied_members=set(), remote_capacity=0, now=NOW,
        local_capacity=1, remaining_seconds=3600,
    )
    assert [a.challenge_id for a in opened] == ["parked"]
    still_waiting = plan_assignments(
        [parked], occupied_members=set(), remote_capacity=0, now=NOW,
        local_capacity=1, remaining_seconds=60,
    )
    assert still_waiting == []


def test_the_tail_never_opens_while_a_fresh_challenge_exists():
    parked = task(
        "parked", difficulty="easy", score=900.0, parked_rank=1,
        retry_at=NOW + timedelta(minutes=5),
    )
    fresh = task("fresh", difficulty="hard", score=1.0)
    assignments = plan_assignments(
        [parked, fresh], occupied_members=set(), remote_capacity=0, now=NOW,
        local_capacity=1, remaining_seconds=3600,
    )
    assert [a.challenge_id for a in assignments] == ["fresh"]


def test_every_fresh_challenge_starts_before_a_parked_one_returns():
    """104-question acceptance run: parking must not jump the fresh queue."""
    from dataclasses import replace

    challenges = [
        task(f"c{index:03d}", remote=True, difficulty="easy", score=100.0 - index * 0.01)
        for index in range(104)
    ]
    by_id = {challenge.id: challenge for challenge in challenges}
    parked_id = "c000"
    fresh = {challenge.id for challenge in challenges} - {parked_id}
    fresh_started: set[str] = set()
    parked_returned = False
    for round_index in range(60):
        plans = plan_assignments(
            list(by_id.values()),
            occupied_members=set(),
            remote_capacity=3,
            now=NOW,
            local_capacity=0,
            remaining_seconds=None,
        )
        if not plans:
            break
        for plan in plans:
            challenge = by_id[plan.challenge_id]
            if challenge.parked_rank:
                parked_returned = True
                continue
            if plan.challenge_id != parked_id:
                fresh_started.add(plan.challenge_id)
            # A solved challenge releases its slot without re-entering the queue.
            by_id[plan.challenge_id] = replace(
                challenge, members=(), state="solved",
                first_assigned_at=NOW, attempt_started_at=NOW,
            )
        if round_index == 0:
            parked = by_id[parked_id]
            by_id[parked_id] = replace(
                parked, members=(), state="ready", attempts=1, parked_rank=1,
                parked_at=NOW, retry_at=NOW + timedelta(minutes=5),
            )

    assert fresh_started == fresh
    assert parked_returned is False
    # Only when the queue is exhausted does the cooldown open for the parked one.
    tail = plan_assignments(
        list(by_id.values()), occupied_members=set(), remote_capacity=3, now=NOW,
        local_capacity=0, remaining_seconds=3600,
    )
    assert [item.challenge_id for item in tail] == [parked_id]
