"""Public response contracts, independent from the evolving workflow internals."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeView(BaseModel):
    id: str
    label: str


class HealthView(BaseModel):
    status: str
    runtime: str
    project_roots: list[str]
    nodes: list[NodeView]
    concurrency: int


class CheckpointView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    node: str
    summary: str = ""
    questions: list[str] = Field(default_factory=list)
    report: str
    files: list[str] = Field(default_factory=list)
    decision_options: list[list[str]] | None = None
    report_sha256: str | None = None


class HistoryView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    node: str | None = None
    timestamp: str | None = None


class WorkflowView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = None
    current_node: str | None = None
    project_goal: str = ""
    mode: str | None = None
    history: list[HistoryView] = Field(default_factory=list)
    expert_checkpoint: CheckpointView | None = None
    error: str | None = None
    paper_status: str | None = None


class RunView(BaseModel):
    id: str
    status: str
    created_at: str
    finished_at: str | None = None
    error: str | None = None
    options: dict[str, Any]


class QuestionView(BaseModel):
    id: str
    task_id: str
    role: str
    phase: str
    question: str
    options: list[str] = Field(default_factory=list)
    status: str
    live: bool
    created_at: str


class QuestionAnswer(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=20000)


class ProjectView(BaseModel):
    id: str
    name: str
    path: str
    created_at: str
    status: str
    state: WorkflowView
    progress: dict[str, Any]
    runs: list[RunView]
    checkpoint_token: str
    pending_questions: list[QuestionView] = Field(default_factory=list)
