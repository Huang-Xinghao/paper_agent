"""Proposal review round trips, approvals and conflicts without model/GPU calls."""

import copy
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paper_agent.api import proposal_revision, store, worker
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState
from paper_agent.proposal_review import (
    CANVAS, PROPOSAL, RECORD, CanvasDraft, draft_hash, guard_legacy_approval,
    json_bytes, load_draft, load_record, publish_candidate, render_proposal,
    require_candidate, sha,
)

api = importlib.import_module('paper_agent.api.app')


@pytest.fixture
def review(tmp_path, monkeypatch):
    projects = tmp_path / 'projects'
    projects.mkdir()
    monkeypatch.setenv('PAPER_AGENT_WEB_DATA', str(tmp_path / 'management'))
    monkeypatch.setenv('PAPER_AGENT_PROJECT_ROOTS', str(projects))
    monkeypatch.setattr(api.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    monkeypatch.setattr(api, 'terminal_projects', lambda: set())
    with TestClient(api.app) as client:
        project = client.post('/api/projects', json={'name': 'review-loop'}).json()
        root = Path(project['path'])
        (root / 'refine-logs').mkdir()
        (root / PROPOSAL).write_text('# Original\n\n## Goal\nOriginal goal and protected evidence.\n\n## Method\nOriginal method.\n')
        state = WorkflowState(run_id='origin', project_goal='Preserve research question', mode='expert',
            current_node='refine', status='awaiting_user', expert_checkpoint={
                'node': 'refine', 'status': 'pending', 'next_node': 'experiment_plan',
                'report': PROPOSAL, 'files': [PROPOSAL], 'summary': 'Original proposal', 'questions': [],
            })
        (root / 'workflow-state.json').write_bytes(json_bytes(state.to_dict()))
        draft = CanvasDraft.model_validate({
            'sourceHash': sha((root / PROPOSAL).read_bytes()),
            'nodes': [{'id':'method','position':{'x':1,'y':2},'data':{'title':'Method','sectionIds':['method']}}],
            'edges': [], 'changes': ['Human edited method'], 'review': {'sections': [
                {'id':'goal','title':'Goal','summary':'Original goal','details':'Original goal and protected evidence.','focus':False,'edited':False},
                {'id':'method','title':'Method','summary':'Human method','details':'Human method details.','focus':True,'edited':True},
            ],'feedback':'Prefer simple statistics first'},
        })
        endpoint = f"/api/projects/{project['id']}"
        saved = client.put(endpoint+'/idea-canvas', json={'sha256':'','draft':draft.model_dump()})
        assert saved.status_code == 200
        yield client, root, endpoint, project


def response_for(root, *, questions=False):
    draft = load_draft(root)
    sections = [s.model_dump() for s in draft.review.sections]
    for s in sections:
        s['edited'] = False
    sections[1]['summary'] = 'Unified simple method'
    sections[1]['details'] = 'Compare simple statistics before a complex encoder; preserve the fixed label and split.'
    nodes = [n.model_dump() for n in draft.nodes]
    nodes[0]['data']['steps'] = sections[1]['details']
    nodes[0]['data']['needsReview'] = False
    return {'review': {'sections':sections,'feedback':''}, 'nodes':nodes,'edges':[],
        'summary':'Applied human method and adjusted evidence requirements',
        'impacts':[{'kind':'related','title':'Evidence requirements','detail':'Compare the simple baseline before attributing gains.','sectionIds':['method']}],
        'questions':[{'id':'budget','question':'Which budget?','sectionIds':['method']}] if questions else [],
        'coverage':[{'sourceTitle':'Goal','sectionIds':['goal']},{'sourceTitle':'Method','sectionIds':['method']}]}


def fake_model(monkeypatch, root, *, value=None, mutate=None, finish='stop'):
    def create(**kwargs):
        assert 'tools' not in kwargs and kwargs['stream'] is True
        assert kwargs['timeout'] == 600
        if mutate:
            mutate()
        content = json.dumps(value if value is not None else response_for(root))
        def chunks():
            for part in (content[:25], content[25:]):
                yield SimpleNamespace(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content=part), finish_reason=None)])
            if finish is not None:
                yield SimpleNamespace(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content=None), finish_reason=finish)])
        return chunks()
    monkeypatch.setattr(proposal_revision, 'ModelClient', lambda **kwargs: SimpleNamespace(model='mock',client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))


