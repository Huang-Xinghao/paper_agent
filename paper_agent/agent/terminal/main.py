"""Interactive main agent; role hosts start only when main delegates work."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import sys
import threading
from uuid import uuid4

from paper_agent.agent.runtime.herdr import Client, HerdrError, _read, connect, directory, load_config, require_managed_pane
from paper_agent.agent.terminal.role import HELP, TITLES, RoleTerminal, follow_process
from paper_agent.agent.runtime.sessions import PHASE_ROLES
from paper_agent.agent.workflow_cli import is_approval, checkpoint_snapshot, approve_checkpoint
from paper_agent.storage import write_json as _write_json


class MainTerminal(RoleTerminal):
    def __init__(self, project, root, report, *, options=None, show_roles=None):
        super().__init__(project, 'main', root, report, None)
        self.options = options or {}
        self.show_roles = show_roles
        self.help_text = HELP.replace('直接对话只讨论，阶段任务由主控派发。',
            '直接与主控对话；主控按需启动执行角色。取消后等待已有任务核清。')
        self.help_text += '\n检查点输入“批准”或“批准推荐方案，继续实验规划”可正式批准并继续。'
        self.help_text += '\n执行中遇到提问：/answer 回答内容（或 /answer 问题ID 回答内容）；/status 仅查看进度。'
        self.pending_question = None

    def submit(self):
        value = self.editor.text.strip()
        if value.startswith('/answer'):
            from paper_agent.agent.runtime.questions import answer_command
            try:
                answer = answer_command(self.project, value)
                self.editor.buffer.append_to_history()
                self.editor.text = ''
                self.append('› 你的回答 → ' + TITLES[answer['role']], answer['answer'])
                self.pending_question = None
                if self.busy:
                    self.set_activity('回答已送回原任务，正在继续')
                elif not answer['live']:
                    self.editor.text = f"用户已正式回答问题 [{answer['id']}]：{answer['answer']}"
                    super().submit()
            except (ValueError, OSError) as exc:
                self.append('· 回答未提交', str(exc))
            return
        if self.busy and value and not value.startswith('/'):
            from paper_agent.agent.runtime.questions import UserQuestions
            if UserQuestions(self.project).pending():
                self.append('· 当前任务在等你的回答', '使用 /answer 回答内容 继续；/status 查看进度。草稿已保留。')
                return
        super().submit()

    def footer(self):
        if self.pending_question:
            return [('class:working', '  等待你的回答 · /answer 内容 · /status 查看进度 · Ctrl+C 取消')]
        return super().footer()

    def event(self, record):
        kind = record.get('kind')
        role = record.get('role') or PHASE_ROLES.get(record.get('phase'))
        title = 'Main' if record.get('phase') == 'coordinator' else TITLES.get(role, 'Main')
        self.model_event(record, title)
        self.action_event(record, title)
        if kind == 'assignment_start':
            task = record['assignment']
            self.set_activity('正在派发 ' + task['node'], record)
            self.append('· 分配给 ' + TITLES[task['role']], task['node'] + '\n' + task['instruction'])
        elif kind == 'herdr_submitted':
            self.set_activity('角色执行中 · ' + record['node'], record)
            if self.show_roles:
                self.show_roles(self.app.output.get_size().columns >= 120)
        elif kind == 'assignment_end':
            if self.show_roles:
                self.show_roles(False)
            self.append('· 阶段回执', str(record.get('status', 'unknown')))
            self.set_activity('主控正在核对结果', record)
        elif kind in {'role_task_start', 'wiki_model_start', 'wiki_tool_start'}:
            self.set_activity('角色准备中 · ' + str(record.get('phase', record.get('role', ''))), record)
        elif kind == 'user_question':
            question = record['question']
            self.pending_question = question
            origin = TITLES.get(question['role'], question['role'])
            choices = '\n'.join(f"{i}. {option}" for i, option in enumerate(question.get('options', []), 1))
            self.append('◆ ' + origin + ' 需要你回答', question['question'] +
                        ('\n' + choices if choices else '') +
                        f"\n问题 {question['id']} · 任务 {question['task_id']}\n输入 /answer 回答内容；回答将送回这个任务。")
            self.set_activity('等待你的回答 · ' + origin, record)
            if self.show_roles:
                self.show_roles(False)
        elif kind == 'user_question_answered':
            self.pending_question = None
            self.set_activity('已收到回答，原任务正在继续', record)
        elif kind == 'coordinator_decision':
            self.set_activity('需要你的决定', record)
        elif kind == 'checkpoint_approved':
            self.append('· 审批已完成', record['node'] + ' · 已核验并保存确认版本')
            self.set_activity('主控正在继续下一阶段', record)

    async def converse(self, value):
        folder = self.root / 'turns' / uuid4().hex
        self.folder = folder
        self.latest_log = folder / 'terminal.log'
        self.set_activity('Main · 正在思考')
        self.last_output = ''
        try:
            _write_json(folder / 'request.json', {'project': str(self.project),
                'prompt': value, 'options': self.options,
                'checkpoint': checkpoint_snapshot(self.project) if is_approval(value) else None})
            await asyncio.to_thread(self.report, 'working')
            self.process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-m',
                'paper_agent.agent.terminal.main', '--turn', str(folder),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            await follow_process(folder, self.process, self.event)
            result = _read(folder / 'result.json')
            if result is None:
                raise RuntimeError('主控进程没有返回回执。请查看 /logs 并核查项目，不会自动重跑。')
            if result.get('final') and result['final'] != self.last_output:
                self.append('● Main', result['final'])
            if result.get('error'):
                self.append('· 主控提示', result['error'])
            self.detail = {'completed': '就绪', 'awaiting_user': '等待你的决定',
                'awaiting_results': '等待实验结果', 'interrupted': '已中断，请核对已有结果'}.get(
                    result['status'], '本轮未完成，请核查')
        except Exception as exc:
            self.append('· 本轮未完成', str(exc))
            self.detail = '请核查结果，不会自动重试'
        finally:
            self.pending_question = None
            try:
                if self.show_roles:
                    await asyncio.to_thread(self.show_roles, False)
                await asyncio.to_thread(self.report, 'idle')
            except Exception:
                self.detail = 'Herdr 状态上报暂不可用'
            self.process, self.folder, self.busy = None, None, False
            self.app.invalidate()

    def run(self):
        self.app.run()  # Main has no role inbox; never prestart the team.


def run_turn(folder):
    """Isolate a coordinator turn so SIGINT safely cancels nested stage waits."""
    from paper_agent.agent.coordinator import CoordinatorAgent
    from paper_agent.agent.runtime.execution import ExecutionOwner, OWNER_ENV
    from paper_agent.agent.runtime.questions import QuestionInterrupted
    require_managed_pane()
    folder = Path(folder).resolve()
    request = _read(folder / 'request.json')
    project = Path(request['project']).resolve()
    if folder.parent != project / '.paper-agent' / 'main-terminal' / 'turns':
        raise ValueError('Invalid main turn folder')
    owner = ExecutionOwner(project, folder)
    with (folder / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / 'started.json').exists():
            raise RuntimeError('Turn already started; do not replay')
        _write_json(folder / 'started.json', {'pid': os.getpid()})
        previous_owner = os.environ.get(OWNER_ENV)
        os.environ[OWNER_ENV] = str(folder)
        stop = threading.Event()
        def watch():
            signalled = False
            while not stop.wait(.2):
                if (folder / 'cancel').exists():
                    # Stage dispatch runs in asyncio's executor thread. SIGINT
                    # alone does not interrupt that thread; also notify the
                    # role worker owned by this turn, including late dispatch.
                    owner.cancel()
                    if not signalled:
                        signalled = True
                        os.kill(os.getpid(), signal.SIGINT)
        def emit(record):
            import json
            with (folder / 'events.jsonl').open('a') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            print(record.get('kind', 'event'), flush=True)
        def guard(node):
            if node == 'experiment_bridge':
                from paper_agent.tools.experiments.executor import check_runtime
                check_runtime()
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            if (folder / 'cancel').exists():
                raise KeyboardInterrupt
            options = dict(request['options'])
            options['wait_for_user'] = True
            prompt = request['prompt']
            # Older running terminal hosts also launch a fresh worker per turn.
            approved = None
            expected = None
            if is_approval(prompt):
                expected = request['checkpoint'] if 'checkpoint' in request else checkpoint_snapshot(project)
            if expected:
                approved, stop_after = approve_checkpoint(project, expected)
                emit({'kind': 'checkpoint_approved', 'node': approved.expert_checkpoint['node'],
                      'sha256': approved.expert_checkpoint['confirmed_sha256']})
                if approved.status != 'completed':
                    options.update(first_node=approved.current_node, stop_after=stop_after)
                    if approved.expert_checkpoint['node'] == 'refine' and stop_after == 'experiment_plan':
                        options['stage_task'] = {'node': 'experiment_plan', 'role': 'experiment',
                            'instruction': '依据已批准的 refine-logs/FINAL_PROPOSAL.md 制定实验计划，'
                                           '落实已确认配置和验收标准，交付后停止，不启动训练。'}
                    prompt += ('\n终端已通过正式审批入口核验并批准当前检查点。'
                               f'请从 {approved.current_node} 继续用户已授权的任务；'
                               '历史对话中“检查点未解除”的描述已经过时。')
            if approved and approved.status == 'completed' and approved.current_node == 'improvement':
                result = CoordinatorAgent(project, on_event=emit, execution_guard=guard,
                                          **options).go(
                    '最终修订产物已正式批准，完整研究已完成。调用 curate_wiki(project_evidence=true) 沉淀成果后总结。',
                    archive_completed=True)
                output = dict(status=result.status, final=result.final, error=result.error)
            elif approved and approved.status == 'completed':
                output = dict(status='completed', final='已批准当前产物，并按原定停止位置结束。', error=None)
            else:
                result = CoordinatorAgent(project, on_event=emit, execution_guard=guard,
                                          **options).go(prompt)
                output = dict(status=result.status, final=result.final, error=result.error)
        except QuestionInterrupted as exc:
            output = dict(status='interrupted', final='', error=str(exc))
        except KeyboardInterrupt:
            output = dict(status='interrupted', final='', error='本轮已请求取消。')
        except Exception as exc:
            output = dict(status='failed', final='', error=str(exc))
        finally:
            stop.set()
            watcher.join(timeout=1)
            if 'output' in locals() and output['status'] in {'failed', 'interrupted'}:
                owner.cancel()
                unresolved = owner.settle(wait_seconds=120)
                if unresolved:
                    output['error'] = (output.get('error') or '') + ' ' + unresolved
            if previous_owner is None:
                os.environ.pop(OWNER_ENV, None)
            else:
                os.environ[OWNER_ENV] = previous_owner
        _write_json(folder / 'result.json', output)


def main():
    parser = argparse.ArgumentParser(description='Chat with the research main agent')
    parser.add_argument('--project', type=Path)
    parser.add_argument('--session')
    parser.add_argument('--model')
    parser.add_argument('--goal', help='Research goal for a new project; existing workflow goal takes precedence')
    parser.add_argument('--mode', choices=['auto', 'expert'], default='auto')
    parser.add_argument('--max-assignments', type=int, default=None)
    parser.add_argument('--max-tool-rounds', type=int, default=100)
    parser.add_argument('--turn', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.turn:
        return run_turn(args.turn)
    if not args.project or not args.session:
        parser.error('--project and --session are required; run inside Herdr')
    require_managed_pane()
    project = args.project.resolve()
    root = project / '.paper-agent' / 'main-terminal'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'host.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Main terminal already running for this project')
        previous = load_config(project)
        if previous and previous['session'] != args.session:
            parser.error('Project is bound to another Herdr session; use that session')
        config = connect(project, args.session, direction='right')
        settings_path = root / 'project.json'
        settings = _read(settings_path) or {}
        if args.goal:
            settings['goal'] = args.goal
            _write_json(settings_path, settings)
        client = Client(config['session'], config['binary'])
        pane = config['anchor_pane']
        name = 'pa-' + hashlib.sha256(str(project).encode()).hexdigest()[:8] + '-main'
        def report(status):
            client.call('pane', 'report-agent', pane, '--source', 'main-terminal',
                        '--agent', 'paper-agent-main', '--state', status)
        report('idle')
        client.call('pane', 'rename', pane, 'Main · Paper Agent')
        client.call('agent', 'rename', pane, name)
        def show_roles(visible):
            # Keep hosts alive for session reuse; show execution panes only
            # during each stage, then return to main as soon as its receipt arrives.
            try:
                client.call('pane', 'zoom', pane, '--off' if visible else '--on')
            except HerdrError:
                # A layout failure must not interrupt a scientific task or
                # prevent its result from being read by the main terminal.
                pass
        try:
            show_roles(False)
            MainTerminal(project, root, report, options={'model': args.model, 'mode': args.mode,
                'project_goal': settings.get('goal'),
                'max_assignments': args.max_assignments,
                'worker_max_iterations': args.max_tool_rounds}, show_roles=show_roles).run()
        finally:
            report('unknown')


if __name__ == '__main__':
    main()
