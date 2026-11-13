"""Experiment block tracking and runtime instructions for agent-authored jobs."""

import re
from collections import Counter
from pathlib import Path
from paper_agent.storage import atomic_bytes


def read_plan_blocks(project):
    """Read stable D/B block headings and priorities from the research plan."""
    text = (Path(project) / 'refine-logs/EXPERIMENT_PLAN.md').read_text(encoding='utf-8')
    blocks = []
    seen = set()
    fenced = False
    current = None
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
            current = dict(id=identifier, name=name.strip(), priority='必须执行')
            blocks.append(current)
        elif re.match(r'^#{1,3}\s', line):
            current = None
        elif current is not None:
            priority = re.match(r'^\s*-\s*优先级\s*[:：]\s*(.+)', line)
            if priority:
                current['priority'] = priority[1].strip()
    return blocks


def write_experiment_tracker(project, blocks, jobs=(), job_states=()):
    """Render one row per planned block; counts describe execution, not conclusions."""
    records = {item['id']: item for item in job_states}
    states = {identifier: item['status'] for identifier, item in records.items()}
    def cell(value):
        return str(value).replace('|', r'\|').replace('\n', ' ')
    rows = ['# 实验跟踪器', '',
            '> 由 runner 自动生成；执行完成不代表研究结论为正。', '',
            '| 块ID | 名称 | 优先级 | 执行状态 | 完成/总数 | 失败 | 阻塞 | 进程成功退出 |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |']
    blocks = list(blocks)
    known = {block['id'] for block in blocks}
    for job in jobs:
        identifier = job.get('block_id') or job['id']
        if identifier not in known:
            blocks.append({'id': identifier, 'name': identifier, 'priority': '按计划执行'})
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
        values = [block['id'], block['name'], block['priority'], status,
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


EXPERIMENT_INTERFACE = """执行接口（由运行器维护）：
实验计划含 enabled=true 的 hpo-policy 时，按计划实现训练配置接口和 experiments/hpo_spec.json。
详细接口可用 skill_resource_read(name="experiment-plan", resource="references/hyperparameter-search.md") 读取。
run_hyperparameter_search 仅执行 smoke/寻优；run_experiments 会先完成或复用寻优，再运行正式 manifest。
正式 manifest 增加 "hpo": {"job_ids": ["需要使用最佳参数的训练job"]}，对应 argv 用独立参数 "{best_config}" 接收最终配置。
HPO trial 使用独立状态，不写入正式 D/B tracker。正式脚本必须读取配置 parameters，并保存实际生效配置。
通过 experiments/manifest.json 声明可执行任务及预期产物。
任务退出成功且产物校验通过才记为完成。JSON 对象/数组和 CSV 表允许为空；要求非空时在对应 expected_outputs 条目设置 "nonempty": true。metrics 字段始终要求存在且数值有效。
{
  "backend": "local",
  "required_inputs": ["data/input.csv"],
  "jobs": [{"id": "B1_train_seed17", "block_id": "B1", "argv": ["python", "experiments/run.py"],
    "depends_on": [], "resources": {"device": "auto"}, "timeout_seconds": 3600,
    "expected_outputs": [{"path": "results/run/metrics.json", "metrics": ["mae"]},
                         {"path": "results/run/checkpoint.pt"}]}]
}
"""
