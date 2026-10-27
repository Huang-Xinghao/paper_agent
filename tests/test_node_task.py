import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_agent.agent import node_task


def fixture_project(tmp_path):
    root = tmp_path / 'project'
    (root / 'figures').mkdir(parents=True)
    (root / 'figures/figure_1.pdf').write_bytes(b'%PDF-original-1')
    (root / 'figures/figure_2.pdf').write_bytes(b'%PDF-original-2')
    (root / 'workflow-state.json').write_text('{"status":"completed","current_node":"improvement"}')
    task = {'node': 'paper_figure', 'target': '图一', 'instruction': '改布局',
            'files': ['figures/figure_1.pdf'], 'compile_pdf': False}
    folder = tmp_path / 'run'
    folder.mkdir()
    return root, task, folder


def agent_stub(monkeypatch, edit):
    class Agent:
        def __init__(self, project, **kwargs):
            self.project = project
        def go(self, prompt, *, phase, completion_check):
            assert phase == 'paper_figure'
            assert '用户原话' in prompt and '只改图一' in prompt
            edit(self.project)
            return SimpleNamespace(final='图一已更新')
    monkeypatch.setattr(node_task, 'PhaseAgent', Agent)


def test_scoped_task_updates_only_target_and_preserves_workflow(tmp_path, monkeypatch):
    root, task, folder = fixture_project(tmp_path)
    state = (root / 'workflow-state.json').read_bytes()
    agent_stub(monkeypatch, lambda p: (p / 'figures/figure_1.pdf').write_bytes(b'%PDF-revised'))
    result = node_task.run_node_task(root, task, '只改图一', folder)
    assert result.status == 'completed', result.error
    assert (root / 'figures/figure_1.pdf').read_bytes() == b'%PDF-revised'
    assert (root / 'figures/figure_2.pdf').read_bytes() == b'%PDF-original-2'
    assert (root / 'workflow-state.json').read_bytes() == state
    assert (folder / 'before/figures/figure_1.pdf').read_bytes() == b'%PDF-original-1'
    assert not (folder / 'workspace').exists()


def test_outside_scope_changes_are_not_published(tmp_path, monkeypatch):
    root, task, folder = fixture_project(tmp_path)
    def edit(p):
        (p / 'figures/figure_1.pdf').write_bytes(b'%PDF-new')
        (p / 'figures/figure_2.pdf').write_bytes(b'%PDF-unrequested')
    agent_stub(monkeypatch, edit)
    result = node_task.run_node_task(root, task, '只改图一', folder)
    assert result.status == 'failed'
    assert '超出任务范围' in result.error
    assert (root / 'figures/figure_1.pdf').read_bytes() == b'%PDF-original-1'
    assert (root / 'figures/figure_2.pdf').read_bytes() == b'%PDF-original-2'
    assert (folder / 'workspace/figures/figure_2.pdf').read_bytes() == b'%PDF-unrequested'


def test_failed_generation_does_not_modify_project(tmp_path, monkeypatch):
    root, task, folder = fixture_project(tmp_path)
    def edit(p):
        (p / 'figures/figure_1.pdf').write_bytes(b'incomplete')
        raise RuntimeError('generation failed')
    agent_stub(monkeypatch, edit)
    result = node_task.run_node_task(root, task, '只改图一', folder)
    assert result.status == 'failed'
    assert (root / 'figures/figure_1.pdf').read_bytes() == b'%PDF-original-1'


def test_changed_original_is_not_overwritten(tmp_path, monkeypatch):
    root, task, folder = fixture_project(tmp_path)
    def edit(p):
        (p / 'figures/figure_1.pdf').write_bytes(b'%PDF-agent')
        (root / 'figures/figure_1.pdf').write_bytes(b'%PDF-user-edit')
    agent_stub(monkeypatch, edit)
    result = node_task.run_node_task(root, task, '只改图一', folder)
    assert result.status == 'failed'
    assert '期间变化' in result.error
    assert (root / 'figures/figure_1.pdf').read_bytes() == b'%PDF-user-edit'


def test_compile_keeps_build_outputs_in_paper(tmp_path, monkeypatch):
    root, task, folder = fixture_project(tmp_path)
    (root / '.latexmkrc').write_text('$pdf_mode = 5;')
    (root / 'paper').mkdir()
    (root / 'paper/main.tex').write_text('source')
    (root / 'paper/main.pdf').write_bytes(b'%PDF-old')
    task['compile_pdf'] = True
    agent_stub(monkeypatch, lambda p: (p / 'figures/figure_1.pdf').write_bytes(b'%PDF-new'))

    def compile_stub(command, *, cwd, **kwargs):
        assert '-outdir=paper' in command
        assert command[-1] == 'paper/main.tex'
        (cwd / 'paper/main.pdf').write_bytes(b'%PDF-compiled')
        (cwd / 'paper/main.aux').write_text('build output')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(node_task.subprocess, 'run', compile_stub)
    result = node_task.run_node_task(root, task, '只改图一', folder)
    assert result.status == 'completed', result.error
    assert (root / 'paper/main.pdf').read_bytes() == b'%PDF-compiled'
    assert not (root / 'main.pdf').exists()
    assert not (root / 'paper/main.aux').exists()
