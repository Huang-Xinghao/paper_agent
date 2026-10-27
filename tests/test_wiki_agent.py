import json

import pytest

from paper_agent.agent.model_client import ModelResponse
from paper_agent.agent.wiki import WikiAgent
from paper_agent.tools import ToolContext, research_wiki
from paper_agent.tools.literature import read_paper_pages


def make_pdf(tmp_path, monkeypatch, texts=("Introduction", "Methods", "Results")):
    source = tmp_path / "papers" / "battery.pdf"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"%PDF-1.4")
    def read(project, path):
        assert path == source
        return {"status": "completed", "figures": [],
                "pages": [{"page": i + 1, "text": text} for i, text in enumerate(texts)]}
    monkeypatch.setattr("paper_agent.agent.wiki.read_mineru_paper", read)
    return source


def analysis(experiments=None, datasets=None, sections=None):
    return {
        "metadata": {"title": "Battery State Estimation", "authors": ["A Smith"],
                     "year": 2025, "doi": "10.1234/battery.2025",
                     "research_field": [], "method": [], "data_type": [], "scenario": ["实验室", "退役电池"], "task": ["soh", "rul"]},
        "sections": sections or {
            "One-line thesis": "State estimation [p. 1]",
            "Dataset and Features": "Voltage features [p. 2]",
            "Experiments": "Overall and comparison [p. 3]",
        },
        "datasets": datasets if datasets is not None else [
            {"name": "MATR", "evidence": "Training set [p. 2]", "catalog_id": "MATR"},
            {"name": "MATR", "evidence": "Same training set [p. 3]", "catalog_id": "MATR"},
            {"name": "UL", "evidence": "External test [p. 3]", "catalog_id": None},
        ],
        "idea": {"title": "Battery state estimation",
                 "research_field": [], "method": [], "data_type": [], "scenario": ["实验室"], "task": ["soh"],
                 "thesis": "Voltage can estimate state [p. 1]",
                 "evidence": "Hypothesis [p. 1]"},
        "experiments": experiments if experiments is not None else [
            {"title": title, "design": "Evaluation design [p. 2]",
             "research_field": [], "method": [], "data_type": [], "scenario": ["实验室"], "task": ["soh"],
             "data": "MATR and UL [p. 2]", "result": "Lower error [p. 3]",
             "evidence": "Table 1 [p. 3]"}
            for title in ("Overall evaluation", "Baseline comparison")
        ],
    }


def test_mineru_failure_stops_before_analysis_and_wiki_writes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    def fail(*args):
        raise RuntimeError("MinerU paper parsing failed")
    monkeypatch.setattr("paper_agent.agent.wiki.read_mineru_paper", fail)
    with pytest.raises(RuntimeError, match="MinerU paper parsing failed"):
        WikiAgent(tmp_path, model_client=object()).go("Read", paper_path="papers/battery.pdf")
    assert not (tmp_path / "research-wiki").exists()


