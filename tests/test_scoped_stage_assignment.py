"""Main may refine a stage instruction while retaining its authorized scope."""

from types import SimpleNamespace

import pytest

from paper_agent.agent import coordinator
from paper_agent.agent.workflow import WorkflowRunner


def test_stage_details_preserve_original_instruction_and_stop(tmp_path, monkeypatch):
    original = {'node': 'experiment_plan', 'role': 'experiment',
                'instruction': 'Plan experiments only; do not train models.'}
    main = coordinator.CoordinatorAgent(tmp_path, model_client=object(),
        stage_task=original, first_node='experiment_plan', stop_after='experiment_plan',
        project_goal='Study battery lifetime', mode='expert')
    monkeypatch.setattr(WorkflowRunner, 'start_from_project', lambda *a: None)
    calls = []

    def execute(project, node, instruction, **options):
        calls.append((node, instruction, options))
        return SimpleNamespace(status='awaiting_user', error=None, node_results={},
                               expert_checkpoint={'node': node, 'status': 'pending'})

    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    main._delegate(None, {**original, 'instruction': 'Use the approved proposal and specify CPU budgets.'})
    assert len(calls) == 1
    node, instruction, options = calls[0]
    assert node == 'experiment_plan' and options['role'] == 'experiment'
    assert original['instruction'] in instruction
    assert 'specify CPU budgets' in instruction
    assert options['workflow_stop_after'] == 'experiment_plan'
    assert main.worker_status == 'awaiting_user'
    with pytest.raises(ValueError, match='Dispatch stopped'):
        main._delegate(None, original)
    assert len(calls) == 1


@pytest.mark.parametrize('changed', [
    {'node': 'experiment_bridge'}, {'role': 'idea_generator'},
])
def test_refined_stage_cannot_change_node_or_role(tmp_path, changed):
    original = {'node': 'experiment_plan', 'role': 'experiment', 'instruction': 'Plan only.'}
    main = coordinator.CoordinatorAgent(tmp_path, model_client=object(), stage_task=original)
    with pytest.raises(ValueError, match="explicit assignment"):
        main._delegate(None, {**original, **changed})
    assert main.assignments == 0


@pytest.mark.parametrize('scoped', [False, True])
def test_definite_failure_can_create_fresh_assignment(tmp_path, monkeypatch, scoped):
    assignment = {'node': 'experiment_bridge', 'role': 'experiment',
                  'instruction': 'Continue implementation using existing files and receipts.'}
    main = coordinator.CoordinatorAgent(tmp_path, model_client=object(), mode='auto',
        project_goal='Study battery lifetime', stage_task=assignment if scoped else None)
    monkeypatch.setattr(WorkflowRunner, 'start_from_project', lambda *a: None)
    calls = []

    def execute(project, node, instruction, **options):
        calls.append(options)
        return SimpleNamespace(status='failed' if len(calls) == 1 else 'completed',
            error='model exceeded max_iterations=300' if len(calls) == 1 else None,
            node_results={}, expert_checkpoint=None)

    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    assert main._delegate(None, assignment).is_error
    assert main.halted is None
    assert not main._delegate(None, assignment).is_error
    assert len(calls) == 2
    assert all(call['max_iterations'] == 300 for call in calls)


def test_unknown_execution_still_blocks_retry(tmp_path):
    main = coordinator.CoordinatorAgent(tmp_path, model_client=object(), mode='auto')
    main.halted = 'Task outcome pending; reconcile before retrying'
    main.worker_status = 'failed'
    with pytest.raises(ValueError, match='Dispatch stopped'):
        main._execution_gate()


@pytest.mark.parametrize('fails', [False, True])
def test_revision_edits_directly_with_target_backup_and_one_role_lock(tmp_path, monkeypatch, fails):
    from paper_agent.agent.runtime import revision
    from paper_agent.agent.runtime.sessions import RoleSession
    report = tmp_path / 'PAPER_PLAN.md'
    report.write_text('Original')
    # An unrelated large results tree or symlink need not be cloned or inventoried.
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/external').symlink_to('/tmp')
    class Agent:
        def __init__(self, project, **kwargs):
            assert project == tmp_path
        def go(self, *args, **kwargs):
            with RoleSession(tmp_path, 'paper_writer').locked():
                report.write_text('Revised')
                if fails:
                    raise RuntimeError('Revision failed')
                return SimpleNamespace(final='Revised the plan')
    monkeypatch.setattr(revision, 'PhaseAgent', Agent)
    folder = tmp_path / 'task'
    result = revision.run_node_task(tmp_path, dict(node='paper_plan', target='PAPER_PLAN.md',
        files=['PAPER_PLAN.md'], instruction='Revise the plan'), 'Revise', folder)
    assert result.status == ('failed' if fails else 'completed')
    assert report.read_text() == ('Original' if fails else 'Revised')
    assert (folder / 'before/PAPER_PLAN.md').read_text() == 'Original'
    assert not (folder / 'workspace').exists()


def test_revision_rollback_does_not_follow_replaced_target_symlink(tmp_path, monkeypatch):
    from paper_agent.agent.runtime import revision
    project = tmp_path / 'project'
    project.mkdir()
    report = project / 'PAPER_PLAN.md'
    report.write_text('Original plan')
    outside = tmp_path / 'outside.md'
    outside.write_text('Outside content')
    class Agent:
        def __init__(self, *args, **kwargs): pass
        def go(self, *args, **kwargs):
            report.unlink()
            report.symlink_to(outside)
            return SimpleNamespace(final='Done')
    monkeypatch.setattr(revision, 'PhaseAgent', Agent)
    result = revision.run_node_task(project, dict(node='paper_plan', target='PAPER_PLAN.md',
        files=['PAPER_PLAN.md'], instruction='Revise'), 'Revise', project / 'task')
    assert result.status == 'failed'
    assert report.read_text() == 'Original plan' and not report.is_symlink()
    assert outside.read_text() == 'Outside content'
