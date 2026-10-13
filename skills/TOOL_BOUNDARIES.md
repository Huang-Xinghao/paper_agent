# Skill and Tool Boundaries

Skills contain research decisions: what makes a claim novel, which ablations are sufficient,
how to position a contribution, and how to interpret evidence. The shared
`shared-references/project-evidence.md` contract is loaded with every phase skill.
It describes source roles, not cached wiki or dataset content. Active skills
should not depend on ARIS slash commands or unavailable MCP tools.

The local tools contain deterministic actions:

| Phase | Fixed tools | Keep in the skill prompt |
| --- | --- | --- |
| literature | `discover_local_papers`, `read_local_paper`, `search_papers`, `verify_papers` | source selection, relevance, synthesis, uncertainty |
| ideas | runner pre-reads wiki and online paper metadata; agent may search/read battery resources and verify papers | idea generation, ranking, feasibility and contribution judgment |
| novelty | paper search/verification, `external_review` | claim decomposition, competing-work comparison, novelty verdict |
| review | artifact inspection, `external_review` | review rubric, severity, revision advice |
| proposal | runner pre-reads wiki and battery-resource dataset/input/method descriptions; validates required artifacts | method refinement, anchor/simplicity checks, claim-driven plan |
| experiment | runner reads the plan, validates inputs, and inspects the generated package after the model call | implementation choices, baselines, ablations, interpretation of sanity results |
| paper plan/figure/write | result-file discovery and validation, artifact checks | claims-to-evidence mapping, figure selection, narrative and citations |

The workflow state records the original task, canonical node artifacts, node metadata, and a
compact tool-call trace. Full model messages remain in the phase runtime and are not copied into
the state; project files are the durable context passed between phases.
