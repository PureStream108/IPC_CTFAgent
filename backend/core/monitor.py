"""Diamond's deterministic global monitor.

Members no longer self-report difficulty.  Instead the orchestrator evaluates
every running project each scheduler tick from persisted blackboard state and
derives a project-level difficulty that drives reinforcement dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.blackboard.models import ProjectDetail
from backend.core.difficulty import (
    DIFFICULTY_RANK,
    detect_attack_surfaces,
    detect_exploit_classes,
    max_difficulty,
)

# Minutes without a new confirmed fact before a project counts as stuck.
STALL_MEDIUM_MINUTES = 10.0
STALL_HIGH_MINUTES = 25.0

# Orchestrator-side struggle counters (stalls + transient failures) that
# escalate a project even when its accumulated evidence still looks shallow.
STRUGGLE_MEDIUM = 2
STRUGGLE_HIGH = 4


@dataclass(slots=True)
class MonitorVerdict:
    difficulty: str = "low"
    evidence: list[str] = field(default_factory=list)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def assess_project(
    detail: ProjectDetail,
    *,
    struggle_count: int = 0,
    now: datetime | None = None,
) -> MonitorVerdict:
    """Grade a project's current difficulty from blackboard evidence only."""

    now = now or datetime.now(UTC)
    difficulty = "low"
    evidence: list[str] = []

    texts: list[str] = [fact.description for fact in detail.facts]
    texts.extend(hint.content for hint in detail.hints)
    for report in detail.reports:
        texts.extend([report.progress, *report.steps, *report.knowledge])
    texts.extend(attachment.filename for attachment in detail.attachments)

    classes = detect_exploit_classes(texts)
    if len(classes) >= 4:
        difficulty = max_difficulty(difficulty, "ex")
        evidence.append("distinct_exploit_classes:4+")
    elif len(classes) >= 3:
        difficulty = max_difficulty(difficulty, "high")
        evidence.append("distinct_exploit_classes:3")
    elif len(classes) >= 2:
        difficulty = max_difficulty(difficulty, "medium")
        evidence.append("distinct_exploit_classes:2")

    surfaces = detect_attack_surfaces(texts)
    if len(surfaces) >= 4:
        difficulty = max_difficulty(difficulty, "ex")
        evidence.append("credible_attack_surfaces:4+")
    elif len(surfaces) >= 3:
        difficulty = max_difficulty(difficulty, "high")
        evidence.append("credible_attack_surfaces:3")
    elif len(surfaces) >= 2:
        difficulty = max_difficulty(difficulty, "medium")
        evidence.append("credible_attack_surfaces:2")

    # Stuckness: age of the newest confirmed fact (project creation when the
    # solver has not concluded anything yet).
    concrete = [f for f in detail.facts if f.id not in ("origin", "goal")]
    stamps = [ts for ts in (_parse_ts(f.created_at) for f in concrete) if ts is not None]
    latest = max(stamps, default=None)
    anchor = latest or _parse_ts(detail.project.created_at)
    if anchor is not None:
        minutes = (now - anchor).total_seconds() / 60.0
        if minutes >= STALL_HIGH_MINUTES:
            difficulty = max_difficulty(difficulty, "high")
            evidence.append(f"no_new_fact_minutes:{int(minutes)}")
        elif minutes >= STALL_MEDIUM_MINUTES:
            difficulty = max_difficulty(difficulty, "medium")
            evidence.append(f"no_new_fact_minutes:{int(minutes)}")

    if struggle_count >= STRUGGLE_HIGH:
        difficulty = max_difficulty(difficulty, "high")
        evidence.append(f"struggle_count:{struggle_count}")
    elif struggle_count >= STRUGGLE_MEDIUM:
        difficulty = max_difficulty(difficulty, "medium")
        evidence.append(f"struggle_count:{struggle_count}")

    return MonitorVerdict(difficulty=difficulty, evidence=evidence)


def escalated(previous: str | None, current: str) -> bool:
    """True only when a known previous level is strictly exceeded.

    The first observation of a project only records the baseline; reinforcement
    fires on *changes*, not on whatever a challenge statement happens to imply.
    """

    if previous is None:
        return False
    return DIFFICULTY_RANK[current] > DIFFICULTY_RANK[previous]
