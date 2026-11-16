"""Keep real image batches usable without carrying failed tasks' pixels forever."""
import base64
import io
from types import SimpleNamespace

from PIL import Image

from paper_agent.agent.context import WorkingContext, request_tokens, compact_threshold
from paper_agent.integrations.model import ModelResponse


def pixels(width, height):
    stream = io.BytesIO()
    Image.new('RGB', (width, height), 'white').save(stream, format='PNG')
    return {'type': 'image_url', 'image_url': {
        'url': 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()}}


def image_batch():
    return [pixels(1600, 1200), pixels(1600, 1200), pixels(2048, 2048), pixels(4096, 3000)]


def test_compaction_reserves_actual_summary_limit_for_fresh_figures():
    images = image_batch()
    state = SimpleNamespace(task_start_index=1, context_memory=None, messages=[
        {'role': 'system', 'content': 'System'},
        {'role': 'user', 'content': 'Current figure task ' + 'x' * 1000},
        {'role': 'assistant', 'content': 'Historical discussion ' + '历史文献证据。' * 20000},
        {'role': 'user', 'content': images},
    ])
    class Model:
        def complete(self, messages, tools):
            assert 'data:image/' not in str(messages)
            return ModelResponse(content='Prior discussion summarized; inspect current figures.')
    assert request_tokens(state.messages[1:]) > 40000
    compacted = []
    view = WorkingContext().prepare(state, Model(), 40000, lambda: compacted.append(True))
    assert request_tokens(view[1:]) <= compact_threshold(40000)
    assert view[-1]['content'] == images
    assert state.messages[-1]['content'] == images
    assert compacted and state.context_memory


def test_new_assignment_archives_unconsumed_images_from_failed_assignment():
    old_images, current = image_batch(), pixels(512, 512)
    state = SimpleNamespace(task_start_index=4, context_memory=None, messages=[
        {'role': 'system', 'content': 'System'},
        {'role': 'user', 'content': 'Previous assignment'},
        {'role': 'assistant', 'content': 'Inspecting previous outputs'},
        {'role': 'user', 'content': old_images},
        {'role': 'user', 'content': 'New assignment: continue using saved figures'},
        {'role': 'user', 'content': [current]},
    ])
    view = WorkingContext().prepare(state, object(), 40000, lambda: None)
    assert all(block['type'] == 'text' for block in view[3]['content'])
    assert view[4]['content'].startswith('New assignment')
    assert view[5]['content'] == [current]
    assert state.messages[3]['content'] == old_images


def test_compaction_tells_summarizer_which_assignment_supersedes_old_memory():
    state = SimpleNamespace(task_start_index=2, context_memory=None, messages=[
        {'role': 'system', 'content': 'System'},
        {'role': 'user', 'content': 'Previous assignment: draw figures'},
        {'role': 'user', 'content': 'Current assignment: write paper/main.tex'},
        {'role': 'assistant', 'content': 'Read results ' + '历史结果记录。' * 5000},
        {'role': 'user', 'content': 'Continue writing'},
    ])
    requests = []

    class Model:
        def complete(self, messages, tools):
            requests.append(str(messages))
            return ModelResponse(content='Figures finished; now write paper/main.tex.')

    view = WorkingContext().prepare(state, Model(), 4000, lambda: None)
    assert any('Current authoritative assignment' in r and
               'Current assignment: write paper/main.tex' in r for r in requests)
    assert state.messages[2] in view


def test_token_budget_counts_system_tools_and_preserves_compaction_event_order():
    from paper_agent.agent.context import CONTEXT_MAX_TOKENS, AUTO_COMPACT_TOKENS
    assert compact_threshold(CONTEXT_MAX_TOKENS) == AUTO_COMPACT_TOKENS == 244800
    state = SimpleNamespace(task_start_index=1, context_memory=None, messages=[
        {'role': 'system', 'content': '必须保留系统指令。' * 200},
        {'role': 'user', 'content': '当前研究任务'},
        {'role': 'assistant', 'content': '已查证历史记录。' * 4000},
        {'role': 'user', 'content': '继续工作'},
    ])
    tools = [{'type': 'function', 'function': {'name': 'read', 'description': '工具说明。' * 200}}]
    events = []

    class Model:
        def complete(self, messages, tools):
            assert events[-1] == 'start'
            assert request_tokens(messages) <= compact_threshold(8000)
            return ModelResponse(content='已查证历史，继续当前研究任务。')

    view = WorkingContext().prepare(state, Model(), 8000,
        lambda: events.append('end'), tools=tools, on_compacting=lambda: events.append('start'))
    assert events == ['start', 'end']
    assert view[0] == state.messages[0] and state.messages[1] in view
    assert request_tokens(view, tools) <= compact_threshold(8000)
    assert request_tokens(view, tools) > request_tokens(view[1:])


def test_summary_splits_oversized_history_and_reserves_output_tokens():
    from paper_agent.agent.context import summarize_messages, text_tokens
    requests = []
    source = '中文证据不能按字符当作token。' * 1500

    class Model:
        def complete(self, messages, tools):
            requests.append(messages)
            assert request_tokens(messages) + 500 <= 2000
            return ModelResponse(content='保留已确认的研究证据。')

    summary = summarize_messages(Model(), [{'role': 'tool', 'content': source}], 2000)
    assert len(requests) > 1
    assert text_tokens(summary) < 500
    assert '保留已确认的研究证据' in requests[1][1]['content']
    # Every source character is consumed, without resending the whole raw history.
    fragments = [m[1]['content'].split('(may split a JSON record):\n', 1)[1] for m in requests]
    import json
    assert json.loads(''.join(fragments))[0]['content'] == source


def test_text_tokens_uses_bpe_not_character_count():
    from paper_agent.agent.context import text_tokens
    import tiktoken
    text = '上下文研究 evidence ' * 10
    assert text_tokens(text) == len(tiktoken.get_encoding('o200k_base').encode(text))
    assert text_tokens(text) != len(text)
