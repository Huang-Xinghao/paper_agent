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

from batteryscientist.agent.runtime.sessions import now
from batteryscientist.storage import write_json as _write_json


class QuestionInterrupted(KeyboardInterrupt):
    """The suspended task cannot continue with this question's assumptions."""


class UserQuestions:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / '.batteryscientist' / 'user-questions'

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
        return bool(task_id and (self.project / '.batteryscientist/herdr/tasks' / task_id / 'cancel').exists())

    def _reconcile(self):
        """Expire questions whose waiting task exited or was cancelled; lock required."""
        for path in sorted(self.root.glob('*.json')):
            value = json.loads(path.read_text())
            if value.get('status') in {'awaiting_user', 'answered'} and (not self.owner_alive(value) or (value.get('live') and self.cancelled(value))):
                value.update(status='interrupted', ended_at=now(), reason='Original question owner exited or was cancelled')
                self.save(value)

    def _records(self):
        return [json.loads(path.read_text()) for path in sorted(self.root.glob('*.json'))]

    def pending(self):
        """List unanswered questions, first expiring abandoned live waits."""
        if not self.root.exists():
            return []
        with self.locked():
            self._reconcile()
            return [value for value in self._records() if value['status'] == 'awaiting_user']

    def latest(self, *, role):
        """Read a role's latest question after reconciling abandoned waits."""
        with self.locked():
            self._reconcile()
            return max((value for value in self._records() if value['role'] == role),
                       key=lambda value: value['created_at'], default={})

    def snapshot(self, context_paths=()):
        """Version the workflow and explicit question dependencies, including missing files."""
        snapshot = {}
        for name in sorted({'workflow-state.json', *context_paths}):
            path = Path(name)
            target = (self.project / path).resolve()
            if path.is_absolute() or not target.is_relative_to(self.project):
                raise ValueError('Question context must be a file path inside the project')
            if target.exists() and not target.is_file():
                raise ValueError('Question context must name files, not directories')
            snapshot[name] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        return snapshot

    def create(self, question, *, role, task_id, phase, options=None, live=True, blocking_reason='', context_paths=None):
        if not question.strip() or len(question) > 10000:
            raise ValueError('Decision question must be nonempty and bounded')
        options = options or []
        if len(options) > 8 or any(not isinstance(o, str) or not o.strip() or len(o) > 1000 for o in options):
            raise ValueError('Provide at most eight short, nonempty options')
        with self.locked():
            self._reconcile()
            if any(value['status'] == 'awaiting_user' for value in self._records()):
                raise ValueError('An unanswered question already exists; resolve it before asking another')
            value = {'id': uuid4().hex, 'question': question, 'options': options,
                     'role': role, 'task_id': task_id, 'phase': phase,
                     'status': 'awaiting_user', 'created_at': now(),
                     'owner_pid': os.getpid(), 'owner_created': psutil.Process().create_time(),
                     'live': live, 'snapshot': self.snapshot(context_paths or ())}
            if blocking_reason:
                value['blocking_reason'] = blocking_reason
            from batteryscientist.agent.runtime.execution import CURRENT_EXECUTION_TASK, OWNER_ENV
            execution_id = CURRENT_EXECUTION_TASK.get()
            if execution_id:
                value['execution_task_id'] = execution_id
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
            if value['snapshot'] != self.snapshot(value['snapshot']):
                value.update(status='stale', ended_at=now())
                self.save(value)
                raise ValueError('问题对应的项目版本已变化，请核查并重新提问')
            value.update(status='answered', answer=answer.strip(), answered_at=now())
            self.save(value)
            return value

    def wait(self, question, on_event):
        identifier = question['id']
        try:
            on_event('user_question', question=question)
            while True:
                with self.locked():
                    value = self.read(identifier)
                    if value['status'] == 'answered':
                        if value['snapshot'] != self.snapshot(value['snapshot']):
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
            self._reconcile()
            for value in self._records():
                if value['status'] == 'awaiting_user' and value['live'] and value['owner_pid'] == pid:
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
