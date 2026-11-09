"""Prevent shared requirements from drifting between independent Wiki requests."""

import ast
from pathlib import Path

import pytest

from paper_agent.agent import wiki_prompts as prompts


@pytest.mark.parametrize("stage", ["GENERATION_PROMPT", "REVIEW_PROMPT", "REVISION_PROMPT"])
def test_shared_rules_appear_once_in_each_model_request(stage):
    request = getattr(prompts, stage)
    for rule in (prompts.ABSTRACT_PROMPT, prompts.NARRATIVE_EVIDENCE_PROMPT,
                 prompts.MECHANISM_PROMPT, prompts.IDEA_DIAGRAM_PROMPT,
                 prompts.EXPERIMENT_SELECTION_PROMPT):
        assert request.count(rule) == 1
    # Bibliography extraction belongs to generation; repairs cannot replace it.
    assert (prompts.EXTRACTION_PROMPT in request) == (stage == "GENERATION_PROMPT")


def test_workflow_modules_do_not_reintroduce_inline_model_instructions():
    directory = Path(prompts.__file__).parent
    checked = 0
    for name in ("wiki", "wiki_quality", "wiki_relations"):
        for node in ast.walk(ast.parse((directory / f"{name}.py").read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            call_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if call_name in {"_analyze", "analyze", "request"}:
                # Direct names keep all model instructions in the central module.
                assert isinstance(node.args[0], ast.Name), (name, node.lineno)
                checked += 1
    assert checked >= 6