def revise(root, monkeypatch, folder, run_id='revision1', **kwargs):
    fake_model(monkeypatch, root, **kwargs)
    return proposal_revision.run_revision(root, sha((root / CANVAS).read_bytes()), run_id, folder)


def test_revision_reports_real_stages_without_per_token_events(review, monkeypatch, tmp_path):
    _, root, _, _ = review
    fake_model(monkeypatch, root)
    moments = iter([10.0, 10.1])
    monkeypatch.setattr(proposal_revision, 'time', SimpleNamespace(monotonic=lambda: next(moments)))
    events = []
    proposal_revision.run_revision(root, sha((root / CANVAS).read_bytes()), 'progress',
                                   tmp_path / 'progress', on_event=events.append)
    reports = [entry for entry in events if entry['kind'] == 'proposal_revision_progress']
    assert [entry['stage'] for entry in reports] == [
        'preparing', 'waiting_model', 'receiving', 'validating', 'saving', 'ready']
    assert reports[2]['receivedChars'] == 25
    assert reports[3]['receivedChars'] == len((tmp_path / 'progress/response.txt').read_text())
    assert reports[-1]['receivedChars'] == reports[3]['receivedChars']


def test_revision_repeats_without_changing_formal_or_workflow(review, monkeypatch, tmp_path):
    client, root, endpoint, project = review
    before = (root / PROPOSAL).read_bytes(), (root / 'workflow-state.json').read_bytes()
    assert revise(root, monkeypatch, tmp_path / 'r1').status == 'completed'
    loaded = client.get(endpoint+'/idea-canvas').json()
    assert loaded['canApprove'] and loaded['revision']['synced']
    assert loaded['revision']['impacts'][0]['kind'] == 'related'
    assert loaded['revision']['diff']
    draft = loaded['draft']
    draft['review']['feedback'] = 'Narrow scope again'
    assert client.put(endpoint+'/idea-canvas', json={'sha256':loaded['sha256'],'draft':draft}).status_code == 200
    loaded = client.get(endpoint+'/idea-canvas').json()
    assert not loaded['canApprove'] and not loaded['revision']['synced']
    with pytest.raises(ValueError, match='统一修订'):
        require_candidate(root, loaded['sha256'])
    assert revise(root, monkeypatch, tmp_path / 'r2', run_id='revision2').status == 'completed'
    assert client.get(endpoint+'/idea-canvas').json()['canApprove']
    assert (root / PROPOSAL).read_bytes() == before[0]
    assert (root / 'workflow-state.json').read_bytes() == before[1]
    assert (root / 'refine-logs/review-history/revision1.json').exists()
    assert (root / 'refine-logs/review-history/revision2.json').exists()
    assert not store.rows('SELECT * FROM runs')


@pytest.mark.parametrize('failure', ['missing_coverage','dangling_node','truncated','missing_stop','concurrent','bad_json'])
def test_invalid_or_stale_model_output_never_overwrites_draft(review, monkeypatch, tmp_path, failure):
    _, root, _, _ = review
    before = (root / CANVAS).read_bytes()
    result = response_for(root)
    mutation = None
    if failure == 'missing_coverage':
        result['coverage'] = []
    if failure == 'dangling_node':
        result['nodes'][0]['data']['sectionIds'] = ['missing']
    if failure == 'bad_json':
        result = {'not':'a candidate'}
    if failure == 'concurrent':
        mutation = lambda: (root / CANVAS).write_bytes(before + b'\n')
    with pytest.raises(ValueError):
        revise(root, monkeypatch, tmp_path / 'invalid', value=result, mutate=mutation,
               finish='length' if failure == 'truncated' else None if failure == 'missing_stop' else 'stop')
    assert (root / CANVAS).read_bytes() == before + (b'\n' if mutation else b'')
    assert not (root / RECORD).exists()
    assert not (root / PROPOSAL).read_text().startswith('# 最终')


def test_questions_block_approval_until_new_revision(review, monkeypatch, tmp_path):
    client, root, endpoint, _ = review
    result = response_for(root, questions=True)
    revise(root, monkeypatch, tmp_path / 'questions', value=result)
    current = client.get(endpoint+'/idea-canvas').json()
    assert not current['canApprove']
    with pytest.raises(ValueError, match='待决策'):
        require_candidate(root, current['sha256'])
    token = client.get(endpoint).json()['checkpoint_token']
    response = client.post(endpoint+'/runs', json={'action':'proposal_approve','proposal_sha256':current['sha256'],'checkpoint_token':token})
    assert response.status_code == 409
    assert not store.rows('SELECT * FROM runs')


