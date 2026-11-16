"""Compose a Wiki assignment and run it through the shared role loop."""

import json


def draft_instructions() -> str:
    """Add the Wiki draft contract to the role's Skill instructions."""
    from paper_agent.tools.wiki_prompts import GENERATION_PROMPT, PROJECT_SOURCE_PROMPT

    return (
        "\n## Wiki draft file contract\nThe following rules govern the JSON draft FILE. "
        "Continue using tools to read, write, review and publish; your final response is a delivery summary.\n"
        + GENERATION_PROMPT
        + "\n"
        + PROJECT_SOURCE_PROMPT
        + "\nProject-source rules apply only to assignments with project_evidence=true. "
        "Write the requested JSON to draft_path using write; finish only after publication, "
        "a verified unchanged-input receipt, or an explicit wiki_abort failure receipt.\n"
    )


def run_wiki_role(
    project,
    instruction,
    *,
    paper_path="paper/main.pdf",
    project_evidence=False,
    source_paths=None,
    wiki_root=None,
    force=False,
    complete_relations=True,
    model=None,
    model_client=None,
    timeout=600,
    max_iterations=300,
    on_progress=None,
    interaction_mode="auto",
):
    """Run Wiki curation through the shared PhaseAgent role loop."""
    from paper_agent.agent.loop import PhaseAgent

    agent = PhaseAgent(
        project,
        phase="wiki",
        assignment_role="wiki_agent",
        model=model,
        model_client=model_client,
        timeout=timeout,
        max_iterations=max_iterations,
        interaction_mode=interaction_mode,
        on_progress=on_progress,
    )
    agent.wiki_ingestion.assign(
        paper_path=paper_path,
        project_evidence=project_evidence,
        source_paths=source_paths,
        wiki_root=wiki_root,
        force=force,
        complete_relations=complete_relations,
    )
    request = (
        instruction
        + "\nWiki assignment inputs:\n"
        + json.dumps(
            {
                "paper_path": paper_path,
                "project_evidence": project_evidence,
                "source_paths": source_paths or [],
                "force": force,
            },
            ensure_ascii=False,
        )
    )
    try:
        state = agent.go(request)
        return {
            **agent.wiki_ingestion.receipt,
            "final": state.final,
            "receipt_path": agent.wiki_ingestion._display(
                agent.wiki_ingestion.receipt_path
            ),
        }
    except BaseException as exc:
        curator = agent.wiki_ingestion
        if hasattr(curator, "signature") and (
            not curator.receipt
            or curator.receipt.get("status") not in {"completed", "skipped"}
        ):
            curator._save_receipt(
                (
                    "interrupted"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "failed"
                ),
                error=str(exc),
            )
        raise
