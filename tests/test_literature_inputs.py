import io
import json
from types import SimpleNamespace

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState
from paper_agent.tools import ToolContext, ToolResult, BUILTIN_TOOLS, tools_for_phase
from paper_agent.tools import literature
from paper_agent.agent import workflow


def result(value):
    return ToolResult(json.dumps(value))


class RecordingAgent(PhaseAgent):
    def __init__(self, project, calls):
        self.project = project
        self.calls = calls

    def go(self, prompt, *, phase, completion_check=None):
        self.calls.append(("agent", phase))
        self.prompt = prompt
        target = self.project / "literature/RESEARCH_REPORT.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text("Literature report")
        if completion_check:
            completion_check()
        return SimpleNamespace(final="Done", iterations=1)


def test_runner_collects_local_evidence_without_downloading(tmp_path, monkeypatch):
    calls = []

    def search(ctx, query, source="openalex", max_results=5):
        calls.append(("search", source, max_results))
        return result({"query": query, "papers": [
            {"title": "Battery", "doi": "10.1234/battery",
             "pdf_urls": ["https://repository.org/battery.pdf"],
             "url": "https://arxiv.org/abs/2401.12345"},
            {"title": "Extra", "url": "https://arxiv.org/abs/2401.54321"}]})

    def read(ctx, path, max_pages):
        calls.append(("read", path, max_pages))
        return result({"path": path, "text": "Evidence", "pages_read": max_pages, "page_count": 12})

    def unexpected_download(*args, **kwargs):
        raise AssertionError("The workflow must leave downloads to the model")

    monkeypatch.setattr(literature.urllib.request, "urlopen", unexpected_download)
    monkeypatch.setattr(workflow, "search_papers", search)
    monkeypatch.setattr(workflow, "discover_local_papers", lambda *a, **k: result(
        {"found": 1, "papers": [{"path": "papers/local.pdf"}]}))
    monkeypatch.setattr(workflow, "read_local_paper", read)
    agent = RecordingAgent(tmp_path, calls)
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="literature"))
    output = runner._run_literature(WorkflowState("test"), "Battery SOH")
    context = json.loads(agent.prompt.split("运行器收集的文献上下文：\n")[1])
    assert calls[-1] == ("agent", "literature")
    assert ("search", "openalex", 5) in calls
    assert ("read", "papers/local.pdf", 3) in calls
    assert "Battery SOH" in agent.prompt
    assert (tmp_path / "literature/RESEARCH_REPORT.md").read_text() == output.artifact
    assert [c for c in calls if c[0] == "search"] == [("search", "openalex", 5)]
    assert context["local_papers"]["status"] == "read"
    assert output.metadata["research_sources"]["local_papers"] == ["papers/local.pdf"]
    assert not (tmp_path / "papers").exists()


def test_pdf_failures_remain_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "discover_local_papers", lambda *a, **k: result(
        {"found": 1, "papers": [{"path": "papers/scanned.pdf"}]}))
    monkeypatch.setattr(workflow, "read_local_paper", lambda *a, **k: result({"text": ""}))
    runner = WorkflowRunner(RecordingAgent(tmp_path, []))
    context = runner._literature_inputs(ToolContext(tmp_path), "SOH")
    assert context["local_papers"]["status"] == "read_failed"
    assert "No extractable" in context["local_papers"]["errors"][0]


def test_arxiv_download_and_cache(tmp_path, monkeypatch):
    requests = []

    def fetch(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(b"%PDF-1.7\nmock payload")

    monkeypatch.setattr(literature.urllib.request, "urlopen", fetch)
    ctx = ToolContext(tmp_path)
    first = json.loads(literature.download_arxiv_paper(ctx, "http://arxiv.org/abs/2401.12345v2").output)
    second = json.loads(literature.download_arxiv_paper(ctx, "https://arxiv.org/pdf/2401.12345v2.pdf").output)
    assert first["status"] == "downloaded"
    assert second["status"] == "cached"
    assert len(requests) == 1
    assert (tmp_path / first["path"]).read_bytes().startswith(b"%PDF-")
    assert list((tmp_path / "papers/arxiv").iterdir()) == [tmp_path / first["path"]]


def test_arxiv_download_rejects_bad_response_and_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(literature.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"<html>error"))
    ctx = ToolContext(tmp_path)
    assert literature.download_arxiv_paper(ctx, "https://arxiv.org/abs/2401.12345").is_error
    assert not list((tmp_path / "papers/arxiv").iterdir())
    for url in ["https://example.com/abs/2401.12345", "https://arxiv.org/abs/../../evil"]:
        assert literature.download_arxiv_paper(ctx, url).is_error


