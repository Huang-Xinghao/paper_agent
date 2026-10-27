import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_agent.api import dispatch
from paper_agent.agent.model_client import ModelResponse, ToolCall


def project(tmp_path):
    (tmp_path / 'figures').mkdir()
    (tmp_path / 'figures/figure_1.pdf').write_bytes(b'%PDF-original')
    (tmp_path / 'figures/plot.py').write_text('plot()')
    (tmp_path / 'workflow-state.json').write_text(json.dumps({
        'status': 'completed', 'current_node': 'improvement', 'task_prompt': 'study'}))
    return {'name': 'study', 'path': str(tmp_path)}


def stub_model(monkeypatch, responses, captured):
    class Model:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, tools):
            captured.append((list(messages), tools))
            return responses.pop(0)
    monkeypatch.setattr(dispatch, 'ModelClient', Model)


def test_completed_project_routes_figure_change_through_tool_call(tmp_path, monkeypatch):
    p = project(tmp_path)
    captured = []
    args = {'node': 'paper_figure', 'target': '图一', 'instruction': '只调整图一',
            'files': ['figures/plot.py', 'figures/figure_1.pdf'], 'compile_pdf': False}
    stub_model(monkeypatch, [
        ModelResponse(tool_calls=(ToolCall('read', 'get_project_context', {'query': 'figure_1'}),)),
        ModelResponse(tool_calls=(ToolCall('edit', 'revise_artifact', args),)),
    ], captured)
    result = dispatch.dispatch_message(p, '图一换一个布局')
    assert result == {'tool': 'revise_artifact', 'arguments': args}
    assert captured[1][0][-1]['role'] == 'tool'
    assert 'figures/figure_1.pdf' in captured[1][0][-1]['content']
    names = {tool['function']['name'] for tool in captured[0][1]}
    assert names == {'get_project_context', 'run_workflow', 'resume_workflow', 'revise_artifact'}
    assert '图一换一个布局' in str(captured[0][0])


def test_status_question_returns_reply_without_execution(tmp_path, monkeypatch):
    stub_model(monkeypatch, [ModelResponse(content='当前已完成。')], [])
    assert dispatch.dispatch_message(project(tmp_path), '现在进度如何？')['tool'] == 'reply'


@pytest.mark.parametrize('calls', [
    (ToolCall('x', 'unknown', {}),),
    (ToolCall('x', 'resume_workflow', {'node': 'improvement'}),),
    (ToolCall('x', 'resume_workflow', {}), ToolCall('y', 'run_workflow', {'instruction': 'new'})),
])
def test_invalid_or_multiple_execution_requests_fail_closed(tmp_path, monkeypatch, calls):
    stub_model(monkeypatch, [ModelResponse(tool_calls=calls)], [])
    with pytest.raises(ValueError):
        dispatch.dispatch_message(project(tmp_path), '继续')


@pytest.mark.parametrize('name', ['../outside.py', '/tmp/x.py', 'results/predictions.md',
                                  'paper/audit/old.tex', 'workflow-state.json', '.env'])
def test_revision_scope_rejects_protected_paths(name):
    with pytest.raises(ValidationError):
        dispatch.RevisionTask(node='paper_figure', target='图一', instruction='修改', files=[name])


def test_context_exposes_actual_figure_references(tmp_path):
    p = project(tmp_path)
    (tmp_path / 'paper').mkdir()
    (tmp_path / 'paper/main.tex').write_text(
        r'\includegraphics[width=\linewidth]{../figures/figure_1.pdf}\caption{Framework}')
    context = dispatch.project_context(p)
    assert context['figure_references'][0] == {
        'source': 'paper/main.tex',
        'text': r'\includegraphics[width=\linewidth]{../figures/figure_1.pdf}',
    }
