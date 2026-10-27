from pathlib import Path

from paper_agent.agent.loop import ToolCall
from paper_agent.agent.model_client import ModelResponse
from paper_agent.agent.model_client import ModelClient
from paper_agent.agent.loop import PhaseAgent
from paper_agent.tools import ToolResult, ToolSpec


class FakeModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if len(self.calls) == 1:
            return ModelResponse(tool_calls=(ToolCall("1", "write", {
                "path": "idea-stage/IDEA_REPORT.md", "content": "A falsifiable battery idea."}),))
        assert any(message["role"] == "tool" and "Wrote" in message["content"]
                   for message in messages)
        return ModelResponse(content="Idea artifact created.")


def test_paper_plan_recovers_premature_completion(tmp_path):
    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content="Here is the plan, but not saved.")
            if self.calls == 2:
                assert "PAPER_PLAN.md" in messages[-1]["content"]
                return ModelResponse(tool_calls=(ToolCall("save", "write", {
                    "path": "PAPER_PLAN.md", "content": "# Evidence-based paper plan"}),))
            return ModelResponse(content="Saved.")

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="paper_plan", max_iterations=3)
    result = agent.go("Plan the paper")
    assert result.final == "Saved."
    assert (tmp_path / "PAPER_PLAN.md").is_file()


def test_paper_plan_missing_artifact_exhausts_bounded_budget(tmp_path):
    import pytest

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content="Done without writing.")

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="paper_plan", max_iterations=2)
    with pytest.raises(RuntimeError, match="paper_plan cannot finish without: PAPER_PLAN.md"):
        agent.go("Plan the paper")


def test_paper_write_requests_missing_section_before_finishing(tmp_path):
    (tmp_path / 'paper').mkdir()
    (tmp_path / 'paper/main.tex').write_text('main')
    (tmp_path / 'paper/references.bib').write_text('% references')
    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content='Done')
            if self.calls == 2:
                assert 'paper/sections/*.tex' in messages[-1]['content']
                return ModelResponse(tool_calls=(ToolCall('section', 'write', {
                    'path': 'paper/sections/intro.tex', 'content': 'Introduction'}),))
            return ModelResponse(content='Saved')
    agent = PhaseAgent(tmp_path, model_client=Model(), phase='paper_write', max_iterations=3)
    assert agent.go('Write paper').final == 'Saved'
    assert (tmp_path / 'paper/sections/intro.tex').is_file()


def test_paper_agent_runs_tool_call_loop(tmp_path):
    fake = FakeModel()
    agent = PhaseAgent(tmp_path, model_client=fake, phase="ideas")
    result = agent.go("Create an initial idea artifact")
    assert result.final == "Idea artifact created."
    assert (tmp_path / "idea-stage/IDEA_REPORT.md").read_text() == "A falsifiable battery idea."
    assert len(fake.calls) == 2
    assert any(schema["function"]["name"] == "write" for schema in fake.calls[0][1])


def test_custom_tool_is_available_even_when_phase_does_not_list_it(tmp_path):
    (tmp_path / "literature").mkdir()
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text("Fixture report")
    seen = []

    def handler(ctx, value: str):
        seen.append(value)
        return ToolResult("custom result")

    class Model:
        def complete(self, messages, tools):
            if not any(m["role"] == "tool" for m in messages):
                return ModelResponse(tool_calls=(ToolCall("x", "custom", {"value": "ok"}),))
            return ModelResponse(content="done")

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="literature")
    agent.add_tool(ToolSpec("custom", "A custom operation", handler))
    assert agent.go("run custom") .final == "done"
    assert seen == ["ok"]


def test_phase_selects_local_skill_without_review_tools(tmp_path):
    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content="done")

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="novelty")
    agent.configure()
    result = agent.go("check novelty")
    assert "novelty-check" in result.messages[0]["content"]
    names = {spec.name for spec in agent.tools}
    assert "external_review" not in names


def test_agent_uses_packaged_skills_and_wiki_scanner(tmp_path):
    from paper_agent.tools import research_wiki

    agent = PhaseAgent(tmp_path, model_client=FakeModel(), phase="literature")
    assert agent.skills_root == Path(__file__).resolve().parents[1] / "paper_agent" / "skills"
    assert (agent.skills_root / "research-lit" / "SKILL.md").is_file()
    assert research_wiki.scan_for_threats is not None


def test_model_client_passes_timeout_to_openai_compatible_client():
    class Completions:
        def create(self, **kwargs):
            assert kwargs["timeout"] == 3.5
            assert "tools" not in kwargs and "tool_choice" not in kwargs
            return {"content": "ok"}

    class Client:
        class Chat:
            completions = Completions()
        chat = Chat()

    client = ModelClient("test", client=Client(), timeout=3.5)
    assert client.complete([], []).content == "ok"


def test_model_client_retries_missing_function_without_executing_it():
    from types import SimpleNamespace as NS

    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return NS(choices=[NS(message=NS(content=None, tool_calls=[NS(function=None)]))])
        return {"content": "", "tool_calls": [{"id": "valid", "function": {
            "name": "read", "arguments": '{"path": "report.md"}'}}]}

    client = ModelClient("test", client=NS(chat=NS(completions=NS(create=create))))
    messages = [{"role": "user", "content": "Read the report"}]
    result = client.complete(messages, [])
    assert result.tool_calls[0].arguments == {"path": "report.md"}
    assert len(requests) == 2
    assert len(messages) == 1
    assert "not executed" in requests[1]["messages"][-1]["content"]


