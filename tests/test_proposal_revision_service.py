"""Revisions read inputs through tools and never overwrite newer user edits."""
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
    report = tmp_path / 'literature/RESEARCH_REPORT.md'
    report.parent.mkdir()
    report.write_text('Current role report: cite papers/battery.md / Method.')
    # Old packets are irrelevant to the current role-owned research reports.
    write_json(tmp_path / '.paper-agent/wiki-retrieval/old/packet.json', {'summary': 'Obsolete'})
    baseline = (tmp_path / CANVAS).read_bytes()
    expected = sha(baseline)
    content = json.dumps({'review': {'sections': [{'id': 'unified', 'title': 'Research design',
        'summary': 'Simpler comparison', 'details': 'Full method and unchanged evidence.',
        'edited': False}], 'feedback': ''}, 'nodes': [], 'edges': [],
        'summary': 'Reorganized into a coherent section', 'impacts': [], 'questions': []})
    calls = []
    class Stream:
        def __init__(self, number): self.number = number
        def __iter__(self):
            if self.number == 1:
                requested = [('skill', {'name': 'research-refine'}),
                             ('read', {'path': PROPOSAL}), ('read', {'path': CANVAS})]
            elif self.number == 2:
                requested = [('read', {'path': 'literature/RESEARCH_REPORT.md'})]
            else:
                requested = []
            if requested:
                tool_calls = [NS(index=i, id=f'call-{self.number}-{i}', type='function',
                                 function=NS(name=name, arguments=json.dumps(args)))
                              for i, (name, args) in enumerate(requested)]
                yield NS(choices=[NS(index=0, delta=NS(content=None, tool_calls=tool_calls),
                                    finish_reason='tool_calls')])
                return
            yield NS(choices=[NS(index=0, delta=NS(content=content, tool_calls=[]), finish_reason=None)])
            if outcome == 'interrupted': raise ConnectionError('stream lost')
            yield NS(choices=[NS(index=0, delta=NS(content=None, tool_calls=[]), finish_reason='stop')])
        def close(self): pass
    def create(**kwargs):
        calls.append(json.loads(json.dumps(kwargs)))
        number = len(calls)
        if number == 1:
            first = json.dumps(kwargs['messages'], ensure_ascii=False)
            assert 'Existing evidence' not in first
            assert 'User requested a simpler comparison' not in first
            assert report.read_text() not in first
        if number == 2:
            results = [m for m in kwargs['messages'] if m['role'] == 'tool']
            assert len(results) == 3 and not any(m.get('is_error') for m in results)
            assert 'Existing evidence' in results[1]['content']
            assert 'User requested a simpler comparison' in results[2]['content']
        if number == 3:
            assert report.read_text() in kwargs['messages'][-1]['content']
        if outcome == 'concurrent' and number == 3:
            changed = {**draft, 'changes': ['Newer user edit']}
            write_json(tmp_path / CANVAS, changed)
        return Stream(number)
    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'true')
    monkeypatch.setattr(proposal_revision, 'ModelClient', lambda **kwargs: ModelClient('fixture', client=client))
    def forbid_mcp(*args, **kwargs): raise AssertionError('Local revision must not connect MCP')
    monkeypatch.setattr('paper_agent.integrations.mcp.MCPManager.from_config', forbid_mcp)
    events = []
    folder = tmp_path / '.paper-agent/revisions/test'
    if outcome == 'complete':
        assert proposal_revision.run_revision(tmp_path, expected, 'test', folder, on_event=events.append).status == 'completed'
        result = load_draft(tmp_path)
        assert len(result.review.sections) == 1 and result.review.sections[0].id == 'unified'
    else:
        with pytest.raises((ConnectionError, ValueError)):
            proposal_revision.run_revision(tmp_path, expected, 'test', folder, on_event=events.append)
        if outcome == 'interrupted': assert (tmp_path / CANVAS).read_bytes() == baseline
        else: assert load_draft(tmp_path).changes == ['Newer user edit']
    assert source.read_text().startswith('# Goal\nExisting evidence')
    assert json.loads((tmp_path / 'workflow-state.json').read_text())['status'] == 'awaiting_user'
    assert len(calls) == 3
    assert {t['function']['name'] for t in calls[0]['tools']} == {
        'read', 'find', 'ls', 'grep', 'skill', 'skill_resource_read'}
    assert not any(item['kind'] in {'assistant_delta', 'assistant_output'} for item in events)
    assert any(item['kind'] == 'proposal_revision_progress' for item in events)
    assert (folder / 'response.txt').read_text() == content
    assert (tmp_path / '.paper-agent/usage.jsonl').is_file()
    submission = json.loads((folder / 'submission.json').read_text())
    assert not {'originalProposal', 'submittedDraft', 'previousRevision', 'researchReports', 'wikiEvidence'} & submission.keys()
    assert submission['originalProposalPath'] == PROPOSAL
    assert submission['submittedDraftPath'] == CANVAS
    assert submission['submittedDraftHash'] == expected