def test_approval_freezes_exact_reviewed_text_and_planner_reads_it(review, monkeypatch, tmp_path):
    _, root, _, _ = review
    revise(root, monkeypatch, tmp_path / 'revision')
    draft = load_draft(root)
    expected = render_proposal(draft).encode()
    runner = WorkflowRunner(SimpleNamespace(project=root), config=WorkflowConfig(mode='expert'))
    state = WorkflowState.from_dict(json.loads((root / 'workflow-state.json').read_bytes()))
    with pytest.raises(ValueError, match='方案审阅'):
        runner.resume_checkpoint(copy.deepcopy(state))
    approved = runner.resume_checkpoint(state, proposal_review_sha=sha((root / CANVAS).read_bytes()))
    assert approved.current_node == 'experiment_plan'
    receipt = approved.history[-1]
    assert receipt['event'] == 'expert_approved'
    assert receipt['sha256'] == sha(expected)
    assert (root / receipt['frozen_report']).read_bytes() == expected == (root / PROPOSAL).read_bytes()
    assert load_draft(root).sourceHash == receipt['sha256']
    guard_legacy_approval(root)
    prompts = []
    def go(prompt, **kwargs):
        prompts.append(prompt)
        (root / 'refine-logs/EXPERIMENT_PLAN.md').write_text('plan')
        return SimpleNamespace(final='planned',iterations=1)
    monkeypatch.setattr(runner,'_go',go)
    monkeypatch.setattr('paper_agent.agent.workflow.read_plan_blocks', lambda root: [])
    monkeypatch.setattr('paper_agent.agent.workflow.write_experiment_tracker', lambda *args: None)
    runner._run_experiment_plan(approved,'plan this')
    assert expected.decode() in prompts[0]
    assert 'Prefer simple statistics first' not in (root / PROPOSAL).read_text()
    (root / PROPOSAL).write_text('changed after approval')
    with pytest.raises(ValueError,match='尚未批准'):
        runner._run_experiment_plan(approved,'plan this')


def test_changed_draft_and_symlink_cannot_be_approved(review, monkeypatch, tmp_path):
    client, root, endpoint, _ = review
    revise(root, monkeypatch, tmp_path / 'revision')
    current = client.get(endpoint+'/idea-canvas').json()
    old_hash = current['sha256']
    current['draft']['review']['feedback'] = 'Human changed mind'
    client.put(endpoint+'/idea-canvas',json={'sha256':old_hash,'draft':current['draft']})
    with pytest.raises(ValueError):
        publish_candidate(root,old_hash)
    assert not (root / 'refine-logs/approved-proposals').exists()
    (root / CANVAS).unlink()
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    (root / CANVAS).symlink_to(outside)
    with pytest.raises(ValueError,match='符号链接'):
        publish_candidate(root,old_hash)
    assert outside.read_text() == '{}'


def test_historical_revision_cannot_rewind_and_auto_stays_auto(review, monkeypatch, tmp_path):
    client, root, endpoint, _ = review
    revise(root, monkeypatch, tmp_path / 'revision')
    state_path = root / 'workflow-state.json'
    state = json.loads(state_path.read_bytes())
    for mode, node in [('expert','experiment_bridge'),('auto','refine')]:
        state.update(mode=mode,current_node=node)
        state_path.write_bytes(json_bytes(state))
        current = client.get(endpoint+'/idea-canvas').json()
        assert not current['canApprove']
        response = client.post(endpoint+'/runs',json={'action':'proposal_approve','proposal_sha256':current['sha256'], 'checkpoint_token':client.get(endpoint).json()['checkpoint_token']})
        assert response.status_code == 409
        assert json.loads(state_path.read_bytes())['mode'] == mode
    assert not store.rows('SELECT * FROM runs')


