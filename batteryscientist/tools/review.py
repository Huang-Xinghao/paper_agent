"""Independent reviews exposed to idea and experiment agents."""

from batteryscientist.integrations.review import DEFAULT_REPORTS, ReviewRequest, ReviewRunner

from .core import ToolContext, ToolResult, tool


IDEA_REVIEW_INSTRUCTIONS = """Evaluate the candidate ideas before a final idea is selected.
For each candidate, assess research value, novelty relative to the supplied literature,
method soundness, dataset compatibility, feasibility, and falsifiability. Cite evidence
for concerns and distinguish confirmed problems from missing evidence. Compare the
candidates, recommend priorities with reasons, and identify necessary revisions.
For method-design tasks, judge whether each candidate proposes a concrete solution
to a specific bottleneck: identify the changed mechanism, the closest prior method,
and a minimal comparison that could falsify its benefit. Model rankings or a larger
benchmark matrix do not replace a proposed solution. Do not default to a benchmark
because it is cheaper or because novelty remains uncertain; request focused method
revisions instead. Respect explicit user requests for benchmarking or reproduction.
Do not select or write the final IDEA_REPORT.md; the idea creator makes the final decision.
"""


CODE_REVIEW_INSTRUCTIONS = """Review the experiment implementation against FINAL_PROPOSAL.md
and EXPERIMENT_PLAN.md before full experiments. Follow the actual call paths from
manifest entry points through data loading, splitting, preprocessing, models, training,
evaluation, and output collection. Inspect supporting workspace files as needed.
Check method fidelity, entity/domain split isolation, train-only fitting of preprocessing,
validation-only model selection, real labels, metric formulas and aggregation units,
baseline/ablation fairness, seed independence, reproducibility, and manifest/output coverage.
Report an overall assessment (PASS, NEEDS_REVISION, or BLOCKED), findings ordered by
severity with file/line evidence, required changes, verification performed, and unresolved
risks. Distinguish confirmed defects from uncertainties; missing runtime evidence is not
proof of correctness. Do not invent test results or launch training or experiment jobs.
Review only: do not modify implementation, plans, results, receipts, or resource settings.
The bridge agent owns repairs and verification through run_experiments.
A PASS assessment does not validate runtime correctness or scientific performance.
"""


def _review(ctx: ToolContext, kind: str, paths: list[str], instructions: str) -> ToolResult:
    output = DEFAULT_REPORTS[kind]
    try:
        ctx.resolve(output, write=True)
        inputs = tuple(dict.fromkeys(ctx.resolve(path).relative_to(ctx.workspace.resolve()) for path in paths))
        result = ReviewRunner(ctx.workspace, on_progress=ctx.on_progress).run(ReviewRequest(
            kind=kind, instructions=instructions, inputs=inputs))
    except (OSError, ValueError) as exc:
        return ToolResult(str(exc), True)
    metadata = {"status": result.status, "thread_id": result.thread_id}
    if result.status != "completed":
        return ToolResult(
            f"{kind.capitalize()} review did not complete ({result.status}): {result.error}. "
            f"No new {output} was published; any existing report is from an earlier run.",
            True, metadata)
    metadata["report_path"] = output.as_posix()
    return ToolResult(
        f"Review completed. Report: {output}\n"
        "Execution completion is not scientific approval. Use the findings to decide revisions.\n\n"
        + result.report.read_text(encoding="utf-8"), metadata=metadata)


@tool()
def review_code(ctx: ToolContext, files: list[str], instructions: str = "") -> ToolResult:
    """Independently review experiment code against the proposal, plan and manifest.
    Supply relevant implementation/config/test files as workspace-relative paths.
    Proposal, experiment plan and manifest are included automatically, plus the data
    report if available. Save refine-logs/CODE_REVIEW.md and return its full text.
    Does not edit code or run training. A completed call does not mean code approval.
    """
    if not files:
        return ToolResult("Code review requires implementation files in files.", True)
    paths = ["refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
             "experiments/manifest.json", *files]
    if ctx.resolve("data-search/DATA_REPORT.md").is_file():
        paths.append("data-search/DATA_REPORT.md")
    return _review(ctx, "code", paths, CODE_REVIEW_INSTRUCTIONS + "\n" + instructions)


@tool()
def review_idea(ctx: ToolContext, instructions: str = "") -> ToolResult:
    """Independently review candidates against literature and the data report.
    Save idea-stage/review.md and return its full text for the agent's decision.
    Does not write candidates or the final idea report. Completion is not approval.
    """
    return _review(ctx, "idea", ["idea-stage/IDEA_CANDIDATES.md",
        "literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md"],
        IDEA_REVIEW_INSTRUCTIONS + "\n" + instructions)
