from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.core.config import MEMBER_NAMES

CHALLENGE_DURATION = timedelta(hours=5)
WP_DURATION = timedelta(minutes=10)
SYNC_INTERVAL_SECONDS = 120

_DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}

_EXPECTED_MINUTES = {"easy": 8.0, "medium": 25.0, "hard": 60.0}
_DEFAULT_MINUTES = 30.0

_HARD_WINDOW_FRACTION = 0.35

PARK_BACKOFF_BASE_MINUTES = 5
PARK_BACKOFF_MAX_MINUTES = 40


def park_backoff_minutes(attempts: int) -> int:
    """Cooldown after the n-th park: 5, 10, 20, 40 minutes, then 40."""

    attempt = max(1, int(attempts))
    return min(PARK_BACKOFF_BASE_MINUTES * (2 ** (attempt - 1)), PARK_BACKOFF_MAX_MINUTES)


@dataclass(frozen=True)
class Challenge:
    id: str
    category: str
    ready_at: datetime
    remote: bool = False
    instance_ready: bool = False
    first_assigned_at: datetime | None = None
    attempt_started_at: datetime | None = None
    deadline_at: datetime | None = None
    members: tuple[str, ...] = ()
    state: str = "ready"
    solve_count: int | None = None
    difficulty: str | None = None
    score: float | None = None
    attempts: int = 0
    retry_at: datetime | None = None
    # 0 = never parked. A parked challenge gets a monotonically increasing rank
    # so every fresh challenge is scheduled before any retried one, and among
    # retried ones the oldest park goes first.
    parked_rank: int = 0
    parked_at: datetime | None = None


@dataclass(frozen=True)
class Assignment:
    member: str
    challenge_id: str
    role: str
    needs_instance: bool = False


def expected_minutes(challenge: Challenge) -> float:
    """How long one challenge of this band is expected to hold its slot."""

    return _EXPECTED_MINUTES.get((challenge.difficulty or "").strip().lower(), _DEFAULT_MINUTES)


def value_density(challenge: Challenge, now: datetime, *, aging_per_hour: float = 0.05) -> float:
    """Points per expected minute, with an aging bonus and an attempt penalty.

    This is the reason a 24-point-per-minute medium target outranks a
    five-hour hard one: the queue is spending a scarce remote slot, and what
    that slot returns is points divided by the time it holds.  The aging term
    keeps a low-value target from being starved forever, which is what
    happened to the last five medium challenges of the reference run.
    """

    difficulty = (challenge.difficulty or "").strip().lower()
    if challenge.score is None or challenge.score <= 0:
        return 1e-3 * (3 - _DIFFICULTY_RANK.get(difficulty, 3))
    density = float(challenge.score) / expected_minutes(challenge)
    if challenge.attempts:
        density /= 1 + int(challenge.attempts)
    waited_hours = max(0.0, (now - challenge.ready_at).total_seconds() / 3600)
    return density * (1 + aging_per_hour * waited_hours)


_SWEEP_HARD_PENALTY = 0.1


def _is_sweepable(challenge: Challenge) -> bool:
    """Sweep work: everything whose band is not explicitly hard."""

    return (challenge.difficulty or "").strip().lower() != "hard"


def _priority(challenge: Challenge, now: datetime, phase: str) -> float:
    density = value_density(challenge, now)
    if phase != "assault" and not _is_sweepable(challenge):
        return density * _SWEEP_HARD_PENALTY
    return density


def phase_for(now: datetime, started_at: datetime | None, ends_at: datetime | None) -> str:
    """sweep until the tail of the window, then assault.

    Sweep spends the open slot on work that is likely to finish; assault is
    the last stretch where a long target is the only thing left worth trying.
    """

    if started_at is None or ends_at is None or ends_at <= started_at:
        return "sweep"
    span = (ends_at - started_at).total_seconds()
    remaining = (ends_at - now).total_seconds()
    threshold = round(span * _HARD_WINDOW_FRACTION)
    return "assault" if remaining <= threshold else "sweep"


def park_candidates(
    challenges: list[Challenge], *, now: datetime, per_challenge_minutes: float,
) -> list[str]:
    """Challenges that have held a slot past their soft deadline.

    The platform deadline is five hours, which is longer than a whole run, so
    on its own it never frees a slot.  The soft deadline is what turns one
    slow target into a reclaimed slot the queue can reuse.
    """

    parked: list[str] = []
    for challenge in challenges:
        if challenge.state not in {"ready", "solving"} or not challenge.members:
            continue
        if challenge.retry_at is not None and challenge.retry_at > now:
            continue
        started = challenge.attempt_started_at or challenge.first_assigned_at
        if started is None:
            continue
        soft = started + timedelta(minutes=per_challenge_minutes)
        if challenge.deadline_at is not None and challenge.deadline_at < soft:
            soft = challenge.deadline_at
        if now >= soft:
            parked.append(challenge.id)
    return parked


