from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from paper_agent.agent import model_client
from paper_agent.agent.model_client import ModelClient


@pytest.fixture
def clock(monkeypatch):
    state = NS(now=0.0, sleeps=[])

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(model_client.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(model_client.time, "sleep", sleep)
    return state


def make_client(create):
    return ModelClient("test", client=NS(chat=NS(completions=NS(create=create))), timeout=300)


def status_error(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    return APIStatusError("failure", response=response, body=None)


@pytest.mark.parametrize("error", [
    RuntimeError("Upstream HTTP/2 stream failed"),
    RuntimeError("Model stream ended without a completion marker; no tools were executed"),
    APIConnectionError(request=httpx.Request("POST", "https://example.test")),
    httpx.ReadError("connection reset"),
    TimeoutError("timeout"),
    status_error(429), status_error(503),
])
def test_transient_failure_retries_same_conversation(clock, error):
    requests = []
    messages = [{"role": "user", "content": "continue"},
                {"role": "tool", "tool_call_id": "done", "content": "training completed"}]

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise error
        return {"content": "ok"}

    assert make_client(create).complete(messages, []).content == "ok"
    assert clock.sleeps == [2]
    assert all(request["messages"] == messages for request in requests)
    assert requests[1]["timeout"] == 298


def test_retry_window_expires_and_preserves_original_cause(clock):
    error = RuntimeError("Upstream HTTP/2 stream failed")
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        raise error

    with pytest.raises(RuntimeError, match="retry window exhausted") as caught:
        make_client(create).complete([], [])
    assert caught.value.__cause__ is error
    assert clock.now == 300
    assert clock.sleeps[:4] == [2, 4, 8, 15]
    assert len(requests) == len(clock.sleeps)


@pytest.mark.parametrize("error", [status_error(400), status_error(401), status_error(403),
                                       ValueError("invalid request")])
def test_nontransient_failure_does_not_retry(clock, error):
    def create(**kwargs):
        raise error

    with pytest.raises(type(error)) as caught:
        make_client(create).complete([], [])
    assert caught.value is error
    assert clock.sleeps == []


def test_partial_stream_is_closed_and_discarded_before_retry(clock):
    class Stream:
        closed = False

        def __iter__(self):
            call = NS(index=0, id="partial", function=NS(name="bash", arguments='{"command":'))
            yield NS(choices=[NS(index=0, delta=NS(content="discard", tool_calls=[call]),
                                 finish_reason=None)])
            raise httpx.ReadError("connection reset")

        def close(self):
            self.closed = True

    stream = Stream()
    responses = iter([stream, {"content": "recovered"}])
    response = make_client(lambda **kwargs: next(responses)).complete([], [])
    assert response.content == "recovered"
    assert response.tool_calls == ()
    assert stream.closed
    assert clock.sleeps == [2]


def test_httpx2_stream_errors_are_transient():
    httpx2 = pytest.importorskip("httpx2")
    assert model_client._transient_request_error(httpx2.ReadError("connection reset"))


def test_retry_stream_checks_deadline_and_closes(clock):
    class Stream:
        closed = False

        def __iter__(self):
            clock.now = 301
            yield NS(choices=[])

        def close(self):
            self.closed = True

    stream = Stream()
    with pytest.raises(TimeoutError, match="retry window exhausted"):
        model_client._stream_response(stream, deadline=300)
    assert stream.closed


def test_failed_retry_request_time_counts_towards_window(clock):
    attempts = []

    def create(**kwargs):
        attempts.append(kwargs)
        if len(attempts) > 1:
            clock.now += kwargs["timeout"]
        raise TimeoutError("read timeout")

    with pytest.raises(RuntimeError, match="retry window exhausted"):
        make_client(create).complete([], [])
    assert len(attempts) == 2
    assert clock.now == 300