def test_wiki_agent_reads_full_pdf_once_and_deduplicates_datasets(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    catalog = tmp_path / "电池资源库" / "电芯" / "数据集"
    catalog.mkdir(parents=True)
    guides = catalog.parent / "共享指南"
    guides.mkdir()
    (guides / "数据集目录.md").write_text("MATR and UL_PUR catalog entries")
    (catalog / "MATR_README.md").write_text(
        "MATR\n\n## BatteryLife 数据集检索\n\nSource record: example DOI\n")
    (catalog / "UL_PUR_README.md").write_text("UL_PUR")

    class Model:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            assert tools == []
            assert "2 to 5 broad experimental directions" in messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            assert payload["pages"] == [
                {"page": 1, "text": "Introduction"},
                {"page": 2, "text": "Methods"},
                {"page": 3, "text": "Results"},
            ]
            assert payload["dataset_catalog"]["path"] == str(catalog)
            assert "UL_PUR catalog entries" in payload["dataset_catalog"]["index"]
            assert payload["dataset_catalog"]["catalog_ids"] == ["MATR", "UL_PUR"]
            assert payload["dataset_catalog"]["source_notes"] == {
                "MATR": "Source record: example DOI"}
            return ModelResponse(content=json.dumps(analysis()))

    model = Model()
    agent = WikiAgent(tmp_path, model_client=model)
    result = agent.go("Record this paper", paper_path="papers/battery.pdf")
    assert model.calls == 1
    assert result.page_count == 3
    assert result.steps == ["select_paper", "read_full_paper", "extract_architecture_figures", "validate_analysis",
                            "ingest_paper", "upsert_idea", "add_reported_experiments",
                            "verify_write"]
    paper = (tmp_path / result.wiki_path).read_text()
    for heading in ("Supported Conclusions", "Assumptions", "Reusable Ingredients",
                    "Open Questions", "Claims", "Relevance to This Project"):
        assert f"## {heading}\n" not in paper
    assert "MATR (local catalog: MATR): Training set [p. 2]" in paper
    assert paper.count("MATR (local catalog: MATR)") == 1
    assert "UL: External test [p. 3]" in paper
    assert "UL (local catalog: UL_PUR)" not in paper
    assert len(result.experiment_paths) == 2
    for relative in result.experiment_paths:
        text = (tmp_path / relative).read_text()
        header = text.split("\n---\n", 1)[0]
        for key in ("verdict", "confidence", "date", "hardware", "duration", "provenance", "research_object"):
            assert not any(line.startswith(key + ":") for line in header.splitlines())
        assert "**verdict:**" not in text
        assert "Evidence: Table 1 [p. 3]" in text
    assert all((tmp_path / path).is_file() for path in result.experiment_paths)
    edges = [json.loads(line) for line in
             (tmp_path / "research-wiki" / "graph" / "edges.jsonl").read_text().splitlines()]
    assert len(edges) == 3
    again = agent.go("Record this paper", paper_path="papers/battery.pdf")
    assert model.calls == 2
    assert again.experiment_paths == result.experiment_paths
    assert len(list((tmp_path / "research-wiki" / "papers").glob("*.md"))) == 1
    assert len((tmp_path / "research-wiki" / "graph" / "edges.jsonl").read_text().splitlines()) == 3


def test_wiki_agent_rejects_bad_analysis_before_writing(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(analysis() | {
                "metadata": {"title": "Missing authors"}}))

    with pytest.raises(ValueError, match="title, authors, or year"):
        WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    assert not (tmp_path / "research-wiki").exists()


def test_wiki_agent_corrects_sections_without_rereading_pdf(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=json.dumps(analysis(sections={"Methods": "x"})))
            payload = json.loads(messages[1]["content"])
            assert "schema_errors" in payload
            assert "pages" not in payload
            return ModelResponse(content=json.dumps({"sections": analysis()["sections"]}))

    model = Model()
    result = WikiAgent(tmp_path, model_client=model).go("Read", paper_path="papers/battery.pdf")
    assert model.calls == 2
    assert "Voltage features" in (tmp_path / result.wiki_path).read_text()


def test_wiki_agent_consolidates_excessive_experiment_nodes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    many = analysis(experiments=[analysis()["experiments"][0] | {"title": f"Figure {n}"}
                                 for n in range(16)])

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=json.dumps(many))
            assert "Group the paper's reported experiments" in messages[0]["content"]
            return ModelResponse(content=json.dumps({"experiments": analysis()["experiments"]}))

    model = Model()
    result = WikiAgent(tmp_path, model_client=model).go("Read", paper_path="papers/battery.pdf")
    assert model.calls == 2
    assert len(result.experiment_paths) == 2
    for relative in result.experiment_paths:
        text = (tmp_path / relative).read_text()
        header = text.split("\n---\n", 1)[0]
        for key in ("verdict", "confidence", "date", "hardware", "duration", "provenance", "research_object"):
            assert not any(line.startswith(key + ":") for line in header.splitlines())
        assert "**verdict:**" not in text
        assert "Evidence: Table 1 [p. 3]" in text


@pytest.mark.parametrize("invalid_dataset", [
    {"name": "UL", "evidence": "p. 2", "catalog_id": "missing"},
    {"name": "UL", "evidence": "p. 2", "catalog_id": ["UL_PUR"]},
])
def test_wiki_agent_rejects_invalid_catalog_match_before_writing(
        tmp_path, monkeypatch, invalid_dataset):
    make_pdf(tmp_path, monkeypatch)

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(analysis(datasets=[invalid_dataset])))

    with pytest.raises(ValueError, match="unknown catalog_id"):
        WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    assert not (tmp_path / "research-wiki").exists()


def test_wiki_agent_reads_all_pages_by_default_and_respects_explicit_limit(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch, texts=("One",) * 101)
    agent = WikiAgent(tmp_path, model_client=object())
    assert len(agent._read_full_paper(ToolContext(tmp_path), "papers/battery.pdf")["pages"]) == 101
    limited = WikiAgent(tmp_path, model_client=object(), max_pages=100)
    with pytest.raises(ValueError, match="max_pages=100"):
        limited._read_full_paper(ToolContext(tmp_path), "papers/battery.pdf")


