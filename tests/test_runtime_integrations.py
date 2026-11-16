"""Service boundaries without network access or paid requests."""
import asyncio
import base64
import io
import json
import sys
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from paper_agent.integrations import images
from paper_agent.integrations.mcp import MCPManager, load_servers
from paper_agent.integrations.model import ModelClient, InvalidToolCall
from paper_agent.tools.core import ToolContext
from paper_agent.tools.image_generation import generate_image


def test_dead_local_task_recovers_only_after_execution_lock_released(tmp_path):
    import fcntl
    from paper_agent.agent.runtime.execution import (
        ExecutionTask, recover_cancelled_local_task, read_result,
    )
    from paper_agent.storage import write_json
    task = ExecutionTask(id='a' * 32, project=str(tmp_path), node='experiment_plan',
                         role='experiment', instruction='Plan the experiment', project_goal='Research')
    folder = tmp_path / 'task'
    folder.mkdir()
    write_json(folder / 'request.json', task.model_dump())
    write_json(folder / 'started.json', {'id': task.id, 'pid': 123})
    with (folder / 'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not recover_cancelled_local_task(folder)
        assert not (folder / 'result.json').exists()
    assert recover_cancelled_local_task(folder)
    assert read_result(folder).status == 'interrupted'
    receipt = (folder / 'result.json').read_bytes()
    assert not recover_cancelled_local_task(folder)
    assert (folder / 'result.json').read_bytes() == receipt


def test_image_success_survives_usage_storage_failure(tmp_path, monkeypatch):
    from paper_agent.integrations import model
    content = io.BytesIO()
    Image.new('RGB', (16, 16), 'white').save(content, format='PNG')
    calls = []

    class API:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.images = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def generate(self, **kwargs):
            calls.append(kwargs)
            return NS(data=[NS(b64_json=base64.b64encode(content.getvalue()).decode())], usage={})

    monkeypatch.setenv('PAPER_AGENT_IMAGE_API_KEY', 'test-only')
    monkeypatch.setattr(images, 'OpenAI', API)
    monkeypatch.setattr(model.os, 'write', lambda *a: (_ for _ in ()).throw(OSError('ledger unavailable')))
    result = asyncio.run(generate_image(ToolContext(tmp_path), 'A method diagram', 'figures/method.png'))
    assert not result.is_error
    assert len(calls) == 1 and calls[0]['size'] == '1536x1024'
    assert (tmp_path / 'figures/method.png').read_bytes() == content.getvalue()


def test_image_timeout_is_not_replayed(tmp_path, monkeypatch):
    calls = []
    class API:
        def __init__(self, **kwargs): self.images = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def generate(self, **kwargs):
            calls.append(kwargs)
            raise TimeoutError('unknown API outcome')
    monkeypatch.setenv('PAPER_AGENT_IMAGE_API_KEY', 'test-only')
    monkeypatch.setattr(images, 'OpenAI', API)
    result = asyncio.run(generate_image(ToolContext(tmp_path), 'diagram'))
    assert result.is_error and len(calls) == 1
    assert not (tmp_path / 'figures/ai_generated/figure.png').exists()


def test_image_rejects_output_symlink_before_api(tmp_path):
    (tmp_path / 'figures').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='under project figures'):
        images.generate(tmp_path, 'diagram', 'figures/escape.png', size='auto', quality='auto')


def chunk(content=None, finish=None, usage=None):
    return NS(choices=[] if usage else [NS(index=0, delta=NS(content=content, tool_calls=[]),
                                         finish_reason=finish)], usage=usage)


class Stream:
    closed = False
    def __init__(self, chunks): self.chunks = chunks
    def __iter__(self):
        for value in self.chunks:
            if isinstance(value, Exception): raise value
            yield value
    def close(self): self.closed = True


