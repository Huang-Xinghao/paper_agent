"""Run one scoped request with existing phase capabilities, without workflow routing."""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .loop import PhaseAgent
from .workflow_cli import _write_json

IGNORED = {'.git', '.recovery', '.paper-agent', '.review-diagnostics', '__pycache__', '.web-run.lock'}
BUILD_SUFFIXES = {'.aux', '.log', '.out', '.toc', '.fls', '.fdb_latexmk', '.xdv', '.synctex.gz', '.bbl', '.blg'}


def fingerprint(path):
    if path.is_symlink():
        return 'symlink:' + os.readlink(path)
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    result = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED]
        for name in dirs + names:
            if (Path(base) / name).is_symlink():
                raise ValueError('局部修改暂不支持含符号链接的项目，请先使用实际文件副本')
        for name in names:
            if name not in IGNORED:
                path = Path(base) / name
                result[path.relative_to(root).as_posix()] = fingerprint(path)
    return result


def clone_file(source, destination):
    # Reflink where supported; never hard-link editable candidates to the project.
    import fcntl
    try:
        with open(source, 'rb') as incoming, open(destination, 'wb') as outgoing:
            fcntl.ioctl(outgoing.fileno(), 0x40049409, incoming.fileno())
        shutil.copystat(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def run_node_task(project, task, prompt, folder, *, model=None, max_iterations=100,
                  timeout=600, on_event=None):
    from paper_agent.api.dispatch import RevisionTask
    task = RevisionTask.model_validate(task)
    project, folder = Path(project).resolve(), Path(folder).resolve()
    workspace = folder / 'workspace'
    if workspace.exists():
        raise ValueError('Task workspace already exists')
    for name in task.files:
        if not (project / name).resolve().is_relative_to(project):
            raise ValueError('Target resolves outside project')
    before = inventory(project)
    shutil.copytree(project, workspace, symlinks=True, copy_function=clone_file,
                    ignore=shutil.ignore_patterns(*IGNORED))
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
            from .review_runner import ReviewRequest, ReviewRunner
            from .paper_review_inputs import paper_review_inputs
            if len(task.files) != 1 or not task.files[0].endswith('.md'):
                raise ValueError('单独审稿需要指定一个 Markdown 报告输出文件')
            review = ReviewRunner(workspace, timeout=1800).run(ReviewRequest(
                'paper', instructions + '\n本次仅审阅，报告由运行器保存，不实施论文修改。',
                paper_review_inputs(workspace), Path(task.files[0]), save_diagnostics=True))
            if review.status != 'completed':
                raise RuntimeError(review.error)
            summary = '审阅报告已生成，未自动修改论文。'
        else:
            agent = PhaseAgent(workspace, model=model, max_iterations=max_iterations, timeout=timeout,
                               on_progress=lambda kind, data: emit(kind, **data))
            phase = 'research_refine' if task.node == 'refine' else task.node
            result = agent.go(instructions, phase=phase, completion_check=lambda: None)
            summary = result.final
        if task.compile_pdf or task.node == 'paper_compile':
            emit('node_task_compile')
            # Reuse the project's latexmk configuration and path convention.
            cwd = workspace if (workspace / '.latexmkrc').is_file() else workspace / 'paper'
            source = 'paper/main.tex' if cwd == workspace else 'main.tex'
            command = ['latexmk', '-pdf', '-interaction=nonstopmode', '-halt-on-error']
            if cwd == workspace:
                command.append('-outdir=paper')
            command.append(source)
            compiled = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180)
            if compiled.returncode:
                raise RuntimeError('LaTeX compilation failed: ' + (compiled.stdout + compiled.stderr)[-3000:])
            pdf = workspace / 'paper/main.pdf'
            if not pdf.is_file() or not pdf.read_bytes().startswith(b'%PDF-'):
                raise RuntimeError('Compilation did not produce paper/main.pdf')
        after = inventory(workspace)
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        allowed = set(task.files)
        if task.compile_pdf or task.node == 'paper_compile':
            allowed.add('paper/main.pdf')
        def build_file(name):
            return Path(name).parts[0] == 'paper' and any(name.endswith(ext) for ext in BUILD_SUFFIXES)
        outside = [name for name in changed if name not in allowed and not build_file(name)]
        _write_json(folder / 'changes.json', {'changed': changed, 'outside_scope': outside})
        if outside:
            raise RuntimeError('修改超出任务范围，候选文件已保留，未应用到项目：' + ', '.join(outside[:12]))
        publish = [name for name in changed if name in allowed]
        if not publish:
            raise RuntimeError('没有检测到目标文件的修改，候选结果已保留')
        # Preflight all paths and hashes before publishing any change.
        for name in publish:
            target, candidate = project / name, workspace / name
            if (not target.resolve().is_relative_to(project) or candidate.is_symlink()
                    or not candidate.resolve().is_relative_to(workspace) or not candidate.is_file()):
                raise RuntimeError('不允许删除文件或发布符号链接：' + name)
            if fingerprint(target) != before.get(name):
                raise RuntimeError('项目文件在任务期间变化，未覆盖：' + name)
            if not candidate.stat().st_size:
                raise RuntimeError('目标产物为空：' + name)
            if candidate.suffix == '.py':
                import ast
                ast.parse(candidate.read_text(encoding='utf-8'), filename=name)
            if candidate.suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                from PIL import Image
                with Image.open(candidate) as image:
                    image.verify()
            if candidate.suffix.lower() == '.pdf':
                with candidate.open('rb') as stream:
                    if stream.read(5) != b'%PDF-':
                        raise RuntimeError('无效的 PDF：' + name)
        backup = folder / 'before'
        for name in publish:
            target = project / name
            if target.exists():
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
        applied = []
        try:
            for name in publish:
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name('.node-task-' + target.name)
                shutil.copy2(workspace / name, temporary)
                temporary.replace(target)
                applied.append(name)
        except BaseException:
            for name in applied:
                if (backup / name).exists():
                    shutil.copy2(backup / name, project / name)
                else:
                    (project / name).unlink(missing_ok=True)
            raise
        emit('assistant_output', content=summary or '指定产物修改完成。')
        emit('node_task_end', files=publish)
        result = SimpleNamespace(status='completed', error=None)
        # Keep original workflow state/checkpoint untouched; the run owns its result.
        shutil.rmtree(workspace)
        return result
    except Exception as exc:
        emit('node_task_error', error=str(exc))
        return SimpleNamespace(status='failed', error=str(exc))
