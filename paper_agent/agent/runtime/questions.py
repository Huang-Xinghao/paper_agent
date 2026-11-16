"""Durable, task-addressed questions. Answering never resubmits a stage."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

import psutil
from uuid import uuid4

from paper_agent.agent.runtime.sessions import now
from paper_agent.storage import write_json as _write_json


AUTO_QUESTION_REQUIREMENT = (
    '自动模式下，本次任务范围内的常规选择由你依据已有证据自行决定。'
    '只有缺少不可替代的输入、凭据、权限或相互冲突的用户要求，确实无法继续时才提问；'
    '此时必须提供 blocking_reason，说明已做的检查、实际阻塞证据，以及为什么不能自行解决。'
)


def interaction_instructions(mode):
    if mode == 'auto':
        return ('\n当前协作方式：自动模式（auto）。在本次授权范围内完成任务，不为常规选择另行请求批准。'
                + AUTO_QUESTION_REQUIREMENT + '\n')
    return ('\n当前协作方式：专家模式（expert）。完成阶段产物后交由主控解读并等待用户审阅；'
            '用户的修改意见不等于批准，勿重复询问已知信息。\n')


class QuestionInterrupted(KeyboardInterrupt):
    """The suspended task cannot continue with this question's assumptions."""


class UserQuestions:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / '.paper-agent' / 'user-questions'

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'registry.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read(self, identifier):
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('Invalid question ID')
        return json.loads((self.root / (identifier + '.json')).read_text())

    def save(self, value):
        _write_json(self.root / (value['id'] + '.json'), value)

    @staticmethod
    def owner_alive(value):
        if not value.get('live'):
            return True
        # Verify PID creation time as well as the PID before resuming a live question.
        if value.get('owner_created') is None:
            return False
        try:
            process = psutil.Process(value['owner_pid'])
            return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                    and abs(process.create_time() - value['owner_created']) < .001)
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
            return False

    def cancelled(self, value):
        folder = value.get('owner_folder')
        if folder and any((Path(folder) / name).exists() for name in ('cancel', 'stop')):
            return True
        task_id = value.get('execution_task_id')
        return bool(task_id and (self.project / '.paper-agent/herdr/tasks' / task_id / 'cancel').exists())

    def _pending(self):
        pending = []
        for path in sorted(self.root.glob('*.json')):
            value = json.loads(path.read_text())
            if value.get('status') in {'awaiting_user', 'answered'} and (not self.owner_alive(value) or (value.get('live') and self.cancelled(value))):
                value.update(status='interrupted', ended_at=now(), reason='Original question owner exited or was cancelled')
                self.save(value)
            if value.get('status') == 'awaiting_user':
                pending.append(value)
        return pending

    def pending(self):
        if not self.root.exists():
            return []
        with self.locked():
            return self._pending()

    def snapshot(self):
        # Include the workflow and all tracked artifact content, not progress logs.
        from paper_agent.agent.runtime.artifacts import tracked_files
        paths = {'workflow-state.json', *tracked_files(self.project)}
        return {name: hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
                for name in sorted(paths)
                if (target := (self.project / name).resolve()).is_relative_to(self.project)}

    def create(self, question, *, role, task_id, phase, options=None, live=True, blocking_reason=''):
        if not question.strip() or len(question) > 10000:
            raise ValueError('Decision question must be nonempty and bounded')
        options = options or []
        if len(options) > 8 or any(not isinstance(o, str) or not o.strip() or len(o) > 1000 for o in options):
            raise ValueError('Provide at most eight short, nonempty options')
        with self.locked():
            if self._pending():
                raise ValueError('An unanswered question already exists; resolve it before asking another')
            value = {'id': uuid4().hex, 'question': question, 'options': options,
                     'role': role, 'task_id': task_id, 'phase': phase,
                     'status': 'awaiting_user', 'created_at': now(),
                     'owner_pid': os.getpid(), 'owner_created': psutil.Process().create_time(),
                     'live': live, 'snapshot': self.snapshot()}
            if blocking_reason:
                value['blocking_reason'] = blocking_reason
            # Role task IDs differ from the durable execution task ID.
            from paper_agent.agent.runtime.herdr import _read, directory
            active = _read(directory(self.project) / 'active.json') or {}
            execution_id = active.get('id', '')
            if re.fullmatch('[a-f0-9]{32}', execution_id) and not active.get('consumed'):
                started = _read(directory(self.project) / 'tasks' / execution_id / 'started.json') or {}
                if started.get('pid') == os.getpid():
                    value['execution_task_id'] = execution_id
            from paper_agent.agent.runtime.execution import OWNER_ENV
            value['owner_folder'] = os.environ.get(OWNER_ENV)
            self.save(value)
        return value

    def answer(self, identifier, answer, *, task_id=None):
        if not answer.strip() or len(answer) > 20000:
            raise ValueError('Answer must be nonempty and bounded')
        with self.locked():
            value = self.read(identifier)
            if value['status'] != 'awaiting_user':
                raise ValueError('此问题已回答或已失效，请查看最新问题')
            if task_id is not None and value['task_id'] != task_id:
                raise ValueError('回答与原任务不匹配，请刷新后重试')
            if not self.owner_alive(value) or (value.get('live') and self.cancelled(value)):
                value.update(status='interrupted', ended_at=now())
                self.save(value)
                raise ValueError('原任务已退出，不能自动恢复；请先核查已有结果')
            if value['snapshot'] != self.snapshot():
                value.update(status='stale', ended_at=now())
                self.save(value)
                raise ValueError('问题对应的项目版本已变化，请核查并重新提问')
            value.update(status='answered', answer=answer.strip(), answered_at=now())
            self.save(value)
            if value['role'] == 'main':
                _write_json(self.project / '.paper-agent' / 'main-decision.json', value)
            return value

    def wait(self, question, on_event):
        identifier = question['id']
        try:
            on_event('user_question', question=question)
            while True:
                with self.locked():
                    value = self.read(identifier)
                    if value['status'] == 'answered':
                        if value['snapshot'] != self.snapshot():
                            raise QuestionInterrupted('Project changed while awaiting the answer; inspect before continuing')
                        value.update(status='consumed', consumed_at=now())
                        self.save(value)
                        break
                    if value['status'] != 'awaiting_user':
                        raise QuestionInterrupted('Question is no longer valid: ' + value['status'])
                time.sleep(.1)
        except BaseException:
            with self.locked():
                value = self.read(identifier)
                if value['status'] in {'awaiting_user', 'answered'}:
                    value.update(status='interrupted', ended_at=now())
                    self.save(value)
            raise
        on_event('user_question_answered', question_id=identifier, role=value['role'])
        return value['answer']

    def interrupt_owned(self, pid):
        with self.locked():
            for value in self._pending():
                if value['live'] and value['owner_pid'] == pid:
                    value.update(status='interrupted', ended_at=now())
                    self.save(value)


def answer_command(project, text):
    """Only an explicit /answer is an answer; queries cannot consume questions."""
    parts = text.strip().split(maxsplit=2)
    if not parts or parts[0] != '/answer':
        raise ValueError('回答请使用 /answer 内容，或 /answer 问题ID 内容')
    store = UserQuestions(project)
    if len(parts) >= 2 and re.fullmatch('[a-f0-9]{32}', parts[1]):
        if len(parts) != 3:
            raise ValueError('请填写回答内容')
        identifier, answer = parts[1:]
    else:
        pending = store.pending()
        if len(pending) != 1:
            raise ValueError('没有唯一的待回答问题，请指定问题 ID')
        identifier = pending[0]['id']
        answer = text.strip()[len('/answer'):].strip()
    return store.answer(identifier, answer)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--question')
    parser.add_argument('--answer')
    args = parser.parse_args()
    store = UserQuestions(args.project)
    if args.answer is not None:
        value = answer_command(args.project, '/answer ' + ((args.question + ' ') if args.question else '') + args.answer)
    else:
        value = store.pending()
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
