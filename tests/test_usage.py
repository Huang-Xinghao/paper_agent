import json
from types import SimpleNamespace as NS
import pytest
from paper_agent.agent.model_client import ModelClient, InvalidToolCall


def test_stream_usage_only_final_chunk_is_persisted(tmp_path):
    class Stream:
        def __iter__(self):
            yield NS(choices=[NS(index=0, delta=NS(content='ok', tool_calls=[]), finish_reason='stop')])
            yield NS(choices=[], usage={'prompt_tokens': 100, 'completion_tokens': 20})
        def close(self):
            pass
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return Stream()
    model = ModelClient('test', client=NS(chat=NS(completions=NS(create=create))))
    model.usage_workspace = tmp_path
    model.usage_phase = 'ideas'
    assert model.complete([], []).content == 'ok'
    assert requests[0]['stream_options'] == {'include_usage': True}
    event = json.loads((tmp_path / '.paper-agent/usage.jsonl').read_text())
    assert event['usage']['prompt_tokens'] == 100
    assert event['phase'] == 'ideas'


def test_discarded_response_usage_and_unknown_failure_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'false')
    responses = iter([
        {'tool_calls': [{'function': {}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}},
        {'content': 'recovered', 'usage': {'prompt_tokens': 12, 'completion_tokens': 3}},
    ])
    model = ModelClient('test', client=NS(chat=NS(completions=NS(create=lambda **kw: next(responses)))))
    model.usage_workspace = tmp_path
    assert model.complete([], []).content == 'recovered'
    ledger = tmp_path / '.paper-agent/usage.jsonl'
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len({event['event_id'] for event in events}) == 2
    assert sum(e['usage']['completion_tokens'] for e in events) == 5
    def fail(**kwargs):
        raise ValueError('failure')
    model.client.chat.completions.create = fail
    with pytest.raises(ValueError):
        model.complete([], [])
    assert json.loads(ledger.read_text().splitlines()[-1])['usage'] is None
