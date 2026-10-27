import json
import urllib.error
from pathlib import Path

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.tools import ToolContext
from paper_agent.tools import literature


def test_rate_limit_retries_are_bounded(monkeypatch):
    calls, sleeps = [], []
    def request(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError('url', 429, 'limited', {'Retry-After': '60'}, None)
    monkeypatch.setattr(literature.urllib.request, 'urlopen', request)
    monkeypatch.setattr(literature.time, 'sleep', sleeps.append)
    import pytest
    with pytest.raises(urllib.error.HTTPError):
        literature._get_json('https://example.org')
    assert len(calls) == 3
    assert sleeps == [10, 10]


def test_direct_ideas_cannot_finish_without_canonical_report(tmp_path):
    class Model:
        count = 0
        def complete(self, messages, tools):
            self.count += 1
            if self.count == 1: return ModelResponse(content='done')
            if self.count == 2:
                assert 'Completion checks failed:' in messages[-1]['content']
                return ModelResponse(tool_calls=(ToolCall('w', 'write', {
                    'path': 'idea-stage/IDEA_REPORT.md', 'content': 'Evidence and hypothesis'}),))
            return ModelResponse(content='Report ready')
    result = PhaseAgent(tmp_path, model_client=Model()).go('Generate idea')
    assert result.iterations == 3


def test_skill_not_silently_truncated(tmp_path):
    agent = PhaseAgent(tmp_path, model_client=object())
    agent.skills_root = tmp_path
    folder = tmp_path / 'idea-creator'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('x' * 13000 + 'FINAL RULE')
    assert agent._skill_prompt().endswith('FINAL RULE')


def test_idea_search_can_finish_without_metadata_verification(tmp_path):
    from paper_agent.tools import ToolSpec, ToolResult
    class Model:
        count = 0
        def complete(self, messages, tools):
            self.count += 1
            if self.count == 1:
                return ModelResponse(tool_calls=(
                    ToolCall('s', 'search_papers', {}),
                    ToolCall('w', 'write', {'path': 'idea-stage/IDEA_REPORT.md', 'content': 'Metadata only; full text unavailable'})))
            return ModelResponse(content='Report ready')
    agent = PhaseAgent(tmp_path, model_client=Model())
    agent.add_tool(ToolSpec('search_papers', 'Test search', lambda ctx: ToolResult('{"papers": []}')))
    assert agent.go('Idea').iterations == 2


def test_ideas_can_finalize_after_last_tool_round(tmp_path):
    class Model:
        count = 0
        def complete(self, messages, tools):
            self.count += 1
            if self.count == 2:
                assert tools == []
                return ModelResponse(content='Report ready')
            return ModelResponse(tool_calls=(ToolCall('w', 'write', {
                'path': 'idea-stage/IDEA_REPORT.md', 'content': 'Falsifiable idea'}),))
    result = PhaseAgent(tmp_path, model_client=Model(), max_iterations=1).go('Idea')
    assert result.final == 'Report ready'
    assert result.iterations == 2


def test_resource_citation_points_to_real_file(tmp_path):
    from paper_agent.tools import invoke
    import asyncio
    library = tmp_path / 'library with spaces'
    library.mkdir()
    (library / 'resource.md').write_text('# Battery\nResource details')
    result = asyncio.run(invoke('read_battery_resource', ToolContext(tmp_path, knowledge_roots=(library,)), resource_id='resource'))
    payload = json.loads(result.output)
    assert Path(payload['absolute_path']).is_file()
    assert payload['citation_markdown'] == f'[resource](<{library / "resource.md"}>)'
