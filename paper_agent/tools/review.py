"""Independent reviews exposed to idea and experiment agents."""

from pathlib import Path

from .core import ToolContext, ToolResult, tool


IDEA_REVIEW_INSTRUCTIONS = """Evaluate the candidate ideas before a final idea is selected.
For each candidate, assess research value, novelty relative to the supplied literature,
method soundness, dataset compatibility, feasibility, and falsifiability. Cite evidence
for concerns and distinguish confirmed problems from missing evidence. Compare the
candidates, recommend priorities with reasons, and identify necessary revisions.
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


@tool()
def review_code(ctx: ToolContext, files: list[str], instructions: str = "") -> ToolResult:
    """Independently review experiment code without editing it or running training.
    Supply files naming the relevant implementation, loaders, evaluation, tests and
    configs (individual workspace-relative files, not directories). Proposal, experiment
    plan and manifest are included automatically, plus the data report if available.
    Save only refine-logs/CODE_REVIEW.md and return its full text to this conversation.
    Use the findings to repair code, then run the experiments; review again after substantive
    changes. A completed call means execution succeeded, not that the code passed.
    Optional instructions add specific review questions. Failed calls leave any old
    report unchanged; do not use that old report as evidence of a successful new review.
    """
    from paper_agent.agent.review_runner import ReviewRequest, ReviewRunner

    if not files:
        return ToolResult("Code review requires implementation files in files.", True)
    output = ctx.resolve("refine-logs/CODE_REVIEW.md", write=True)
    paths = ["refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
             "experiments/manifest.json", *files]
    if ctx.resolve("data-search/DATA_REPORT.md").is_file():
        paths.append("data-search/DATA_REPORT.md")
    inputs = []
    for path in paths:
        target = ctx.resolve(path)
        if target == output:
            return ToolResult("The code review output cannot also be an input.", True)
        if not target.is_file() or not target.read_bytes().strip():
            return ToolResult(f"Code review requires a nonempty input file: {path}", True)
        relative = target.relative_to(ctx.workspace.resolve())
        if relative not in inputs:
            inputs.append(relative)

    result = ReviewRunner(ctx.workspace).run(ReviewRequest(
        kind="code",
        instructions=CODE_REVIEW_INSTRUCTIONS + "\n" + instructions,
        inputs=tuple(inputs),
        output=Path("refine-logs/CODE_REVIEW.md"),
    ))
    metadata = {"status": result.status, "thread_id": result.thread_id}
    if result.status != "completed":
        return ToolResult(
            f"Code review did not complete ({result.status}): {result.error}. "
            "No new CODE_REVIEW.md was published; any existing report is from an earlier run.",
            True, metadata,
        )
    metadata["report_path"] = "refine-logs/CODE_REVIEW.md"
    return ToolResult(
        "Code review completed. Report: refine-logs/CODE_REVIEW.md\n"
        "Execution completion does not mean code approval. Repair and verify the findings.\n\n"
        + result.report.read_text(encoding="utf-8"),
        metadata=metadata,
    )


@tool()
def review_idea(ctx: ToolContext, instructions: str = "") -> ToolResult:
    """Review idea-stage/IDEA_CANDIDATES.md against literature/RESEARCH_REPORT.md and
    data-search/DATA_REPORT.md using an independent Codex reviewer. Write the successful
    review to idea-stage/review.md and return its full text to this conversation, so you
    can compare/revise candidates and then write idea-stage/IDEA_REPORT.md. Optional
    instructions add review questions. Does not write the
    candidates or final idea report. Execution completion does not mean idea approval.
    """
    from paper_agent.agent.review_runner import ReviewRequest, ReviewRunner

    inputs = tuple(Path(path) for path in (
        "idea-stage/IDEA_CANDIDATES.md",
        "literature/RESEARCH_REPORT.md",
        "data-search/DATA_REPORT.md",
    ))
    for path in inputs:
        target = ctx.resolve(path)
        if not target.is_file() or not target.read_text(encoding="utf-8").strip():
            return ToolResult(f"Review requires a nonempty input file: {path}", True)

    ctx.resolve("idea-stage/review.md", write=True)
    result = ReviewRunner(ctx.workspace).run(ReviewRequest(
        kind="idea",
        instructions=IDEA_REVIEW_INSTRUCTIONS + "\n" + instructions,
        inputs=inputs,
    ))
    metadata = {
        "status": result.status,
        "thread_id": result.thread_id,
    }
    if result.status != "completed":
        return ToolResult(
            f"Candidate review did not complete ({result.status}): {result.error}. "
            "No new idea-stage/review.md was published; any existing report is from an earlier run.",
            True, metadata,
        )

    report = result.report.read_text(encoding="utf-8")
    metadata["report_path"] = "idea-stage/review.md"
    return ToolResult(
        f"Candidate review completed. Report: idea-stage/review.md\n"
        "Execution completion is not scientific approval. Use the findings to decide the final idea.\n\n"
        + report,
        metadata=metadata,
    )