def test_model_client_bounds_invalid_function_retries():
    import pytest
    from types import SimpleNamespace as NS
    from paper_agent.agent.model_client import InvalidToolCall

    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return {"tool_calls": [{"function": None}]}

    client = ModelClient("test", client=NS(chat=NS(completions=NS(create=create))))
    with pytest.raises(InvalidToolCall, match="function name"):
        client.complete([], [])
    assert len(requests) == 3


def test_experiment_context_discovers_project_templates(tmp_path):
    import json

    agent = PhaseAgent(tmp_path, model_client=FakeModel(), phase="experiment_bridge")
    context = agent._experiment_template_context()
    payload = json.loads(context.split("context:\n", 1)[1])
    assert payload["candidate_directories"] == []

    experiments = tmp_path / "experiments"
    for name in ("BatteryLife", "bamboo_mixer", "custom_template", ".cache", "__pycache__"):
        (experiments / name).mkdir(parents=True)
    (experiments / "manifest.json").write_text("{}")
    payload = json.loads(agent._experiment_template_context().split("context:\n", 1)[1])
    assert {Path(path).name for path in payload["candidate_directories"]} == {
        "BatteryLife", "bamboo_mixer", "custom_template"}
    assert all(Path(path).is_absolute() for path in payload["candidate_directories"])

    agent.phase = "literature"
    assert agent._experiment_template_context() == ""


class FakeCompletionStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def stream_chunk(content=None, calls=None, finish=None):
    from types import SimpleNamespace as NS
    return NS(choices=[NS(index=0, delta=NS(content=content, tool_calls=calls), finish_reason=finish)])


def test_streaming_assembles_text_and_parallel_tool_arguments():
    from types import SimpleNamespace as NS
    def delta(index, identifier=None, name=None, arguments=None):
        return NS(index=index, id=identifier, function=NS(name=name, arguments=arguments))
    stream = FakeCompletionStream([
        stream_chunk('Read ', [delta(0, 'a', 'read', '{"path":'), delta(1, 'b', 'find', '{"pattern":')]),
        stream_chunk('files', [delta(1, arguments='"*.md"}'), delta(0, arguments='"report.md"}')]),
        stream_chunk(finish='tool_calls'), NS(choices=[])])
    def create(**kwargs):
        assert kwargs['stream'] is True
        return stream
    client = ModelClient('test', client=NS(chat=NS(completions=NS(create=create))))
    result = client.complete([], [])
    assert result.content == 'Read files'
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ('a', 'read', {'path': 'report.md'}), ('b', 'find', {'pattern': '*.md'})]
    assert stream.closed


def test_streaming_text_completion():
    from paper_agent.agent.model_client import _stream_response
    stream = FakeCompletionStream([stream_chunk('Hello'), stream_chunk(' world', finish='stop')])
    assert _stream_response(stream).content == 'Hello world'
    assert stream.closed


def test_streaming_rejects_missing_finish_marker_and_token_limit():
    import pytest
    from paper_agent.agent.model_client import _stream_response
    for chunks in [[stream_chunk('partial')], [stream_chunk('partial', finish='length')]]:
        stream = FakeCompletionStream(chunks)
        with pytest.raises(RuntimeError, match='stream'):
            _stream_response(stream)
        assert stream.closed


def test_streaming_can_be_disabled(monkeypatch):
    from types import SimpleNamespace as NS
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'false')
    def create(**kwargs):
        assert kwargs['stream'] is False
        return {'content': 'ok'}
    assert ModelClient('test', client=NS(chat=NS(completions=NS(create=create)))).complete([], []).content == 'ok'


def test_output_limit_retries_without_exposing_partial_calls(monkeypatch):
    from types import SimpleNamespace as NS
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'true')
    calls = []
    streams = []
    messages = [{'role': 'user', 'content': 'Write report'}]

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            partial = NS(index=0, id='partial', function=NS(name='write', arguments='{"path":'))
            stream = FakeCompletionStream([stream_chunk(calls=[partial], finish='length')])
        else:
            assert 'discarded' in kwargs['messages'][-1]['content']
            assert kwargs['messages'][:-1] == messages
            stream = FakeCompletionStream([stream_chunk('Short reply', finish='stop')])
        streams.append(stream)
        return stream

    response = ModelClient('test', client=NS(chat=NS(completions=NS(create=create)))).complete(messages, [])
    assert response.content == 'Short reply'
    assert response.tool_calls == ()
    assert len(calls) == 2
    assert all(stream.closed for stream in streams)
    assert messages == [{'role': 'user', 'content': 'Write report'}]


def test_nonstream_output_limit_has_bounded_retries(monkeypatch):
    import pytest
    from types import SimpleNamespace as NS
    from paper_agent.agent.model_client import OutputLimitExceeded
    monkeypatch.setenv('PAPER_AGENT_STREAM', 'false')
    monkeypatch.setenv('PAPER_AGENT_MAX_TOKENS', '32768')
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs['max_tokens'] == 32768
        return NS(choices=[NS(finish_reason='length', message=NS(content='partial', tool_calls=[]))])

    with pytest.raises(OutputLimitExceeded, match='after 3 attempts'):
        ModelClient('test', client=NS(chat=NS(completions=NS(create=create)))).complete([], [])
    assert len(calls) == 3