def plan_assignments(
    challenges: list[Challenge], *, occupied_members: set[str],
    remote_capacity: int, now: datetime, local_capacity: int = 10,
    available_members: Iterable[str] | None = None,
    phase: str = "sweep",
    remaining_seconds: float | None = None,
) -> list[Assignment]:
    """Pure policy; the store rechecks leases and capacity when committing.

    needs_instance assignments reserve a seat, not a started five-hour timer.
    The caller must prepare the instance before committing the first solve.
    WP owners must appear in occupied_members even though they are not solvers.
    """
    if now.tzinfo is None:
        raise ValueError("scheduler timestamps must be timezone aware")
    if remote_capacity < 0 or local_capacity < 0:
        raise ValueError("capacity must not be negative")
    occupied = set(occupied_members)
    for challenge in challenges:
        occupied.update(challenge.members)
        if len(challenge.members) > 2:
            raise ValueError("a challenge cannot have more than two solvers")
    if available_members is None:
        member_order = list(MEMBER_NAMES)
    else:
        allowed = set(available_members)
        unknown = allowed.difference(MEMBER_NAMES)
        if unknown:
            raise ValueError(f"unknown Member identities: {sorted(unknown)}")
        member_order = [name for name in MEMBER_NAMES if name in allowed]
    free = [name for name in member_order if name not in occupied]
    ready_now = [
        c for c in challenges
        if c.state in {"ready", "solving"}
        and (c.deadline_at is None or c.deadline_at > now)
    ]
    eligible = [c for c in ready_now if c.retry_at is None or c.retry_at <= now]
    # Tail opening: when only parked work is left, a challenge still inside its
    # cooldown may use a window that would otherwise end while it waits. This
    # is deliberately gated on "no fresh challenge at all" so a retried target
    # never competes with an untried one.
    if remaining_seconds is not None and not any(c.parked_rank == 0 for c in eligible):
        late = [
            c for c in ready_now
            if c.retry_at is not None and c.retry_at > now and c.parked_rank > 0
            and remaining_seconds > expected_minutes(c) * 1.2 * 60.0
        ]
        if late:
            eligible.extend(late)
    running_local = sum(bool(c.members) and not c.remote for c in challenges)
    running_remote = sum(bool(c.members) and c.remote for c in challenges)
    available_local = max(0, local_capacity - running_local)
    available_remote = max(0, remote_capacity - running_remote)
    candidates = [c for c in eligible if not c.members]
    if phase == "assault" and remaining_seconds is not None:
        fitting = [
            c for c in candidates
            if remaining_seconds >= expected_minutes(c) * 1.5
        ]
        if fitting:
            candidates = fitting
    candidates.sort(key=lambda c: (
        c.parked_rank,
        -_priority(c, now, phase),
        c.attempts,
        -(c.solve_count if c.solve_count is not None else -1),
        0 if c.remote else 1,
        c.parked_at or c.ready_at,
        c.id,
    ))
    result: list[Assignment] = []
    assigned: set[str] = set()
    for challenge in candidates:
        if not free:
            break
        needs_instance = challenge.remote and not challenge.instance_ready
        if challenge.remote:
            if available_remote <= 0:
                continue
        elif available_local <= 0:
            break
        result.append(Assignment(free.pop(0), challenge.id, "primary", needs_instance))
        assigned.add(challenge.id)
        if challenge.remote:
            available_remote -= 1
        else:
            available_local -= 1
    startable_left = any(
        c.id not in assigned
        and not c.members
        and ((c.remote and available_remote > 0) or (not c.remote and available_local > 0))
        for c in candidates
    )
    helpers = [c for c in eligible if len(c.members) == 1 and c.id not in assigned]
    helpers.sort(key=lambda c: (c.first_assigned_at or c.ready_at, c.id))
    pentest_helpers = [c for c in helpers if c.category == "pentest"]
    other_helpers = [c for c in helpers if c.category != "pentest"]
    helped: set[str] = set()
    for challenge in pentest_helpers:
        if not free:
            break
        result.append(Assignment(free.pop(0), challenge.id, "helper"))
        helped.add(challenge.id)
    if len(eligible) < 10 or not startable_left:
        for challenge in other_helpers:
            if not free:
                break
            result.append(Assignment(free.pop(0), challenge.id, "helper"))
            helped.add(challenge.id)
    return result
