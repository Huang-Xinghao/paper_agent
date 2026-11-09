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
from uuid import uuid4

from .role_sessions import now


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
        from .workflow_cli import _write_json
        _write_json(self.root / (value['id'] + '.json'), value)

    def pending(self):
        if not self.root.exists():
            return []
        return [value for path in sorted(self.root.glob('*.json'))
                if (value := json.loads(path.read_text())).get('status') == 'awaiting_user']

    def snapshot(self):
        # Include the workflow and all tracked artifact content, not progress logs.
        from .artifact_versions import tracked_files
        paths = {'workflow-state.json', *tracked_files(self.project)}
        return {name: hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
                for name in sorted(paths)
                if (target := (self.project / name).resolve()).is_relative_to(self.project)}

    def create(self, question, *, role, task_id, phase, options=None, live=True):
        if not question.strip() or len(question) > 10000:
            raise ValueError('Decision question must be nonempty and bounded')
        options = options or []
        if len(options) > 8 or any(not isinstance(o, str) or not o.strip() or len(o) > 1000 for o in options):
            raise ValueError('Provide at most eight short, nonempty options')
        with self.locked():
            if self.pending():
                raise ValueError('An unanswered question already exists; resolve it before asking another')
            value = {'id': uuid4().hex, 'question': question, 'options': options,
                     'role': role, 'task_id': task_id, 'phase': phase,
                     'status': 'awaiting_user', 'created_at': now(),
                     'owner_pid': os.getpid(), 'live': live, 'snapshot': self.snapshot()}
            self.save(value)
        return value

    def answer(self, identifier, answer):
        if not answer.strip() or len(answer) > 20000:
            raise ValueError('Answer must be nonempty and bounded')
        with self.locked():
            value = self.read(identifier)
            if value['status'] != 'awaiting_user':
                raise ValueError('此问题已回答或已失效，请查看最新问题')
            if value['live']:
                try:
                    os.kill(value['owner_pid'], 0)
                except ProcessLookupError:
                    value.update(status='interrupted', ended_at=now())
                    self.save(value)
                    raise ValueError('原任务已退出，不能自动恢复；请先核查已有结果') from None
            if value['snapshot'] != self.snapshot():
                value.update(status='stale', ended_at=now())
                self.save(value)
                raise ValueError('问题对应的项目版本已变化，请核查并重新提问')
            value.update(status='answered', answer=answer.strip(), answered_at=now())
            self.save(value)
            if value['role'] == 'main':
                from .workflow_cli import _write_json
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
            for value in self.pending():
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