def test_research_wiki_updates_preserve_metadata_and_other_sections(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    assert not (root / "gap_map.md").exists()
    path = research_wiki.ingest_paper(str(root), title="Paper One",
                                      authors=["A Smith"], year=2024)
    assert not (root / "gap_map.md").exists()
    slug = path.stem
    research_wiki.update_paper_sections(str(root), slug,
                                        {"Method": "A method [p. 2]"})
    research_wiki.update_paper_sections(str(root), slug,
                                        {"Experiments": "An experiment [p. 4]"})
    page = path.read_text()
    assert "A method [p. 2]" in page and "An experiment [p. 4]" in page
    assert "node_id: paper:" in page
    with pytest.raises(ValueError, match="Invalid sections"):
        research_wiki.update_paper_sections(str(root), slug, {"Unknown": "bad"})
    with pytest.raises(ValueError, match="invalid paper slug"):
        research_wiki.update_paper_sections(str(root), "../outside", {"Method": "bad"})
    original = path.read_text()
    repeated = research_wiki.ingest_paper(str(root), title="Paper One",
                                          authors=["A Smith"], year=2024)
    assert repeated == path
    assert path.read_text() == original


def test_read_paper_pages_returns_page_numbers_and_continuation(tmp_path, monkeypatch):
    source = tmp_path / "papers" / "source.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.4")

    class Page:
        def __init__(self, number):
            self.number = number

        def extract_text(self):
            return f"Evidence from page {self.number}"

    class Reader:
        def __init__(self, path):
            assert path == str(source)
            self.pages = [Page(number) for number in range(1, 4)]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    result = read_paper_pages(ToolContext(tmp_path), "papers/source.pdf", start_page=2,
                              page_count=1)
    payload = json.loads(result.output)
    assert payload["pages"] == [{"page": 2, "text": "Evidence from page 2"}]
    assert payload["next_page"] == 3
    assert read_paper_pages(ToolContext(tmp_path), "papers/source.pdf", start_page=4).is_error


def test_pdf_classification_is_per_node_and_updates_existing_nodes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis()
    draft['experiments'][1].update(scenario=['实车', '储能'], task=['degradation trajectory', 'soc', 'soc'])

    class Model:
        def complete(self, messages, tools):
            assert 'scenario and task arrays' in messages[0]['content']
            return ModelResponse(content=json.dumps(draft))

    agent = WikiAgent(tmp_path, model_client=Model())
    result = agent.go('Read', paper_path='papers/battery.pdf')

    def fields(relative):
        text = (tmp_path / relative).read_text()
        header = text.split('\n---\n', 1)[0]
        return {key: json.loads(next(line.split(': ', 1)[1] for line in header.splitlines()
                                    if line.startswith(key + ': ')))
                for key in ('scenario', 'task')}

    assert fields(result.wiki_path) == {'scenario': ['实验室', '退役电池'], 'task': ['soh', 'rul']}
    assert fields(result.idea_path) == {'scenario': ['实验室'], 'task': ['soh']}
    assert fields(result.experiment_paths[1]) == {
        'scenario': ['实车', '储能'], 'task': ['degradation trajectory', 'soc']}
    idea_path = tmp_path / result.idea_path
    idea_path.write_text(idea_path.read_text() + '\nUser annotation preserved.\n')
    draft['idea'].update(scenario=['退役电池'], task=[])
    again = agent.go('Read', paper_path='papers/battery.pdf')
    assert fields(again.idea_path) == {'scenario': ['退役电池'], 'task': []}
    assert 'User annotation preserved.' in idea_path.read_text()
    assert idea_path.read_text().count('\nscenario:') == 1


@pytest.mark.parametrize('target,key,value', [
    ('metadata', 'scenario', ['unknown']),
    ('idea', 'task', ['  ']),
    ('experiment', 'task', 'soh'),
    ('metadata', 'scenario', None),
    ('idea', 'task', [42]),
])
def test_invalid_classification_fails_before_any_write(tmp_path, monkeypatch, target, key, value):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis()
    node = draft['experiments'][0] if target == 'experiment' else draft[target]
    node[key] = value

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(draft))

    with pytest.raises(ValueError, match=key + ' must be an array'):
        WikiAgent(tmp_path, model_client=Model()).go('Read', paper_path='papers/battery.pdf')
    assert not (tmp_path / 'research-wiki').exists()


def test_zotero_old_user_id_variable_is_not_used(tmp_path, monkeypatch, capsys):
    import sys
    from paper_agent.agent import wiki
    monkeypatch.setattr(wiki, 'WikiAgent', lambda *args, **kwargs: object())
    monkeypatch.delenv('ZOTERO_LIBRARY_ID', raising=False)
    monkeypatch.setenv('ZOTERO_USER_ID', '12345')
    monkeypatch.setattr(sys, 'argv', ['paper-wiki-agent', '--project', str(tmp_path),
                                    '--zotero-item', 'ABC123'])
    with pytest.raises(SystemExit) as error:
        wiki.main()
    assert error.value.code == 2
    assert 'ZOTERO_LIBRARY_ID' in capsys.readouterr().err
