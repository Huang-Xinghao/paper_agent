import asyncio
import ast
import json
from pathlib import Path

import pytest

from paper_agent.tools import BUILTIN_TOOLS, ToolContext, invoke, tools_for_phase


def test_paper_templates_can_be_read_outside_project(tmp_path):
    skills = Path(__file__).resolve().parents[1] / "paper_agent/skills"
    ctx = ToolContext(tmp_path, (skills,))
    template = skills / "paper-write/templates/iclr2026.tex"
    result = asyncio.run(invoke("skill_resource_read", ctx, name="paper-write",
                                resource="templates/iclr2026.tex"))
    assert not result.is_error
    assert result.output == template.read_text(encoding="utf-8")
    assert asyncio.run(invoke("read", ctx, path=str(template))).is_error


@pytest.mark.parametrize("resource", ["../private.md", "/etc/passwd",
                                      "templates/../../private.md", "templates/leak.tex"])
def test_skill_resources_cannot_escape_their_directory(tmp_path, resource):
    skill = tmp_path / "skills/checker"
    (skill / "templates").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Checker")
    private = tmp_path / "private.md"
    private.write_text("private")
    (skill / "templates/leak.tex").symlink_to(private)
    ctx = ToolContext(tmp_path / "project", (tmp_path / "skills",))
    result = asyncio.run(invoke("skill_resource_read", ctx, name="checker", resource=resource))
    assert result.is_error
    assert result.metadata["error_code"] == "RESOURCE_NOT_FOUND"


def test_skill_resources_reject_symlinked_directory(tmp_path):
    skill = tmp_path / "skills/checker"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Checker")
    external = tmp_path / "external"
    external.mkdir()
    (external / "leak.tex").write_text("private")
    (skill / "templates").symlink_to(external, target_is_directory=True)
    ctx = ToolContext(tmp_path / "project", (tmp_path / "skills",))
    result = asyncio.run(invoke("skill_resource_read", ctx, name="checker", resource="templates/leak.tex"))
    assert result.is_error


def test_image_generation_is_visible_to_paper_figure_agent():
    names = {tool.name for tool in tools_for_phase("paper_figure", BUILTIN_TOOLS)}
    assert "generate_image" in names


def test_image_generation_returns_structured_success_receipt(tmp_path, monkeypatch):
    from paper_agent.mcp import image_client
    class FakeClient:
        def __init__(self, project): pass
        def generate_start(self, prompt, **kwargs):
            target = tmp_path / kwargs["output_path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
            return {"jobId": "img-test", "status": "done", "model": "gpt-image-2"}
    monkeypatch.setattr(image_client, "ImageMCPClient", FakeClient, raising=False)
    result = asyncio.run(invoke("generate_image", ToolContext(tmp_path), prompt="A method diagram"))
    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["output_path"].startswith("figures/")


def test_image_generation_rejects_output_escape(tmp_path):
    result = asyncio.run(invoke("generate_image", ToolContext(tmp_path), prompt="x", output_path="../x.png"))
    assert result.is_error and result.metadata["error_code"] == "IMAGE_OUTPUT_OUTSIDE_FIGURES"


def test_builtin_tools_and_phase_allowlist():
    names = {tool.name for tool in BUILTIN_TOOLS}
    assert {"read", "write", "edit", "grep", "find", "ls", "bash", "skill"} <= names
    assert "bash" not in {tool.name for tool in tools_for_phase("literature", BUILTIN_TOOLS)}
    assert "bash" in {tool.name for tool in tools_for_phase("experiment_bridge", BUILTIN_TOOLS)}


def test_file_tools_are_workspace_scoped(tmp_path):
    ctx = ToolContext(tmp_path)
    result = asyncio.run(invoke("write", ctx, path="notes/idea.md", content="claim\nmethod"))
    assert not result.is_error
    result = asyncio.run(invoke("read", ctx, path="notes/idea.md"))
    assert "1: claim" in result.output
    result = asyncio.run(invoke("read", ctx, path="../outside.txt"))
    assert result.is_error


def test_edit_requires_unambiguous_match(tmp_path):
    ctx = ToolContext(tmp_path)
    asyncio.run(invoke("write", ctx, path="draft.md", content="x\nx\n"))
    result = asyncio.run(invoke("edit", ctx, path="draft.md", old="x", new="y"))
    assert result.is_error and result.metadata["error_code"] == "AMBIGUOUS_REPLACEMENT"


def test_python_write_and_edit_format_without_changing_program(tmp_path):
    ctx = ToolContext(tmp_path)
    source = "def calculate(x):\n return x+1\nvalue=calculate(2);print(value)\n"
    result = asyncio.run(invoke("write", ctx, path="run.py", content=source))
    saved = (tmp_path / "run.py").read_text()
    assert not result.is_error and "Black formatted" in result.output
    assert "    return x + 1" in saved and ";" not in saved
    assert ast.dump(ast.parse(saved)) == ast.dump(ast.parse(source))

    result = asyncio.run(invoke("edit", ctx, path="run.py", old="return x + 1", new="return x*2; # revised"))
    saved = (tmp_path / "run.py").read_text()
    assert not result.is_error and "Black formatted" in result.output
    assert "    return x * 2\n" in saved and "# revised" in saved
    namespace = {}
    exec(saved, namespace)
    assert namespace["value"] == 4


def test_python_formatting_preserves_incomplete_code_and_other_files(tmp_path):
    ctx = ToolContext(tmp_path)
    incomplete = "def calculate(\n"
    result = asyncio.run(invoke("write", ctx, path="run.py", content=incomplete))
    assert not result.is_error and "Black skipped" in result.output
    assert (tmp_path / "run.py").read_text() == incomplete
    result = asyncio.run(invoke("edit", ctx, path="run.py", old=incomplete, new="value=1;print(value)"))
    assert not result.is_error and "Black formatted" in result.output
    assert (tmp_path / "run.py").read_text() == "value = 1\nprint(value)\n"

    text = "value=1;print(value)"
    result = asyncio.run(invoke("write", ctx, path="notes.md", content=text))
    assert not result.is_error
    assert (tmp_path / "notes.md").read_text() == text


def test_literature_local_discovery(tmp_path):
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / "battery_models.pdf").write_bytes(b"%PDF-not-parsed")
    ctx = ToolContext(tmp_path)
    result = asyncio.run(invoke("discover_local_papers", ctx, query="battery"))
    payload = json.loads(result.output)
    assert payload["found"] == 1
    assert payload["papers"][0]["path"] == "papers/battery_models.pdf"


