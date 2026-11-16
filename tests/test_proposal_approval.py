"""Human approval can retain open questions without accepting a stale draft."""

from types import SimpleNamespace

import pytest

from paper_agent.agent.workflow import WorkflowRunner, WorkflowState
from paper_agent.proposal_review import (
    CANVAS, PROPOSAL, RECORD, CanvasDraft, draft_hash, json_bytes,
    load_draft, load_record, require_candidate, require_resolved_proposal_review,
    review_status, sha,
)
from paper_agent.storage import write_json


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / PROPOSAL
    source.parent.mkdir()
    source.write_text('# Original proposal')
    draft = CanvasDraft(sourceHash=sha(source.read_bytes()), nodes=[], edges=[], review={
        'sections': [{'id': 'method', 'title': 'Method', 'summary': 'Current design',
                      'details': 'Current method. Label provenance remains unverified.'}],
    })
    (tmp_path / CANVAS).write_bytes(json_bytes(draft.model_dump()))
    write_json(tmp_path / RECORD, {
        'id': 'review', 'draftHash': draft_hash(draft),
        'questions': [{'id': 'labels', 'question': 'Accept the evidence gap?',
                       'sectionIds': ['method']}],
    })
    state = WorkflowState('test', mode='expert', current_node='refine', status='awaiting_user',
                          expert_checkpoint={'node': 'refine', 'status': 'pending',
                                             'next_node': 'experiment_plan', 'report': PROPOSAL})
    write_json(tmp_path / 'workflow-state.json', state.to_dict())
    return draft, state


def test_approve_with_open_questions_advances_and_preserves_review(tmp_path, candidate):
    draft, state = candidate
    assert review_status(tmp_path, draft)['canApprove']
    expected = sha((tmp_path / CANVAS).read_bytes())
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    result = runner.resume_checkpoint(state, proposal_review_sha=expected)
    assert result.current_node == 'experiment_plan'
    assert result.status == 'running'
    assert result.expert_checkpoint['status'] == 'approved'
    record = load_record(tmp_path)
    assert record['questions'][0]['id'] == 'labels'
    assert record['approvedAt']
    assert (tmp_path / record['frozenPath']).read_bytes() == (tmp_path / PROPOSAL).read_bytes()
    require_resolved_proposal_review(tmp_path)
    assert not review_status(tmp_path, load_draft(tmp_path))['canApprove']
    with pytest.raises(ValueError, match='已批准'):
        require_candidate(tmp_path, sha((tmp_path / CANVAS).read_bytes()))


def test_open_questions_do_not_bypass_version_checks(tmp_path, candidate):
    draft, _ = candidate
    old_hash = sha((tmp_path / CANVAS).read_bytes())
    draft.review.sections[0].details = 'A new user edit'
    (tmp_path / CANVAS).write_bytes(json_bytes(draft.model_dump()))
    assert not review_status(tmp_path, draft)['canApprove']
    with pytest.raises(ValueError, match='已变化'):
        require_candidate(tmp_path, old_hash)
    with pytest.raises(ValueError, match='尚未由 Agent'):
        require_candidate(tmp_path, sha((tmp_path / CANVAS).read_bytes()))
