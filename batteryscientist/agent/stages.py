"""Shared role descriptions and default research stage navigation.

Stage definitions choose roles, skills and order. Deliveries and review files
belong to the current assignment, not to a fixed stage file contract.
"""

from dataclasses import dataclass


ROLE_DESCRIPTIONS = {
    "main": "Coordinate the research goal, decisions, assignments and evidence-based acceptance. Respect user checkpoints and execution budgets.",
    "idea_generator": "Develop evidence-backed research questions, literature and data understanding, ideas, novelty assessments and the final proposal.",
    "experiment": "Plan, implement and execute approved experiments; verify real execution receipts and explain results without inventing evidence.",
    "paper_writer": "Plan the paper, create figures, write, compile and revise using verified project evidence; distinguish claims from untested hypotheses.",
    "wiki_agent": "Read papers and project evidence, author grounded Wiki drafts, obtain independent review and publish verified Wiki records.",
}


@dataclass(frozen=True)
class StageSpec:
    node: str
    phase: str
    skill: str | None
    role: str


STAGES = (
    StageSpec('literature', 'literature', 'research-lit', 'idea_generator'),
    StageSpec('data_search', 'data_search', 'data-search', 'idea_generator'),
    StageSpec('ideas', 'ideas', 'idea-creator', 'idea_generator'),
    StageSpec('refine', 'research_refine', 'research-refine', 'idea_generator'),
    StageSpec('experiment_plan', 'experiment_plan', 'experiment-plan', 'experiment'),
    StageSpec('experiment_bridge', 'experiment_bridge', 'experiment-bridge', 'experiment'),
    StageSpec('paper_plan', 'paper_plan', 'paper-plan', 'paper_writer'),
    StageSpec('paper_figure', 'paper_figure', 'paper-figure', 'paper_writer'),
    StageSpec('paper_write', 'paper_write', 'paper-write', 'paper_writer'),
    StageSpec('paper_compile', 'paper_compile', None, 'paper_writer'),
    StageSpec('improvement', 'improvement', 'auto-paper-improvement-loop', 'paper_writer'),
)

BY_NODE = {stage.node: stage for stage in STAGES}
BY_PHASE = {stage.phase: stage for stage in STAGES}
WORKFLOW_NODE_NAMES = tuple(BY_NODE)
NEXT_NODE = dict(zip(WORKFLOW_NODE_NAMES, (*WORKFLOW_NODE_NAMES[1:], None)))
PHASE_ROLES = {stage.phase: stage.role for stage in STAGES}
SKILL_BY_PHASE = {stage.phase: stage.skill for stage in STAGES}
# Wiki curation is a standalone assignment, outside the research stage sequence.
PHASE_ROLES["wiki"] = "wiki_agent"
SKILL_BY_PHASE["wiki"] = "wiki-curation"
