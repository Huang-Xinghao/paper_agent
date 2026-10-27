import json

from paper_agent.agent.loop import PhaseAgent
from paper_agent.tools import ToolContext, invoke


def test_wiki_tools_search_and_read_are_wiki_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(tmp_path / "research-wiki"))
    page = tmp_path / "research-wiki" / "papers" / "smith2025_battery.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Battery paper\n\n## Method\nVoltage features and state estimation.\n")
    ctx = ToolContext(tmp_path)

    result = __import__("asyncio").run(invoke("search_wiki", ctx, query="voltage features"))
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["matches"][0]["path"] == "research-wiki/papers/smith2025_battery.md"

    result = __import__("asyncio").run(invoke(
        "read_wiki", ctx, path="research-wiki/papers/smith2025_battery.md", offset=1, limit=2))
    payload = json.loads(result.output)
    assert "## Method" in payload["content"]

    result = __import__("asyncio").run(invoke("read_wiki", ctx, path="../secret.md"))
    assert result.is_error


def test_paper_agent_exposes_wiki_tools_in_research_phases(tmp_path):
    class Model:
        def complete(self, messages, tools):
            return type("Response", (), {"content": "done", "tool_calls": ()})()

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="ideas")
    names = {spec.name for spec in agent.tools}
    assert {"search_wiki", "read_wiki"} <= names
    assert "research-wiki" in agent._system_prompt()


def test_default_wiki_root_is_repository_root(monkeypatch):
    from pathlib import Path
    from paper_agent.tools import wiki
    monkeypatch.delenv('PAPER_AGENT_WIKI_ROOT', raising=False)
    assert wiki.resolve_wiki_root() == Path(wiki.__file__).resolve().parents[2] / 'research-wiki'


def test_shared_wiki_read_across_projects(tmp_path, monkeypatch):
    import asyncio
    shared = tmp_path / 'shared/research-wiki'
    page = shared / 'papers/battery.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Battery SOH\nShared voltage evidence')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(shared))
    for name in ('demo8', 'demo9'):
        project = tmp_path / name
        project.mkdir()
        local = project / 'research-wiki/papers/local.md'
        local.parent.mkdir(parents=True)
        local.write_text('Battery local page must not override shared wiki')
        ctx = ToolContext(project)
        found = asyncio.run(invoke('search_wiki', ctx, query='Battery'))
        matches = json.loads(found.output)['matches']
        assert len(matches) == 1 and matches[0]['path'] == str(page)
        for path in (str(page), 'research-wiki/papers/battery.md', 'papers/battery.md'):
            read = asyncio.run(invoke('read_wiki', ctx, path=path))
            assert not read.is_error
            assert 'Shared voltage evidence' in json.loads(read.output)['content']
        assert asyncio.run(invoke('read_wiki', ctx, path=str(local))).is_error
        assert asyncio.run(invoke('read_wiki', ctx, path='../secret.md')).is_error


def test_runner_prereads_shared_wiki(tmp_path, monkeypatch):
    from paper_agent.agent.workflow import WorkflowRunner
    from paper_agent.tools import wiki
    shared = tmp_path / 'shared/research-wiki'
    (shared / 'papers').mkdir(parents=True)
    (shared / 'index.md').write_text('# Shared index')
    page = shared / 'papers/battery.md'
    page.write_text('Battery SOH shared method evidence')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(shared))
    project = tmp_path / 'demo8'
    project.mkdir()
    agent = PhaseAgent(project, model_client=object())
    context = WorkflowRunner(agent)._research_context('Battery SOH', stage='test')
    assert context['wiki']['status'] == 'read'
    assert context['wiki']['root'] == str(shared)
    assert any(record['path'] == str(page) and 'shared method evidence' in record['content']
               for record in context['wiki']['pages'])