def test_worker_revision_skips_cuda_and_workflow_and_keeps_checkpoint(review, monkeypatch):
    client, root, endpoint, _ = review
    from paper_agent.api import runtime
    from paper_agent.agent import workflow_cli
    monkeypatch.setattr(runtime,'check_runtime',lambda: pytest.fail('revision must not require CUDA'))
    monkeypatch.setattr(workflow_cli,'run_coordinated_workflow',lambda *a,**kw: pytest.fail('revision must not run workflow'))
    fake_model(monkeypatch,root)
    current = client.get(endpoint+'/idea-canvas').json()
    submitted = client.post(endpoint+'/runs',json={'action':'proposal_revise','proposal_sha256':current['sha256']})
    assert submitted.status_code == 202
    run_id = submitted.json()['id']
    worker.execute(run_id)
    store.run_update(run_id,status='completed')
    assert client.get(endpoint).json()['status'] == 'awaiting_user'
    current = client.get(endpoint+'/idea-canvas').json()
    assert current['canApprove'] and current['operation']['status'] == 'completed'
    # Legacy chat approval must not silently use the old formal proposal.
    denied = client.post(endpoint+'/runs',json={'action':'review','checkpoint_token':client.get(endpoint).json()['checkpoint_token']})
    assert denied.status_code == 409


def test_worker_approval_hands_off_once_and_stops_after_planning(review, monkeypatch, tmp_path):
    client, root, endpoint, _ = review
    revise(root,monkeypatch,tmp_path/'revision')
    current = client.get(endpoint+'/idea-canvas').json()
    token = client.get(endpoint).json()['checkpoint_token']
    submitted = client.post(endpoint+'/runs',json={'action':'proposal_approve','proposal_sha256':current['sha256'],'checkpoint_token':token})
    assert submitted.status_code == 202
    from paper_agent.api import runtime
    from paper_agent.agent import workflow_cli
    monkeypatch.setattr(runtime,'check_runtime',lambda: pytest.fail('text planning must not require CUDA'))
    called = []
    def run(root, **kwargs):
        called.append(kwargs)
        assert kwargs['continue_run'] and kwargs['stop_after'] == 'experiment_plan'
        assert kwargs['proposal_review_sha'] == current['sha256']
        assert 'prompt' not in kwargs and 'mode' not in kwargs
        return SimpleNamespace(status='awaiting_user',error=None)
    monkeypatch.setattr(workflow_cli,'run_coordinated_workflow',run)
    worker.execute(submitted.json()['id'])
    assert len(called) == 1

@pytest.mark.parametrize('planning_fails',[False,True])
def test_real_cli_persists_approval_before_planning_and_never_executes_experiments(review,monkeypatch,tmp_path,planning_fails):
    _,root,_,_=review
    revise(root,monkeypatch,tmp_path/'revision')
    from paper_agent.agent import workflow_cli
    from paper_agent.agent.workflow import NodeResult
    monkeypatch.setattr(workflow_cli,'PhaseAgent',lambda project,**kwargs:SimpleNamespace(project=project))
    called=[]
    def plan(self,state,prompt):
        called.append('plan')
        persisted=json.loads((root/'workflow-state.json').read_bytes())
        assert persisted['history'][-1]['event']=='expert_approved'
        assert persisted['current_node']=='experiment_plan'
        assert persisted['expert_checkpoint']['status']=='approved'
        frozen=root/persisted['history'][-1]['frozen_report']
        assert frozen.read_bytes()==(root/PROPOSAL).read_bytes()
        if planning_fails:
            raise RuntimeError('simulated planning failure')
        (root/'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Main experiment\n- 优先级：必须执行')
        (root/self._checkpoint_path('experiment_plan')).write_text(json.dumps({'summary':'Plan ready','questions':['Change baseline?','Change budget?']}))
        return NodeResult('Plan ready',{'phase':'experiment_plan'})
    monkeypatch.setattr(WorkflowRunner,'_run_experiment_plan',plan)
    monkeypatch.setattr(WorkflowRunner,'_run_experiment_bridge',lambda *args:pytest.fail('must never execute experiments'))
    state=workflow_cli._run_workflow(root,continue_run=True,proposal_review_sha=sha((root/CANVAS).read_bytes()),stop_after='experiment_plan')
    assert called==['plan']
    assert state.current_node=='experiment_plan'
    assert state.status==('failed' if planning_fails else 'awaiting_user')
    persisted=json.loads((root/'workflow-state.json').read_bytes())
    assert any(h.get('frozen_report') for h in persisted['history'])
    assert load_record(root)['approvedAt']
    if not planning_fails:
        assert state.expert_checkpoint['node']=='experiment_plan'
        assert state.expert_checkpoint['status']=='pending'
