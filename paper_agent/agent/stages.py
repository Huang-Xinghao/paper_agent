"""Stage identities, order and file contracts shared by all entry points.

Inputs gate explicit workflow entry; outputs describe stage delivery; review files
identify the version approved by the user. Specialized validators remain in the
runner. The output-check level preserves the weaker checks used by direct calls.
This module has no runtime, CLI or tool imports.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StageSpec:
    node: str | None
    phase: str
    skill: str
    role: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    review_files: tuple[str, ...] | None = None
    output_check: Literal["direct", "workflow", "specialized"] = "direct"

    @property
    def review_paths(self) -> tuple[str, ...]:
        return self.outputs if self.review_files is None else self.review_files

    def completion_outputs(self, *, direct: bool = False) -> tuple[str, ...]:
        if self.output_check == "specialized" or (direct and self.output_check == "workflow"):
            return ()
        return self.outputs


# Workflow order is the order below, excluding standalone phases (node=None).
STAGES = (
    StageSpec(
        node="literature", phase="literature", skill="research-lit", role="idea_generator",
        outputs=("literature/RESEARCH_REPORT.md",),
    ),
    StageSpec(
        node="data_search", phase="data_search", skill="data-search", role="idea_generator",
        inputs=("literature/RESEARCH_REPORT.md",),
        outputs=("data-search/DATA_REPORT.md",),
    ),
    StageSpec(
        node="ideas", phase="ideas", skill="idea-creator", role="idea_generator",
        inputs=("literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md"),
        outputs=("idea-stage/IDEA_REPORT.md",),
    ),
    StageSpec(
        node="refine", phase="research_refine", skill="research-refine", role="idea_generator",
        inputs=("idea-stage/IDEA_REPORT.md",),
        outputs=("refine-logs/FINAL_PROPOSAL.md",), output_check="workflow",
    ),
    StageSpec(
        node="experiment_plan", phase="experiment_plan", skill="experiment-plan", role="experiment",
        inputs=("refine-logs/FINAL_PROPOSAL.md",),
        outputs=("refine-logs/EXPERIMENT_PLAN.md",), output_check="workflow",
    ),
    StageSpec(
        node="experiment_bridge", phase="experiment_bridge", skill="experiment-bridge", role="experiment",
        inputs=("refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md"),
        outputs=("refine-logs/EXPERIMENT_RESULTS.md", "experiments/manifest.json"),
        output_check="specialized",
    ),
    StageSpec(
        node="paper_plan", phase="paper_plan", skill="paper-plan", role="paper_writer",
        inputs=("refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md"),
        outputs=("PAPER_PLAN.md",),
    ),
    StageSpec(
        node="paper_figure", phase="paper_figure", skill="paper-figure", role="paper_writer",
        inputs=("PAPER_PLAN.md",),
        outputs=("figures/latex_includes.tex", "figures/figure_plan.md"),
        review_files=("figures/figure_plan.md", "figures/latex_includes.tex"),
    ),
    StageSpec(
        node="paper_write", phase="paper_write", skill="paper-write", role="paper_writer",
        inputs=("PAPER_PLAN.md", "figures/latex_includes.tex"),
        outputs=("paper/main.tex", "paper/references.bib"),
    ),
    StageSpec(
        node="paper_compile", phase="paper_compile", skill="paper-compile", role="paper_writer",
        inputs=("paper/main.tex",), outputs=("paper/main.tex", "paper/main.pdf"),
        output_check="specialized",
    ),
    StageSpec(
        node="improvement", phase="improvement", skill="auto-paper-improvement-loop", role="paper_writer",
        inputs=("paper/main.tex", "paper/main.pdf"),
        outputs=("paper/PAPER_IMPROVEMENT_LOG.md", "paper/main.tex", "paper/main.pdf"),
        output_check="specialized",
    ),
)

BY_NODE = {stage.node: stage for stage in STAGES if stage.node is not None}
BY_PHASE = {stage.phase: stage for stage in STAGES}
WORKFLOW_NODE_NAMES = tuple(BY_NODE)
NEXT_NODE = dict(zip(WORKFLOW_NODE_NAMES, (*WORKFLOW_NODE_NAMES[1:], None)))
PHASE_ROLES = {stage.phase: stage.role for stage in STAGES}
SKILL_BY_PHASE = {stage.phase: stage.skill for stage in STAGES}
