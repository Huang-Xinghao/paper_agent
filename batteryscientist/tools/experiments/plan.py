"""Experiment block tracking for agent-authored jobs."""

import re
from collections import Counter
from pathlib import Path
from batteryscientist.storage import atomic_bytes


def read_plan_blocks(project):
    """Read stable D/B block headings from the research plan."""
    text = (Path(project) / 'refine-logs/EXPERIMENT_PLAN.md').read_text(encoding='utf-8')
    blocks = []
    seen = set()
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r'^#{2,6}\s+(?:(?:区块|实验块|Block)\s+)?([DB][1-9]\d*)\s*[:：—–-]\s*(.+?)\s*#*$', line, re.I)
        if heading:
            identifier, name = heading.groups()
            identifier = identifier.upper()
            if identifier in seen:
                raise ValueError(f'Duplicate experiment block: {identifier}')
            seen.add(identifier)
            blocks.append(dict(id=identifier, name=name.strip()))
    return blocks


def write_experiment_tracker(project, blocks, jobs=(), job_states=()):
    """Render one row per planned block; counts describe execution, not conclusions."""
    records = {item['id']: item for item in job_states}
    states = {identifier: item['status'] for identifier, item in records.items()}
    def cell(value):
        return str(value).replace('|', r'\|').replace('\n', ' ')
    rows = ['# 实验跟踪器', '',
            '> 由 runner 自动生成；执行完成不代表研究结论为正。', '',
            '| 块ID | 名称 | 执行状态 | 完成/总数 | 失败 | 阻塞 | 进程成功退出 |',
            '| --- | --- | --- | --- | --- | --- | --- |']
    blocks = list(blocks)
    known = {block['id'] for block in blocks}
    for job in jobs:
        identifier = job.get('block_id') or job['id']
        if identifier not in known:
            blocks.append({'id': identifier, 'name': identifier})
            known.add(identifier)
    for block in blocks:
        members = [job for job in jobs if (job.get('block_id') or job['id']) == block['id']]
        counts = Counter(states.get(job['id'], 'pending') for job in members)
        total = len(members)
        failed = counts['failed'] + counts['interrupted']
        status = ('尚未部署' if not total else '执行完成' if counts['completed'] == total
                  else '产物校验失败' if counts['artifact_invalid']
                  else '存在失败' if failed else '依赖产物无效' if counts['dependency_invalid']
                  else '运行中' if counts['running']
                  else '阻塞' if counts['blocked'] else '待运行')
        executed = sum(records.get(job['id'], {}).get('execution_status', states.get(job['id'])) == 'completed'
                       for job in members)
        values = [block['id'], block['name'], status,
                  f"{counts['completed']}/{total}", failed,
                  counts['blocked'] + counts['dependency_invalid'], f'{executed}/{total}']
        rows.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
    problems = [item for item in records.values() if item.get('error')]
    if problems:
        rows.extend(['', '## 任务问题', '', '| 任务 | 状态 | 原因 |', '| --- | --- | --- |'])
        labels = {'artifact_invalid': '产物校验失败', 'dependency_invalid': '依赖产物无效',
                  'failed': '执行失败', 'interrupted': '执行中断', 'blocked': '阻塞'}
        for item in problems:
            rows.append('| ' + ' | '.join(cell(v) for v in (
                item['id'], labels.get(item['status'], item['status']), item['error'])) + ' |')
    path = Path(project) / 'refine-logs/EXPERIMENT_TRACKER.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(path, ('\n'.join(rows) + '\n').encode('utf-8'))