def test_arxiv_atom_metadata_and_invalid_xml(tmp_path, monkeypatch):
    atom = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2401.12345</id><title>SOH &amp; RUL</title>
    <published>2024-01-01</published><summary>Battery prediction</summary>
    <author><name>A &amp; B</name></author></entry></feed>'''
    monkeypatch.setattr(literature, "_get_text", lambda url: atom)
    paper = json.loads(literature.search_papers(ToolContext(tmp_path), "battery", source="arxiv").output)["papers"][0]
    assert paper["title"] == "SOH & RUL"
    assert paper["authors"] == ["A & B"]
    monkeypatch.setattr(literature, "_get_text", lambda url: "not XML")
    assert literature.search_papers(ToolContext(tmp_path), "battery", source="arxiv").is_error


def test_literature_can_read_later_pages():
    available = {tool.name for tool in tools_for_phase("literature", BUILTIN_TOOLS)}
    assert {"read_paper_pages", "download_arxiv_paper", "download_openalex_paper"} <= available


def test_interrupted_download_leaves_no_cache(tmp_path, monkeypatch):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell() >= 5:
                raise OSError("connection interrupted")
            return super().read(size)

    monkeypatch.setattr(literature.urllib.request, "urlopen", lambda *a, **k: Interrupted(b"%PDF-1.7"))
    response = literature.download_arxiv_paper(ToolContext(tmp_path), "https://arxiv.org/abs/2401.12345")
    assert response.is_error
    assert not list((tmp_path / "papers/arxiv").iterdir())


def test_openalex_search_preserves_pdf_locations(tmp_path, monkeypatch):
    monkeypatch.setattr(literature, '_get_json', lambda url: {'results': [{
        'title': 'Battery health', 'doi': 'https://doi.org/10.1234/battery',
        'primary_location': None,
        'best_oa_location': {'pdf_url': 'https://repository.org/battery.pdf'},
        'locations': [{'pdf_url': 'https://repository.org/battery.pdf'},
                      {'pdf_url': 'https://publisher.org/battery.pdf'}]}]})
    paper = json.loads(literature.search_papers(ToolContext(tmp_path), 'battery').output)['papers'][0]
    assert paper['pdf_urls'] == ['https://repository.org/battery.pdf', 'https://publisher.org/battery.pdf']


def test_openalex_fallback_download_and_cache(tmp_path, monkeypatch):
    calls = []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(b'<html>blocked' if len(calls) == 1 else b'%PDF-1.7\ncontent')
    monkeypatch.setattr(literature.urllib.request, 'urlopen', fetch)
    ctx = ToolContext(tmp_path)
    paper = {'doi': '10.1234/battery', 'pdf_urls': ['https://a.org/p.pdf', 'https://b.org/p.pdf']}
    first = json.loads(literature.download_openalex_paper(ctx, paper).output)
    assert first['status'] == 'downloaded'
    assert first['url'] == 'https://b.org/p.pdf'
    assert len(first['attempt_errors']) == 1
    assert json.loads(literature.download_openalex_paper(ctx, paper).output)['status'] == 'cached'
    assert len(calls) == 2
    assert list((tmp_path / 'papers/openalex').iterdir()) == [tmp_path / first['path']]


def test_openalex_doi_lookup_and_unavailable(tmp_path, monkeypatch):
    calls = []
    def lookup(url):
        calls.append(url)
        return {'best_oa_location': {'pdf_url': 'https://repo.org/p.pdf'}}
    monkeypatch.setattr(literature, '_get_json', lookup)
    monkeypatch.setattr(literature.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'%PDF-1.7'))
    assert not literature.download_openalex_paper(ToolContext(tmp_path), {'doi': '10.1234/example'}).is_error
    assert '10.1234%2Fexample' in calls[0]
    monkeypatch.setattr(literature, '_get_json', lambda url: {})
    response = literature.download_openalex_paper(ToolContext(tmp_path), {'doi': '10.1234/missing'})
    assert response.is_error
    assert json.loads(response.output)['status'] == 'unavailable'


def test_openalex_interrupted_download_no_partial_cache(tmp_path, monkeypatch):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell() >= 5:
                raise OSError('connection interrupted')
            return super().read(size)
    monkeypatch.setattr(literature.urllib.request, 'urlopen', lambda *a, **k: Interrupted(b'%PDF-1.7'))
    response = literature.download_openalex_paper(ToolContext(tmp_path), {
        'pdf_urls': ['https://repo.org/p.pdf']})
    assert response.is_error
    assert not list((tmp_path / 'papers/openalex').iterdir())



def test_literature_does_not_save_completion_notice_as_report(tmp_path, monkeypatch):
    from paper_agent.agent.model_client import ModelResponse

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content='Done')

    agent = PhaseAgent(tmp_path, model_client=Model(), max_iterations=2)
    runner = WorkflowRunner(agent)
    monkeypatch.setattr(runner, '_research_context', lambda *args, **kwargs: {})
    state = runner.run('Research')
    assert state.status == 'failed'
    assert 'literature/RESEARCH_REPORT.md' in state.error
    assert not (tmp_path / 'literature/RESEARCH_REPORT.md').exists()
    assert agent.last_state.iterations == 2


def test_default_start_node_is_independent_of_agent_type(tmp_path):
    real = PhaseAgent(tmp_path, model_client=object())
    double = SimpleNamespace(project=tmp_path)
    assert WorkflowRunner(real).config.start_node == 'literature'
    assert WorkflowRunner(double).config.start_node == 'literature'