@pytest.mark.parametrize('failure', [False, True])
def test_text_request_stream_integrity_and_usage(tmp_path, failure):
    stream = Stream([chunk('partial text'), TimeoutError('interrupted')] if failure else
                    [chunk('complete text'), chunk(finish='stop'), chunk(usage={'total_tokens': 7})])
    calls, deltas = [], []
    def create(**kwargs):
        calls.append(kwargs)
        return stream
    adapter = ModelClient(client=NS(chat=NS(completions=NS(create=create))))
    adapter.usage_workspace, adapter.usage_phase = tmp_path, 'proposal_revision'
    if failure:
        with pytest.raises(TimeoutError):
            adapter.complete_text([{'role': 'user', 'content': 'revise'}], on_delta=deltas.append)
        assert ''.join(deltas) == 'partial text'
    else:
        assert adapter.complete_text([], on_delta=deltas.append) == 'complete text'
        assert ''.join(deltas) == 'complete text'
        assert json.loads((tmp_path / '.paper-agent/usage.jsonl').read_text())['usage']['total_tokens'] == 7
    assert stream.closed and len(calls) == 1
    assert 'tools' not in calls[0]


def test_text_request_handles_nonstream_and_rejects_tools(monkeypatch):
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'false')
    deltas = []
    adapter = ModelClient(client=NS(chat=NS(completions=NS(create=lambda **kw: {'content': 'text'}))))
    assert adapter.complete_text([], on_delta=deltas.append) == 'text'
    assert deltas == ['text']
    adapter = ModelClient(responder=lambda **kw: {'tool_calls': [{'name': 'write'}]})
    with pytest.raises(InvalidToolCall): adapter.complete_text([])


def test_mcp_sdk_session_structured_receipt_and_remote_error(tmp_path):
    pytest.importorskip('mcp')
    server = tmp_path / 'server.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fixture")
@mcp.tool()
def echo(value: str) -> dict[str, str]:
    return {"value": value}
@mcp.tool()
def fail() -> str:
    raise ValueError("expected test error")
if __name__ == "__main__": mcp.run(transport="stdio")
''')
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {
        'offline': {'transport': 'stdio', 'command': str(tmp_path / 'missing-server')},
        'local': {'transport': 'stdio',
        'command': sys.executable, 'args': [str(server)], 'allow_tools': ['echo', 'fail']}}}))

    async def run():
        manager = MCPManager.from_config(path=config, phase='literature', allow_unavailable=True)
        async with manager:
            assert len(manager.unavailable) == 1
            assert 'offline' in manager.unavailable[0]
            tools = {t.name: t for t in manager.tools}
            result = await tools['local__echo'].handler(None, value='evidence')
            assert not result.is_error
            assert result.structured_content == {'value': 'evidence'}
            assert json.loads(result.output) == result.structured_content
            error = await tools['local__fail'].handler(None)
            assert error.is_error
            # An individual remote failure does not kill the session/model loop.
            again = await tools['local__echo'].handler(None, value='still running')
            assert again.structured_content == {'value': 'still running'}
        assert manager.tools == () and manager._workers == []
    asyncio.run(run())


def test_mcp_offline_agent_continues_but_explicit_discovery_fails(tmp_path, monkeypatch):
    pytest.importorskip('mcp')
    from paper_agent.agent.loop import AgentLoop
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {
        'offline': {'transport': 'stdio', 'command': str(tmp_path / 'missing-server')}}}))
    monkeypatch.setenv('PAPER_AGENT_MCP_CONFIG', str(config))
    events = []
    loop = AgentLoop(None, workspace=tmp_path, tools=(),
                     on_progress=lambda kind, data: events.append((kind, data)))

    async def model_loop(prompt, **kwargs):
        assert 'offline' in kwargs['system_prompt']
        assert '工具不可用' in kwargs['system_prompt']
        assert loop.tools == ()
        return 'continued'

    monkeypatch.setattr(loop, '_arun', model_loop)

    async def run():
        assert await loop.arun('research', system_prompt='system', phase='experiment_bridge') == 'continued'
        manager = MCPManager.from_config(phase='experiment_bridge')
        with pytest.raises(RuntimeError, match='connection/discovery failed'):
            async with manager:
                pytest.fail('Explicit discovery must report the offline service')
        assert manager._workers == []

    asyncio.run(run())
    assert any(kind == 'mcp_unavailable' for kind, _ in events)


def test_mcp_rejects_removed_transport_alias(tmp_path):
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {'old': {'transport': 'streamable-http',
                                                      'url': 'http://127.0.0.1/mcp'}}}))
    with pytest.raises(ValueError, match='supported transports'):
        load_servers(config, phase='literature')
