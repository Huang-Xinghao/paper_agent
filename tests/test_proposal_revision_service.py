"""Text revisions use the shared client and never overwrite newer user edits."""
import json
from types import SimpleNamespace as NS

import pytest

from paper_agent.api import proposal_revision
from paper_agent.integrations.model import ModelClient
from paper_agent.proposal_review import CANVAS, PROPOSAL, load_draft, sha
from paper_agent.storage import write_json


@pytest.mark.parametrize('outcome', ['complete', 'interrupted', 'concurrent'])
def test_revision_publication_boundary(tmp_path, monkeypatch, outcome):
    source = tmp_path / PROPOSAL
    source.parent.mkdir()
    source.write_text('# Goal\nExisting evidence\n## Method\nExisting method')
    draft = {'sourceHash': sha(source.read_bytes()), 'nodes': [], 'edges': [], 'changes': [],
             'review': {'sections': [{'id': 'method', 'title': 'Method', 'summary': 'User edit',
                        'details': 'User requested a simpler comparison', 'edited': True}], 'feedback': ''}}
    write_json(tmp_path / CANVAS, draft)
    write_json(tmp_path / 'workflow-state.json', {'status': 'awaiting_user', 'mode': 'expert'})
    baseline = (tmp_path / CANVAS).read_bytes()
    expected = sha(baseline)
    content = json.dumps({'review': {'sections': [{'id': 'unified', 'title': 'Research design',
        'summary': 'Simpler comparison', 'details': 'Full method and unchanged evidence.',
        'edited': False}], 'feedback': ''}, 'nodes': [], 'edges': [],
        'summary': 'Reorganized into a coherent section', 'impacts': [], 'questions': []})
    calls = []
    class Stream:
        def __iter__(self):
            yield NS(choices=[NS(index=0, delta=NS(content=content, tool_calls=[]), finish_reason=None)])
            if outcome == 'interrupted': raise ConnectionError('stream lost')
            yield NS(choices=[NS(index=0, delta=NS(content=None, tool_calls=[]), finish_reason='stop')])
        def close(self): pass
    def create(**kwargs):
        calls.append(kwargs)
        if outcome == 'concurrent':
            changed = {**draft, 'changes': ['Newer user edit']}
            write_json(tmp_path / CANVAS, changed)
        return Stream()
    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'true')
    monkeypatch.setattr(proposal_revision, 'ModelClient', lambda **kwargs: ModelClient('fixture', client=client))
    folder = tmp_path / '.paper-agent/revisions/test'
    if outcome == 'complete':
        assert proposal_revision.run_revision(tmp_path, expected, 'test', folder).status == 'completed'
        result = load_draft(tmp_path)
        assert len(result.review.sections) == 1 and result.review.sections[0].id == 'unified'
    else:
        with pytest.raises((ConnectionError, ValueError)):
            proposal_revision.run_revision(tmp_path, expected, 'test', folder)
        if outcome == 'interrupted': assert (tmp_path / CANVAS).read_bytes() == baseline
        else: assert load_draft(tmp_path).changes == ['Newer user edit']
    assert source.read_text().startswith('# Goal\nExisting evidence')
    assert json.loads((tmp_path / 'workflow-state.json').read_text())['status'] == 'awaiting_user'
    assert len(calls) == 1 and 'tools' not in calls[0]
    assert (folder / 'response.txt').read_text() == content
    assert (tmp_path / '.paper-agent/usage.jsonl').is_file()
