"""Visual evidence survives restarts without rewriting inline pixels."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from paper_agent.agent.context import WorkingContext, project_messages, resume_memory
from paper_agent.agent.runtime.sessions import RoleSession
from paper_agent.storage import write_json
from test_working_context import pixels


def test_saved_pixels_are_deduplicated_and_only_fresh_images_are_loaded(tmp_path):
    session = RoleSession(tmp_path, 'paper_writer')
    data = session.load()
    image = pixels(512, 512)
    data['messages'] = [{'role': 'user', 'content': 'Inspect current figure'},
                        {'role': 'user', 'content': [copy.deepcopy(image), copy.deepcopy(image)]}]
    session.save(data)
    assert 'data:image/' not in session.path.read_text()
    assert len(list(session.images.root.iterdir())) == 1
    asset = next(session.images.root.iterdir())
    mtime = asset.stat().st_mtime_ns
    session.save(data)
    assert asset.stat().st_mtime_ns == mtime
    assert session.path.stat().st_size < 2000

    loaded = session.load()
    state = SimpleNamespace(task_start_index=1, context_memory=None, messages=[
        {'role': 'system', 'content': 'Inspect evidence'}, *loaded['messages']])
    view = WorkingContext().prepare(state, object(), 40000, lambda: None, image_store=session.images)
    assert all(b['type'] == 'image_url' for b in view[-1]['content'])
    assert view[-1]['content'][0]['image_url']['url'] == image['image_url']['url']
    assert state.messages[-1]['content'][0]['type'] == 'stored_image'
    assert 'data:image/' not in json.dumps(project_messages(state.messages, for_summary=True))

    # Consumed images stay archived even if their asset is temporarily unavailable.
    asset.unlink()
    state.messages.append({'role': 'assistant', 'content': 'Figure checked; continue writing'})
    historical = WorkingContext().prepare(state, object(), 40000, lambda: None, image_store=session.images)
    assert all(b['type'] == 'text' for b in historical[-2]['content'])


def test_legacy_session_migration_retains_valid_continuation_memory(tmp_path):
    session = RoleSession(tmp_path, 'paper_writer')
    data = session.load()
    data['messages'] = [{'role': 'user', 'content': 'Old assignment'},
                        {'role': 'user', 'content': [pixels(512, 512)]},
                        {'role': 'assistant', 'content': 'Checked the figure'}]
    data['context_memory'] = {'cutoff': 3, 'pinned': 0, 'summary': 'Verified evidence',
                              'prefix_sha256': hashlib.sha256(json.dumps(
                                  data['messages'], ensure_ascii=False).encode()).hexdigest()}
    write_json(session.path, data)
    migrated = session.load()
    assert migrated['messages'][1]['content'][0]['type'] == 'stored_image'
    resumed = resume_memory(migrated['messages'], migrated['context_memory'])
    assert resumed[0]['content'] == 'Verified evidence'
    session.save(migrated)
    assert 'data:image/' not in session.path.read_text()


def test_changed_legacy_prefix_does_not_revalidate_stale_memory(tmp_path):
    session = RoleSession(tmp_path, 'paper_writer')
    data = session.load()
    data['messages'] = [{'role': 'user', 'content': [pixels(256, 256)]}]
    data['context_memory'] = {'cutoff': 1, 'summary': 'Stale summary', 'prefix_sha256': 'invalid'}
    write_json(session.path, data)
    migrated = session.load()
    assert resume_memory(migrated['messages'], migrated['context_memory']) is migrated['messages']


def test_corrupt_fresh_asset_is_rejected_before_model_request(tmp_path):
    session = RoleSession(tmp_path, 'paper_writer')
    data = session.load()
    data['messages'] = [{'role': 'user', 'content': [pixels(256, 256)]}]
    session.save(data)
    next(session.images.root.iterdir()).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='hash'):
        project_messages(data['messages'], image_store=session.images)


def test_role_tool_loop_sends_fresh_pixels_and_saves_only_references(tmp_path):
    from PIL import Image
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.integrations.model import ModelResponse, ToolCall

    Image.new('RGB', (512, 512), 'blue').save(tmp_path / 'figure.png')
    (tmp_path / 'notes.md').write_text('Evidence notes')
    requests = []

    class Model:
        def complete(self, messages, tools):
            requests.append(copy.deepcopy(messages))
            if len(requests) == 1:
                return ModelResponse(tool_calls=[ToolCall(
                    id='view-once', name='view_image', arguments={'path': 'figure.png'})])
            if len(requests) == 2:
                assert messages[-1]['content'][-1]['type'] == 'image_url'
                return ModelResponse(tool_calls=[ToolCall(
                    id='read-once', name='read', arguments={'path': 'notes.md'})])
            assert 'data:image/' not in json.dumps(messages)
            return ModelResponse(content='Verified current evidence')

    agent = PhaseAgent(tmp_path, model_client=Model(), phase='improvement')
    result = agent.go('Inspect figure and notes', completion_check=lambda: None)
    assert result.iterations == 3
    session = RoleSession(tmp_path, 'paper_writer')
    assert 'data:image/' not in session.path.read_text()
    assert any(b.get('type') == 'stored_image' for m in result.messages
               if isinstance(m.get('content'), list) for b in m['content'])
    agent.go('Explain already verified evidence', completion_check=lambda: None)
    assert len(requests) == 4
    assert len(session.load()['tasks']) == 2
