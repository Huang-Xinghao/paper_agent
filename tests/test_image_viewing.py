import asyncio
import base64
import io

import pytest
from PIL import Image

from paper_agent.agent.loop import AgentLoop
from paper_agent.agent.model_client import ModelClient, ModelResponse, ToolCall
from paper_agent.tools import ToolContext, builtin_tools, invoke, tools_for_phase


def test_image_payload_resize_and_scope(tmp_path):
    image = tmp_path / 'plot.png'
    Image.new('RGB', (3000, 1500), 'red').save(image)
    result = asyncio.run(invoke('view_image', ToolContext(tmp_path), path='plot.png'))
    assert not result.is_error
    assert result.metadata['original_size'] == [3000, 1500]
    assert result.metadata['display_size'] == [2048, 1024]
    data = result.images[0]['image_url']['url'].split(',', 1)[1]
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == (2048, 1024)
    outside = tmp_path.parent / 'outside.png'
    Image.new('RGB', (10, 10)).save(outside)
    result = asyncio.run(invoke('view_image', ToolContext(tmp_path), path=str(outside)))
    assert result.is_error
    assert not result.images


def test_invalid_image_is_not_sent(tmp_path):
    (tmp_path / 'bad.png').write_text('not an image')
    result = asyncio.run(invoke('view_image', ToolContext(tmp_path), path='bad.png'))
    assert result.is_error and not result.images


def test_loop_delivers_visuals_after_all_tool_responses(tmp_path):
    Image.new('RGB', (100, 50), 'blue').save(tmp_path / 'plot.png')
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return ModelResponse(tool_calls=(
                ToolCall('first', 'view_image', {'path': 'plot.png'}),
                ToolCall('second', 'view_image', {'path': 'plot.png'})))
        messages = kwargs['messages']
        assert [m['role'] for m in messages[-3:]] == ['tool', 'tool', 'user']
        assert messages[-3]['tool_call_id'] == 'first'
        assert messages[-2]['tool_call_id'] == 'second'
        assert sum(p['type'] == 'image_url' for p in messages[-1]['content']) == 2
        return 'Image inspected'

    tools = tuple(t for t in builtin_tools() if t.name == 'view_image')
    loop = AgentLoop(ModelClient(responder=respond), workspace=tmp_path, tools=tools)
    state = loop.run('Inspect plot', system_prompt='Inspect images', phase='test')
    assert state.final == 'Image inspected'
    assert 'view_image' in {s.name for s in tools_for_phase('paper_figure', builtin_tools())}
