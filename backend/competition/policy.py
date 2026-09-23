from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Iterable

from backend.core.config import MEMBER_NAMES

CHALLENGE_DURATION = timedelta(hours=5)
WP_DURATION = timedelta(minutes=10)
SYNC_INTERVAL_SECONDS = 120


@dataclass(frozen=True)
class Challenge:
    id: str
    category: str
    ready_at: datetime
    remote: bool = False
    instance_ready: bool = False
    first_assigned_at: datetime | None = None
    deadline_at: datetime | None = None
    members: tuple[str, ...] = ()
    state: str = "ready"


@dataclass(frozen=True)
class Assignment:
    member: str
    challenge_id: str
    role: str
    needs_instance: bool = False


def plan_assignments(
    challenges: list[Challenge], *, occupied_members: set[str],
    remote_capacity: int, now: datetime, local_capacity: int = 10,
    available_members: Iterable[str] | None = None,
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
    eligible = [c for c in challenges if c.state in {"ready", "solving"}
                and (c.deadline_at is None or c.deadline_at > now)]
    running = sum(bool(c.members) for c in challenges)
    available_local = max(0, local_capacity - running)
    candidates = [c for c in eligible if not c.members]
    candidates.sort(key=lambda c: (0 if c.remote and c.category == "web" else 1 if c.remote else 2,
                                   c.ready_at, c.id))
    result: list[Assignment] = []
    assigned: set[str] = set()
    for challenge in candidates:
        if not free or not available_local:
            break
        needs_instance = challenge.remote and not challenge.instance_ready
        if needs_instance and remote_capacity == 0:
            continue
        result.append(Assignment(free.pop(0), challenge.id, "primary", needs_instance))
        assigned.add(challenge.id)
        available_local -= 1
        if needs_instance:
            remote_capacity -= 1
    # Helpers only reinforce already-running challenges. A newly reserved
    # challenge first needs a primary session with useful progress to hand off.
    if len(eligible) < 10:
        helpers = [c for c in eligible if len(c.members) == 1 and c.id not in assigned]
        helpers.sort(key=lambda c: (c.first_assigned_at or c.ready_at, c.id))
        for challenge in helpers:
            if not free:
                break
            result.append(Assignment(free.pop(0), challenge.id, "helper"))
    return result