def test_literature_tools_are_visible_only_in_literature_phase():
    names = {tool.name for tool in tools_for_phase("literature", BUILTIN_TOOLS)}
    assert {"discover_local_papers", "read_local_paper", "search_papers"} <= names
    assert "verify_papers" not in {tool.name for tool in BUILTIN_TOOLS}
    assert "search_papers" not in {tool.name for tool in tools_for_phase("paper_compile", BUILTIN_TOOLS)}


def test_battery_resource_tools_search_and_read(tmp_path):
    root = tmp_path / "battery"
    (root / "电芯/电池输入").mkdir(parents=True)
    (root / "电芯/电池输入/demo.md").write_text("# 充电时序\n早期电压 电流 温度", encoding="utf-8")
    ctx = ToolContext(tmp_path, knowledge_roots=(root,))
    result = asyncio.run(invoke("search_battery_resources", ctx, query="早期充电电压", resource_types=["input"]))
    payload = json.loads(result.output)
    assert payload["count"] == 1
    resource_id = payload["resources"][0]["resource_id"]
    result = asyncio.run(invoke("read_battery_resource", ctx, resource_id=resource_id))
    assert "早期电压" in json.loads(result.output)["content"]


def test_battery_resource_tools_require_explicit_root(tmp_path):
    result = asyncio.run(invoke("search_battery_resources", ToolContext(tmp_path), query="电池"))
    assert result.is_error and result.metadata["error_code"] == "KNOWLEDGE_ROOT_UNAVAILABLE"


def test_electrolyte_input_and_method_search(tmp_path):
    root = tmp_path / "battery"
    ctx = ToolContext(tmp_path, knowledge_roots=(root,))
    for directory, kind in [("电解液输入", "input"), ("电解液模型", "method")]:
        document = root / "电解液" / directory / "guide.md"
        document.parent.mkdir(parents=True)
        document.write_text("# Morgan 配方建模\n", encoding="utf-8")
        result = asyncio.run(invoke("search_battery_resources", ctx,
                                   query="Morgan", resource_types=[kind]))
        payload = json.loads(result.output)
        assert payload["count"] == 1
        assert payload["resources"][0]["type"] == kind


@pytest.mark.parametrize("phase", ["refine", "experiment"])
def test_phase_aliases_are_rejected(tmp_path, phase):
    from paper_agent.agent.loop import PhaseAgent
    with pytest.raises(ValueError, match="unknown paper workflow phase"):
        tools_for_phase(phase, BUILTIN_TOOLS)
    with pytest.raises(ValueError, match="unknown paper workflow phase"):
        PhaseAgent(tmp_path, model_client=object(), phase=phase)


def test_skill_resources_require_explicit_directory(tmp_path):
    root = tmp_path / 'skills/checker'
    (root / 'references').mkdir(parents=True)
    (root / 'SKILL.md').write_text('# Checker')
    (root / 'references/guide.md').write_text('Guide')
    ctx = ToolContext(tmp_path / 'project', (tmp_path / 'skills',))
    bare = asyncio.run(invoke('skill_resource_read', ctx, name='checker', resource='guide.md'))
    explicit = asyncio.run(invoke('skill_resource_read', ctx, name='checker', resource='references/guide.md'))
    assert bare.is_error
    assert not explicit.is_error and explicit.output == 'Guide'
