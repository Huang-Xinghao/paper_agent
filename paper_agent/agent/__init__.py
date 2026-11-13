"""Research workflow agents."""

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.runtime.sessions import PhaseTask, PHASE_ROLES, ROLE_DESCRIPTIONS
from paper_agent.agent.coordinator import CoordinatorAgent, StageAssignment
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState

__all__ = ["PhaseAgent", "PhaseTask", "CoordinatorAgent", "StageAssignment", "PHASE_ROLES", "ROLE_DESCRIPTIONS", "WikiAgent", "WorkflowConfig", "WorkflowRunner", "WorkflowState"]


def __getattr__(name):
    if name == 'WikiAgent':
        from paper_agent.agent.wiki.agent import WikiAgent
        return WikiAgent
    raise AttributeError(name)
