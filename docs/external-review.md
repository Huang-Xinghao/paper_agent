# Independent reviews

## Bridge Code Review

The `experiment_bridge` phase exposes `review_code(files=[...], instructions="...")`.
Pass individual implementation, loader, evaluation, test and configuration files;
directories are not accepted. The tool automatically includes
`refine-logs/FINAL_PROPOSAL.md`, `refine-logs/EXPERIMENT_PLAN.md`,
`experiments/manifest.json`, and `data-search/DATA_REPORT.md` when available.
The first three context files and all explicit inputs must exist and be nonempty.

The independent reviewer stays read-only and does not launch experiments. Its only
artifact is `refine-logs/CODE_REVIEW.md`, replaced on success. The full report returns
to the bridge's current conversation for repairs and subsequent verification.
Failure returns a tool error and preserves any earlier report. Review instructions
are in `CODE_REVIEW_INSTRUCTIONS` in `paper_agent/tools/review.py`.

The tool is available without changing bridge/run-experiment skills or adding an
execution gate. Invocation timing, repairs and substantive-change re-reviews should
be described in those skills. Runner success does not mean the code passed review;
reviewer assessments do not replace actual execution or real experimental evidence.

## Idea Review

`paper_agent.agent.review_runner.ReviewRunner` calls `codex exec` directly.
The ideas phase exposes it through the `review_idea` tool; no separate review node
is registered. The previous `paper_agent/review` package has been removed.

The creator writes `idea-stage/IDEA_CANDIDATES.md` before calling `review_idea`.
The tool reviews the candidates together with `literature/RESEARCH_REPORT.md` and
`data-search/DATA_REPORT.md`. All three files must exist and be nonempty. Candidate
review instructions live in `paper_agent/tools/review.py`; the tool's optional
`instructions` argument adds task-specific questions.

The only review artifact is `idea-stage/review.md`. Each successful invocation
replaces this file and returns the full report as a tool result to the creator's
existing conversation. The creator can then decide the final idea and write
`idea-stage/IDEA_REPORT.md`. The reviewer receives input files and instructions,
not the creator's message history. This tool integration does not change the
idea-creator skill or enforce candidate/review artifacts at node completion.

Prompts, commands, events, stderr, and execution metadata are handled in memory;
there are no runner log files, historical report copies, or review run directories.
Status, thread ID, usage, elapsed time, and errors are returned to the Python caller.
There is no persisted reviewer-session continuation. Native Codex may maintain its
own application state outside the project's review artifacts.

Execution is read-only with approval policy `never`. A review is `completed` only
if the process exits successfully, emits `turn.completed`, supplies a nonempty
report, and the listed inputs remain unchanged. Completion means execution succeeded,
not scientific approval. Failed or timed-out runs do not publish partial reports or
replace a previous report. Timeouts terminate the agent process group and its children.
Input hashes are checked in memory and cover only the listed files.

```python
from pathlib import Path
from paper_agent.agent.review_runner import ReviewRequest, ReviewRunner

result = ReviewRunner(Path('/path/to/project')).run(ReviewRequest(
    kind='idea',
    instructions='Compare the candidates and identify evidence gaps.',
    inputs=(Path('idea-stage/IDEA_CANDIDATES.md'),
            Path('literature/RESEARCH_REPORT.md'),
            Path('data-search/DATA_REPORT.md')),
))
if result.status == 'completed':
    print(result.report.read_text())
```

The runner also supports `code` and `paper` reviews, with default outputs
`experiments/review.md` and `paper/review.md`. `ReviewRequest.output` can override
the output path inside the workspace; it must be distinct from the input files.

The standalone command remains available after installing the project:

```bash
paper-review idea idea-stage/IDEA_CANDIDATES.md literature/RESEARCH_REPORT.md data-search/DATA_REPORT.md \
  --workspace /path/to/project --instructions-file /path/to/rubric.md
```

Without installing the entry point, use `python -m paper_agent.agent.review_cli`.
`--output` sets an alternative Markdown path. CLI execution metadata is printed to
stdout, never saved to a JSON file. Install/login to Codex, or set `CODEX_BIN` to
its executable. Model and effort default to `PAPER_AGENT_REVIEW_MODEL` and
`PAPER_AGENT_REVIEW_REASONING_EFFORT` (otherwise `gpt-6-astra` and `xhigh`). The
default timeout is 600 seconds. No live model calls are needed to test the runner.

### Paper improvement workflow

The workflow uses a full first review (target 15–20 minutes, hard limit 1800
seconds), followed by an acceptance review of the first review's findings and
revision log (target 5–10 minutes, hard limit 1200 seconds). Targets are guidance,
not completion guarantees. The model and reasoning effort remain unchanged.
`--paper-review-timeout` configures round one and
`--paper-review-round2-timeout` configures round two. The web run API accepts the
corresponding `paper_review_timeout` and `paper_review_round2_timeout` fields.

Inputs follow literal LaTeX dependencies from `paper/main.tex`: included sections,
tables, bibliography and referenced figures, plus the current PDF, paper plan and
experiment-results summary. Archives and unreferenced figure variants are
excluded. Missing or dynamically constructed dependencies fail explicitly instead
of silently presenting an incomplete file list; expand such references to literal
paths before running. Results files are checked only to investigate specific
contradictions. Reviews must not launch training or experiments.

Workflow reviews opt into `ReviewRequest.save_diagnostics`. Each attempt stores
its request/input hashes, live JSON event stream, stderr and final execution status
under `paper/.review-diagnostics/review_roundN/<attempt>/`. Incomplete agent
messages are saved separately as `partial.md`; they never replace an accepted
review report. Diagnostic event logs are local execution records, not manuscript
inputs.

`workflow-state.json` persists `improvement_checkpoint` with the round and stage
(`review`, `revision`, `compile`, or `completed`). Resume uses this checkpoint to
preserve the original and completed-round snapshots, reuse a completed report,
and skip a completed revision when only compilation failed. An interrupted review
restarts that review with a fresh session; partial reports are diagnostic only.
Failures from older versions retain a compatibility fallback. The project API's
`progress.improvement` includes round, stage, review time budget and elapsed time.
