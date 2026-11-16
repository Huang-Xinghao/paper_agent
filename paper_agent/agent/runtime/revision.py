"""Run one scoped request with existing phase capabilities, without workflow routing."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.runtime.sessions import RoleSession, revision_role
from paper_agent.agent.stages import BY_NODE
from paper_agent.tools.paper_compile import compile_pdf
from paper_agent.storage import write_json as _write_json


def run_node_task(project, task, prompt, folder, *, model=None, max_iterations=300,
                  timeout=600, on_event=None):
    """Keep a role's accepted memory consistent with scoped artifact publication."""
    from paper_agent.agent.runtime.execution import RevisionTask
    validated = RevisionTask.model_validate(task)
    session = RoleSession(Path(project).resolve(), revision_role(Path(project).resolve(), validated.node))
    # PhaseAgent owns the role-session lock; the dispatcher owns the project lock.
    return _run_node_task(project, validated, prompt, folder,
                          model=model, max_iterations=max_iterations,
                          timeout=timeout, on_event=on_event, role_session=session)


def _run_node_task(project, task, prompt, folder, *, model=None, max_iterations=300,
                   timeout=600, on_event=None, role_session):
    project, folder = Path(project).resolve(), Path(folder).resolve()
    # Back up only authorized targets; edits use the real project and role memory.
    allowed = list(task.files)
    if task.compile_pdf or task.node == 'paper_compile':
        allowed.append('paper/main.pdf')
    before = {}
    backup = folder / 'before'
    for name in allowed:
        target = project / name
        if not target.resolve().is_relative_to(project) or target.is_symlink():
            raise ValueError('Target resolves outside project or is a symlink')
        before[name] = target.read_bytes() if target.is_file() else None
        if target.is_file():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    progress_path = project / 'workflow-progress.json'

    def emit(kind, **details):
        record = {'kind': kind, 'time': datetime.now(timezone.utc).isoformat(),
                  'node': task.node, **details}
        _write_json(progress_path, {'status': ('completed' if kind == 'node_task_end' else
                                              'failed' if kind == 'node_task_error' else 'running'),
                                   'current_node': task.node,
                                   'task_kind': 'revision', 'last_event': record})
        if on_event:
            on_event(record)

    emit('node_task_start', target=task.target)
    try:
        instructions = (
            f'用户原话（必须保留其意图和限制）：\n{prompt}\n\n'
            f'本次任务：{task.instruction}\n目标：{task.target}\n'
            f'允许修改的精确文件：{json.dumps(task.files, ensure_ascii=False)}\n'
            '这是已有项目的一次定向任务，不是首次生成或恢复原workflow。'
            '复用当前节点能力，只修改上述目标。其他文件可以读取但不得修改。'
            '不得启动训练、新实验、额外审稿或下游节点；不得按技能中的完整流程重做整套产物。'
            '不修改实验结果、状态、数据或其他图片，不顺带重绘或拆分图片。'
            '若无法在本次范围内完成，说明缺少什么，不扩大范围。'
            '完成前检查目标产物，报告具体修改和限制。不要写无关文档。')
        if task.node == 'improvement':
            from paper_agent.integrations.review import ReviewRequest, ReviewRunner
            from paper_agent.integrations.review import paper_review_inputs
            if len(task.files) != 1 or not task.files[0].endswith('.md'):
                raise ValueError('单独审稿需要指定一个 Markdown 报告输出文件')
            review = ReviewRunner(project, timeout=1800).run(ReviewRequest(
                'paper', instructions + '\n本次仅审阅，报告由运行器保存，不实施论文修改。',
                paper_review_inputs(project), Path(task.files[0]), save_diagnostics=True))
            if review.status != 'completed':
                raise RuntimeError(review.error)
            summary = '审阅报告已生成，未自动修改论文。'
        else:
            agent = PhaseAgent(project, model=model, max_iterations=max_iterations, timeout=timeout,
                               assignment_role=role_session.role,
                               user_question_project=project,
                               on_progress=lambda kind, data: emit(kind, **data))
            phase = BY_NODE[task.node].phase
            result = agent.go(instructions, phase=phase, completion_check=lambda: None)
            summary = result.final
        if task.compile_pdf or task.node == 'paper_compile':
            emit('node_task_compile')
            compile_pdf(project, min_pdf_bytes=0)
        publish = []
        for name in allowed:
            target = project / name
            if not target.resolve().is_relative_to(project) or target.is_symlink() or not target.is_file():
                raise ValueError('目标文件缺失或路径不合法：' + name)
            if not target.stat().st_size:
                raise ValueError('目标产物为空：' + name)
            if target.suffix == '.py':
                import ast
                ast.parse(target.read_text(encoding='utf-8'), filename=name)
            if target.read_bytes() != before[name]:
                publish.append(name)
        emit('assistant_output', content=summary or '指定产物修改完成。')
        emit('node_task_end', files=publish)
        result = SimpleNamespace(status='completed', error=None, final=summary, files=publish)
        return result
    except BaseException as exc:
        for name, original in before.items():
            target = project / name
            if not target.parent.resolve().is_relative_to(project):
                raise RuntimeError('无法恢复项目之外的目标路径：' + name) from exc
            if target.is_symlink():
                target.unlink()
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
        if not isinstance(exc, Exception):
            raise
        emit('node_task_error', error=str(exc))
        return SimpleNamespace(status='failed', error=str(exc))
