from types import SimpleNamespace

from paper_agent.agent.workflow import NodeResult, WorkflowConfig, WorkflowRunner, WorkflowState


def runner_at(tmp_path, **config):
    config.setdefault("start_node", "ideas")
    return WorkflowRunner(SimpleNamespace(project=tmp_path), config=WorkflowConfig(**config))


def test_resume_does_not_spend_historical_successes(tmp_path):
    runner = runner_at(tmp_path, max_node_steps_per_run=2)
    runner._nodes['paper_compile'] = lambda *args: NodeResult('pdf')
    runner._nodes['improvement'] = lambda *args: NodeResult('improved pdf')
    state = WorkflowState('old', current_node='paper_compile', transitions=40)
    state = runner.run('task', state=state)
    assert state.status == 'completed'
    assert (state.transitions, state.fault_retry_count) == (42, 0)


def test_failed_attempt_and_recovery_count_fault_retries(tmp_path):
    runner = runner_at(tmp_path, max_node_steps_per_run=1)
    def fail(*args):
        raise TimeoutError('model timed out')
    runner._nodes['ideas'] = fail
    state = runner.run('task')
    assert state.status == 'failed'
    assert (state.transitions, state.fault_retry_count) == (0, 0)
    state = WorkflowState.from_dict(state.to_dict())
    (tmp_path / "literature").mkdir()
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text("Prior literature")
    (tmp_path / "data-search").mkdir()
    (tmp_path / "data-search/DATA_REPORT.md").write_text("Prior dataset findings")
    state = runner.start_from_project('ideas', 'task', state)
    runner._nodes['ideas'] = lambda *args: NodeResult('idea')
    state = runner.run('task', state=state, stop_after='ideas')
    assert state.status == 'completed'
    assert (state.transitions, state.fault_retry_count) == (1, 1)
    assert state.failed_node is None


def test_old_failed_state_can_record_next_retry(tmp_path):
    state = WorkflowState.from_dict(dict(run_id='old', current_node='ideas',
        status='failed', failed_node='ideas', error='node ideas failed: timeout', transitions=25))
    runner = runner_at(tmp_path)
    (tmp_path / "literature").mkdir()
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text("Prior literature")
    (tmp_path / "data-search").mkdir()
    (tmp_path / "data-search/DATA_REPORT.md").write_text("Prior dataset findings")
    runner._nodes['ideas'] = lambda *args: NodeResult('idea')
    runner.start_from_project('ideas', 'task', state)
    runner.run('task', state=state, stop_after='ideas')
    assert state.fault_retry_count == 1
    assert state.transitions == 26


def test_accidental_routing_loop_still_stops(tmp_path):
    runner = runner_at(tmp_path, max_node_steps_per_run=2)
    runner._nodes['ideas'] = lambda *args: NodeResult('idea')
    runner._next_node = lambda *args: 'ideas'
    state = runner.run('task')
    assert state.status == 'failed'
    assert 'max_node_steps_per_run' in state.error
    assert state.transitions == 2


def test_waiting_for_inputs_is_not_a_fault_retry(tmp_path):
    state = WorkflowState('old', current_node='ideas', status='awaiting_results', transitions=30)
    runner = runner_at(tmp_path)
    runner._nodes['ideas'] = lambda *args: NodeResult('idea')
    runner.run('task', state=state, stop_after='ideas')
    assert state.status == 'completed'
    assert state.fault_retry_count == 0


