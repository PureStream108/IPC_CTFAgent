from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProjectStatus = Literal[
    "created", "running", "flag_found", "wp_writing", "memory_writing", "completed", "stopped"
]


class Fact(BaseModel):
    id: str
    description: str
    created_at: str | None = None


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class Agent(BaseModel):
    name: str
    role: Literal["ipc", "diamond", "member"]
    state: Literal["idle", "active", "paused", "done"] = "idle"
    start_fact_id: str | None = None
    created_at: str | None = None


class AgentLink(BaseModel):
    id: int
    src: str
    dst: str
    kind: Literal["assign", "report", "explore", "flag", "wp", "return", "start"]
    created_at: str


class Report(BaseModel):
    id: str
    member: str
    node_id: str | None = None
    progress: str
    difficulty: str
    steps: list[str] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    created_at: str


class Attachment(BaseModel):
    id: str
    filename: str
    path: str
    created_at: str


class Broadcast(BaseModel):
    id: int
    project_id: str | None = None
    title: str
    flag: str
    created_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    category: str
    status: ProjectStatus
    flag: str | None = None
    wp_path: str | None = None
    log_filename: str | None = None
    created_at: str
    updated_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int
    member_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    agents: list[Agent]
    agent_links: list[AgentLink]
    reports: list[Report]
    attachments: list[Attachment]


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("must not be empty")
    return text


class CreateHintInline(BaseModel):
    content: str
    creator: str

    _v = field_validator("content", "creator")(lambda cls, v: _non_empty(v))


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    category: str = "misc"
    hints: list[CreateHintInline] | None = None

    _v = field_validator("title", "origin", "goal")(lambda cls, v: _non_empty(v))


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    _v = field_validator("content", "creator")(lambda cls, v: _non_empty(v))


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = ConfigDict(populate_by_name=True)

    _v = field_validator("description", "creator", "worker")(lambda cls, v: _non_empty(v))


class HeartbeatRequest(BaseModel):
    worker: str
    _v = field_validator("worker")(lambda cls, v: _non_empty(v))


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str
    _v = field_validator("worker", "trigger")(lambda cls, v: _non_empty(v))


class ConcludeRequest(BaseModel):
    worker: str
    description: str
    _v = field_validator("worker", "description")(lambda cls, v: _non_empty(v))


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    flag: str | None = None

    model_config = ConfigDict(populate_by_name=True)

    _v = field_validator("description", "worker")(lambda cls, v: _non_empty(v))


class ReportRequest(BaseModel):
    member: str
    node_id: str | None = None
    progress: str
    difficulty: str
    steps: list[str] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)

    _v = field_validator("member", "progress", "difficulty")(lambda cls, v: _non_empty(v))
