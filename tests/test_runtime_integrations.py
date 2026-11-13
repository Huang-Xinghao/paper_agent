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
    config.write_text(json.dumps({'mcpServers': {'local': {'transport': 'stdio',
        'command': sys.executable, 'args': [str(server)], 'allow_tools': ['echo', 'fail']}}}))

    async def run():
        manager = MCPManager.from_config(path=config, phase='literature')
        async with manager:
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


def test_mcp_rejects_removed_transport_alias(tmp_path):
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {'old': {'transport': 'streamable-http',
                                                      'url': 'http://127.0.0.1/mcp'}}}))
    with pytest.raises(ValueError, match='supported transports'):
        load_servers(config, phase='literature')
