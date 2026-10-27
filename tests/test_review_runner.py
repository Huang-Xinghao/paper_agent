"""Exercise the actual CLI subprocess boundary without paid model calls."""

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from paper_agent.agent.review_runner import ReviewRequest, ReviewRunner


def fake_codex(tmp_path, body):
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\nimport json, sys, os, time\n"
                          "prompt = sys.stdin.read()\n" + body)
    executable.chmod(0o755)
    return str(executable)


def request(tmp_path, kind="idea", **kwargs):
    (tmp_path / "input.md").write_text("Current research evidence")
    return ReviewRequest(kind, "Check the evidence carefully", (Path("input.md"),), **kwargs)


SUCCESS = """
print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-test'}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '# Review\\nNeeds revisions'}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 20}}))
"""


@pytest.mark.parametrize("kind, output", [
    ("idea", "idea-stage/review.md"),
    ("code", "experiments/review.md"),
    ("paper", "paper/review.md"),
])
def test_success_writes_only_review_and_returns_usage(tmp_path, kind, output):
    checks = """
assert sys.argv[1] == 'exec'
assert 'resume' not in sys.argv
assert 'sandbox_mode="read-only"' in sys.argv
assert 'approval_policy="never"' in sys.argv
assert sys.argv[-1] == '-'
assert 'Check the evidence carefully' in prompt
assert 'input.md' in prompt
assert 'Current research evidence' not in prompt
"""
    executable = fake_codex(tmp_path, checks + SUCCESS)
    task = request(tmp_path, kind)
    before = {p for p in tmp_path.rglob('*') if p.is_file()}
    result = ReviewRunner(tmp_path, executable=executable, model="stub").run(task)
    assert result.status == "completed"
    assert result.thread_id == "thread-test"
    assert result.usage["input_tokens"] == 100
    assert result.report == tmp_path / output
    assert result.report.read_text() == "# Review\nNeeds revisions"
    after = {p for p in tmp_path.rglob('*') if p.is_file()}
    assert after - before == {result.report}
    assert not (tmp_path / 'reviews').exists()


@pytest.mark.parametrize("body", [
    "print('invalid JSON')",
    "print(json.dumps({'type':'turn.completed'}))",
    SUCCESS + "sys.exit(1)",
    SUCCESS + "print(json.dumps({'type':'turn.failed','error':{'message':'capacity'}}))",
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'partial'}}))",
    "print('[]')",
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':['invalid']}}))",
])
def test_incomplete_or_failed_turn_is_never_success(tmp_path, body):
    result = ReviewRunner(tmp_path, executable=fake_codex(tmp_path, body)).run(request(tmp_path))
    assert result.status == "failed"
    assert result.error
    assert not result.report.exists()


def test_missing_executable_returns_error_without_files(tmp_path):
    result = ReviewRunner(tmp_path, executable=str(tmp_path / "missing")).run(request(tmp_path))
    assert result.status == "failed"
    assert result.exit_code is None
    assert not result.report.exists()
    assert not (tmp_path / "reviews").exists()


def test_changed_inputs_invalidate_review(tmp_path):
    executable = fake_codex(tmp_path, SUCCESS + "open('input.md', 'w').write('changed')")
    result = ReviewRunner(tmp_path, executable=executable).run(request(tmp_path))
    assert result.status == "failed"
    assert "changed during review" in result.error
    assert not result.report.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        ReviewRunner(tmp_path, timeout=timeout)


def test_input_outside_workspace_is_rejected(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("evidence")
    with pytest.raises(ValueError, match="inside workspace"):
        ReviewRunner(workspace).run(ReviewRequest("code", "review", (outside,)))


def test_timeout_kills_agent_and_tool_child(tmp_path):
    executable = fake_codex(tmp_path, """
import subprocess, signal
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
open('child.pid', 'w').write(str(child.pid))
print(json.dumps({'type':'thread.started', 'thread_id':'timed-out-thread'}), flush=True)
time.sleep(30)
""")
    result = ReviewRunner(tmp_path, executable=executable, timeout=0.5,
                          termination_grace=0.1).run(request(tmp_path))
    assert result.status == "timed_out"
    assert result.thread_id == "timed-out-thread"
    child = int((tmp_path / "child.pid").read_text())
    # A killed descendant may briefly remain a zombie until the host reaps it.
    state_file = Path(f"/proc/{child}/stat")
    deadline = time.monotonic() + 2
    while state_file.exists() and state_file.read_text().split()[2] != "Z":
        if time.monotonic() > deadline:
            os.kill(child, signal.SIGKILL)
            pytest.fail("tool subprocess survived review timeout")
        time.sleep(0.02)


def test_cli_runs_independently_and_reports_failed_execution(tmp_path):
    import subprocess
    request(tmp_path)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Review current evidence")
    env = dict(os.environ, CODEX_BIN=fake_codex(tmp_path, SUCCESS))
    command = [sys.executable, "-m", "paper_agent.agent.review_cli", "idea", "input.md",
               "--workspace", str(tmp_path), "--instructions-file", str(instructions)]
    completed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "completed"
    fake_codex(tmp_path, "sys.exit(78)")
    failed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["exit_code"] == 78


def test_cli_sigterm_stops_agent_without_publishing_report(tmp_path):
    import subprocess
    request(tmp_path)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Review current evidence")
    executable = fake_codex(tmp_path, """
open('agent.pid', 'w').write(str(os.getpid()))
print(json.dumps({'type':'thread.started','thread_id':'interrupted-thread'}), flush=True)
time.sleep(30)
""")
    command = [sys.executable, "-m", "paper_agent.agent.review_cli", "idea", "input.md",
               "--workspace", str(tmp_path), "--instructions-file", str(instructions)]
    proc = subprocess.Popen(command, env=dict(os.environ, CODEX_BIN=executable),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    agent_pid = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "agent.pid").exists():
            assert time.monotonic() < deadline, "agent never started"
            time.sleep(0.02)
        agent_pid = int((tmp_path / "agent.pid").read_text())
        proc.terminate()
        proc.wait(timeout=5)
        assert not (tmp_path / "idea-stage/review.md").exists()
        assert not (tmp_path / "reviews").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(agent_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if agent_pid is not None:
            try:
                os.kill(agent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_timeout_saves_diagnostics_without_publishing_partial_report(tmp_path):
    executable = fake_codex(tmp_path, """
print(json.dumps({'type':'thread.started','thread_id':'saved-thread'}), flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Partial findings'}}), flush=True)
time.sleep(30)
""")
    result = ReviewRunner(tmp_path, executable=executable, timeout=.3,
                          termination_grace=.05).run(request(tmp_path, save_diagnostics=True))
    assert result.status == 'timed_out'
    assert not result.report.exists()
    folder = next((tmp_path / 'idea-stage/.review-diagnostics/review').iterdir())
    saved = json.loads((folder / 'result.json').read_text())
    assert saved['thread_id'] == 'saved-thread'
    assert saved['status'] == 'timed_out'
    assert saved['elapsed_seconds'] >= .3
    assert 'INCOMPLETE REVIEW' in (folder / 'partial.md').read_text()
    assert 'Partial findings' in (folder / 'events.jsonl').read_text()
    assert json.loads((folder / 'request.json').read_text())['inputs'][0]['sha256']
