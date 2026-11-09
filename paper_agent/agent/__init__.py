"""Research workflow agents."""

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.role_sessions import PhaseTask, PHASE_ROLES, ROLE_DESCRIPTIONS
from paper_agent.agent.coordinator import CoordinatorAgent, StageAssignment
from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState

__all__ = ["PhaseAgent", "PhaseTask", "CoordinatorAgent", "StageAssignment", "PHASE_ROLES", "ROLE_DESCRIPTIONS", "WikiAgent", "WorkflowConfig", "WorkflowRunner", "WorkflowState"]
