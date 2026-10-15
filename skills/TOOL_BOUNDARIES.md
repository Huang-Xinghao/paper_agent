# Skill and Tool Boundaries

Skills contain research decisions: what makes a claim novel, which ablations are sufficient,
how to position a contribution, and how to interpret evidence. The shared
`shared-references/project-evidence.md` contract is loaded with every phase skill.
It describes source roles, not cached wiki or dataset content. Active skills
should not depend on ARIS slash commands or unavailable MCP tools.

The local tools contain deterministic actions:

| Phase | Fixed tools | Keep in the skill prompt |
| --- | --- | --- |
| literature | `discover_local_papers`, `read_local_paper`, `search_papers`, `download_openalex_paper`, `download_arxiv_paper`, `read_paper_pages` | source selection, relevance, synthesis, uncertainty |
| ideas | runner pre-reads wiki and online paper metadata; agent may search/read battery resources and verify papers | idea generation, ranking, feasibility and contribution judgment |
| review | artifact inspection, `external_review` | review rubric, severity, revision advice |
| proposal | runner pre-reads wiki and battery-resource dataset/input/method descriptions; validates required artifacts | method refinement, anchor/simplicity checks, claim-driven plan |
| experiment_bridge | phase-scoped `check_experiments` and `run_experiments`; existing executor/queue owns smoke, processes, GPUs, receipts and resume; coverage validator checks registered required evidence before handoff | implementation, milestone order, plan coverage, code fixes, real gate decisions, result interpretation and collection |
| paper plan/figure/write | result-file discovery and validation, artifact checks | claims-to-evidence mapping, figure selection, narrative and citations |

The workflow state records the original task, canonical node artifacts, node metadata, and a
compact tool-call trace. Full model messages remain in the phase runtime and are not copied into
the state; project files are the durable context passed between phases.

The research route is `literature → ideas → refine → experiment_plan`; novelty is not a
workflow node. Its skill remains available for independent use. Refine consumes the idea
directly and does not load historical novelty assessments.

The main route is `experiment_plan → experiment_bridge → paper_plan`. The bridge loads the
`run-experiment` subskill and invokes its tools inside the same phase. Check/run receipts remain
in state for recovery, but check/run are not public workflow nodes. Old checkpoints at those
locations resume through the bridge. Handoff uses `refine-logs/EXPERIMENT_RESULTS.md` and
`experiments/coverage.json`; a completed job list alone is not evidence of full plan coverage.
