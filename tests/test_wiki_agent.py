import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from paper_agent.agent.model_client import ModelResponse
from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.wiki_quality import validate_entities
from paper_agent.tools import ToolContext, research_wiki
from paper_agent.tools.literature import read_paper_pages


def make_pdf(tmp_path, monkeypatch, texts=("Introduction", "Methods", "Results")):
    source = tmp_path / "papers" / "battery.pdf"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"%PDF-1.4")
    def read(project, path):
        assert path == source
        from paper_agent.tools.paper_preprocessing import document_from_structured
        import hashlib
        doc = document_from_structured({"pages": [{"page_idx": i, "blocks": [
            {"type": "text", "content": text}]} for i, text in enumerate(texts)]},
            pdf_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        return {"status": "completed", "figures": [], "pages": doc["pages"], "document": doc}
    monkeypatch.setattr("paper_agent.agent.wiki.read_mineru_paper", read)
    mock_independent_review(monkeypatch)
    return source


def mock_independent_review(monkeypatch):
    # Legacy publication tests isolate the independent reviewer. Full review and
    # repair failure paths are exercised separately in test_wiki_quality.py.
    original = WikiAgent._analyze
    def analyze(self, system, user):
        from paper_agent.agent.wiki_quality import REVIEW_PROMPT
        if system == REVIEW_PROMPT:
            draft = json.loads(user)["draft"]
            verdict = {"status": "supported", "reason": "Synthetic review"}
            return {"claims": [{"id": c["id"], **verdict} for c in draft["critical_claims"]],
                    "sections": {h: verdict for h in draft["sections"]}, "entities": verdict,
                    "coverage": {**verdict, "missing": []}, "issues": []}
        return original(self, system, user)
    monkeypatch.setattr(WikiAgent, "_analyze", analyze)


def analysis(experiments=None, datasets=None, sections=None):
    result = {
        "coined_terms": [],
        "references": [],
        "metadata": {"title": "Battery State Estimation", "authors": ["A Smith"],
                     "year": 2025, "doi": "10.1234/battery.2025",
                     "research_field": [], "method": [], "data_type": [], "scenario": ["实验室", "退役电池"], "task": ["soh", "rul"]},
        "sections": sections or {
            "Abstract": "Introduction",
            "One-line thesis": "State estimation [p. 1]",
            "Dataset and Features": "Voltage features [p. 2]",
        },
        "datasets": datasets if datasets is not None else [
            {"name": "MATR", "evidence": "Training set [p. 2]", "catalog_id": "MATR"},
            {"name": "MATR", "evidence": "Same training set [p. 3]", "catalog_id": "MATR"},
            {"name": "UL", "evidence": "External test [p. 3]", "catalog_id": None},
        ],
        "idea": {"title": "Battery state estimation",
                 "research_field": [], "method": [], "data_type": [], "scenario": ["实验室"], "task": ["soh"],
                 "thesis": "Voltage can estimate state [p. 1]",
                 "structure": "```text\nvoltage -> encoder -> state\n```\nInput: voltage [B, T, C]; B=batch, T=time, C=features. "
                              "Temporal encoder -> regression head -> state [B, 1]. [p. 2]",
                 },
        "experiments": experiments if experiments is not None else [
            {"title": title, "question": "Does transfer improve prediction?",
             "hypothesis": "Transfer reduces error with limited labels",
             "research_field": [], "method": [], "data_type": [], "scenario": ["实验室"], "task": ["soh"],
             "protocol": "Evaluation on MATR and UL [p. 2]",
             "results": "Lower error; Table 1 [p. 3]"}
            for title in ("Overall evaluation", "Baseline comparison")
        ],
    }

    result["section_status"] = {h: {"status": "extracted", "reason": "Synthetic source",
        "origin": "author_reported", "evidence": [{"block_id": "p1-b0", "quote": "Introduction"}]}
        for h in result["sections"]}
    result["critical_claims"] = [{"id": "data", "kind": "data_evaluation",
        "section": "Dataset and Features", "statement": "Synthetic data claim",
        "evidence": [{"block_id": "p1-b0", "quote": "Introduction"}]}]
    result["critical_claims"].append({"id": "main", "kind": "main_result",
        "section": "One-line thesis", "statement": "Synthetic main contribution",
        "evidence": [{"block_id": "p1-b0", "quote": "Introduction"}]})
    return result


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
            assert "Select at most 3" in messages[0]["content"]
            assert "Start idea.structure with" in messages[0]["content"]
            assert "an ASCII diagram in a fenced text code block" in messages[0]["content"]
            assert "do not add PDF page citations" in messages[0]["content"]
            assert "cite PDF pages in evidence" not in messages[0]["content"]
            payload = json.loads(messages[1]["content"])
            assert "pages" not in payload  # Do not duplicate the complete source.
            assert payload["source_document"]["page_count"] == 3
            assert [(b["page"], b["text"]) for b in payload["source_document"]["blocks"]] == [
                (1, "Introduction"), (2, "Methods"), (3, "Results")]
            assert payload["dataset_catalog"]["path"] == str(catalog)
            assert "UL_PUR catalog entries" in payload["dataset_catalog"]["index"]
            assert payload["dataset_catalog"]["catalog_ids"] == ["MATR", "UL_PUR"]
            assert payload["dataset_catalog"]["source_notes"] == {
                "MATR": "Source record: example DOI"}
            draft = analysis()
            if self.calls > 1:
                draft["idea"]["structure"] = "```text\nvoltage -> encoder -> state\n```\nUpdated input/output and encoder description. [p. 2]"
            return ModelResponse(content=json.dumps(draft))

    model = Model()
    agent = WikiAgent(tmp_path, model_client=model)
    result = agent.go("Record this paper", paper_path="papers/battery.pdf")
    assert model.calls == 1
    assert result.page_count == 3
    assert result.steps == ["select_paper", "read_full_paper", "extract_architecture_figures", "verify_evidence", "validate_analysis",
                            "ingest_paper", "upsert_idea", "add_reported_experiments",
                            "verify_write"]
    paper = (tmp_path / result.wiki_path).read_text()
    for heading in ("Supported Conclusions", "Assumptions", "Reusable Ingredients",
                    "Open Questions", "Claims", "Relevance to This Project",
                    "Key Results", "Experiments", "Abstract (original)"):
        assert f"## {heading}\n" not in paper
    assert "## Abstract\nIntroduction\n" in paper
    assert "MATR (local catalog: MATR): Training set [p. 2]" in paper
    assert paper.count("MATR (local catalog: MATR)") == 1
    assert "UL: External test [p. 3]" in paper
    assert "UL (local catalog: UL_PUR)" not in paper
    assert len(result.experiment_paths) == 2
    idea_text = (tmp_path / result.idea_path).read_text()
    assert "## Evidence\n" not in idea_text
    assert "Evidence:" not in idea_text
    assert "## strcture\n" in idea_text
    assert analysis()["idea"]["structure"] in idea_text
    for relative in result.experiment_paths:
        text = (tmp_path / relative).read_text()
        header = text.split("\n---\n", 1)[0]
        for key in ("verdict", "confidence", "date", "hardware", "duration", "provenance", "research_object"):
            assert not any(line.startswith(key + ":") for line in header.splitlines())
        assert "**verdict:**" not in text
        assert "Lower error; Table 1 [p. 3]" in text
        assert [line for line in text.splitlines() if line.startswith("## ")] == [
            "## Question", "## Hypothesis", "## Protocol", "## Results"]
    assert all((tmp_path / path).is_file() for path in result.experiment_paths)
    edges = [json.loads(line) for line in
             (tmp_path / "research-wiki" / "graph" / "edges.jsonl").read_text().splitlines()]
    assert len(edges) == 3
    again = agent.go("Record this paper", paper_path="papers/battery.pdf")
    assert model.calls == 2
    assert "Updated input/output and encoder description." in (
        tmp_path / again.idea_path).read_text()
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


@pytest.mark.parametrize("structure", [None, "", "   ", []])
def test_wiki_agent_requires_idea_structure_before_writing(tmp_path, monkeypatch, structure):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis()
    if structure is None:
        draft["idea"].pop("structure")
    else:
        draft["idea"]["structure"] = structure

    class Model:
        def complete(self, *args):
            return ModelResponse(content=json.dumps(draft))

    with pytest.raises(ValueError, match="idea.structure"):
        WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    assert not (tmp_path / "research-wiki").exists()


@pytest.mark.parametrize("invalid_sections", [
    {"Methods": "x"},
    {"One-line thesis": "Old thesis", "Dataset and Features": "Old data",
     "Experiments": "Old experiments", "Key Results": "Old results"},
])
def test_wiki_agent_corrects_sections_without_rereading_pdf(tmp_path, monkeypatch,
                                                           invalid_sections):
    make_pdf(tmp_path, monkeypatch)

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=json.dumps(analysis(sections=invalid_sections)))
            payload = json.loads(messages[1]["content"])
            assert "issues" in payload
            assert "do not add PDF page citations" in messages[0]["content"]
            assert "pages" not in payload
            assert payload["source_document"]["blocks"][0]["text"] == "Introduction"
            return ModelResponse(content=json.dumps(analysis()))

    model = Model()
    result = WikiAgent(tmp_path, model_client=model).go("Read", paper_path="papers/battery.pdf")
    assert model.calls == 2
    assert "Voltage features" in (tmp_path / result.wiki_path).read_text()


def test_wiki_agent_consolidates_excessive_experiment_nodes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    many = analysis(experiments=[analysis()["experiments"][0] | {"title": f"Figure {n}"}
                                 for n in range(4)])

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=json.dumps(many))
            assert "Select at most 3" in messages[0]["content"]
            assert "do not re-extract or return them" in messages[0]["content"]
            assert "do not add PDF page citations" in messages[0]["content"]
            # A targeted repair returns only fields the workflow can actually replace.
            corrected = analysis()
            corrected = {key: corrected[key] for key in (
                "sections", "section_status", "critical_claims", "idea", "datasets", "experiments"
            )}
            return ModelResponse(content=json.dumps(corrected))

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
        assert "Lower error; Table 1 [p. 3]" in text
        assert [line for line in text.splitlines() if line.startswith("## ")] == [
            "## Question", "## Hypothesis", "## Protocol", "## Results"]


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
                                        {"Abstract": "The complete original abstract."})
    page = path.read_text()
    assert "A method [p. 2]" in page and "The complete original abstract." in page
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


def test_paper_section_update_migrates_only_the_updated_page(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    page = research_wiki.ingest_paper(str(root), title="Legacy Paper",
                                     authors=["A Smith"], year=2024)
    legacy = page.read_text().replace("## Abstract\n", "## Abstract (original)\n")
    legacy += "\n## Key Results\nOld results\n\n## Experiments\nOld experiments\n"
    page.write_text(legacy)
    archived = page.with_name("untouched.md")
    archived.write_text(legacy)
    abstract = "We introduce FancyNet. It reduces error by 12%.\nAll results use held-out data."
    research_wiki.update_paper_sections(str(root), page.stem, {"Abstract": abstract})
    updated = page.read_text()
    assert f"## Abstract\n{abstract}\n" in updated
    assert all(f"## {heading}\n" not in updated
               for heading in ("Key Results", "Experiments", "Abstract (original)"))
    assert archived.read_text() == legacy
    for heading in ("Key Results", "Experiments"):
        with pytest.raises(ValueError, match="Invalid sections"):
            research_wiki.update_paper_sections(str(root), page.stem, {heading: "Obsolete"})


def test_legacy_idea_refresh_preserves_notes_and_adds_structure_before_connections(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    page = research_wiki.upsert_idea(str(root), "legacy", "Legacy idea", thesis="Old thesis",
                                    description="Source hypothesis.\n\nEvidence: Old evidence [p. 1]")
    before, after = page.read_text().split("## strcture\n", 1)
    page.write_text(before + "## Connections\n" + after.split("## Connections\n", 1)[1]
                    + "\nUser annotation preserved.\n")
    header = page.read_text().split("\n---\n", 1)[0]
    for structure in ("Inputs -> encoder -> outputs", "Updated tensor shapes"):
        research_wiki.update_idea_sections(str(root), "legacy", {
            "Thesis": "Expanded hypothesis", "strcture": structure,
        })
        text = page.read_text()
        assert "User annotation preserved." in text and "Source hypothesis." in text
        assert text.split("\n---\n", 1)[0] == header
        assert "## Evidence\n" not in text and "Old evidence" not in text
        assert text.index("## strcture\n") < text.index("## Connections\n")
        assert f"## strcture\n{structure}\n" in text


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


@pytest.mark.parametrize("count", [0, 1, 3, 4])
def test_experiment_selection_enforces_three_node_limit(count):
    candidates = [analysis()["experiments"][0] | {"title": f"Test {n}"}
                  for n in range(count)]
    draft = analysis(experiments=candidates)
    if count > 3:
        with pytest.raises(ValueError, match="more than 3"):
            validate_entities(draft)
    else:
        assert len(validate_entities(draft)[1]) == count


@pytest.mark.parametrize("field", ["question", "hypothesis", "protocol", "results"])
def test_experiment_requires_each_body_section(field):
    draft = analysis()
    draft["experiments"][0][field] = " "
    with pytest.raises(ValueError, match=f"Experiment 1 missing {field}"):
        validate_entities(draft)


def test_ingest_rebuilds_once_and_refreshes_experiments_preserving_notes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis(datasets=[])
    calls = Counter()
    writes = Counter()
    original_write = Path.write_text

    def write(path, *args, **kwargs):
        writes[path] += 1
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    original_rebuild = research_wiki.rebuild_index

    def rebuild(*args, **kwargs):
        calls["rebuild_index"] += 1
        return original_rebuild(*args, **kwargs)

    monkeypatch.setattr(research_wiki, "rebuild_index", rebuild)

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(draft))

    agent = WikiAgent(tmp_path, model_client=Model())
    first = agent.go("Read", paper_path="papers/battery.pdf")
    assert calls == {"rebuild_index": 1}
    assert not (tmp_path / "research-wiki/query_pack.md").exists()
    assert writes[tmp_path / first.idea_path] == 2  # creation + independent classification
    experiment = tmp_path / first.experiment_paths[0]
    experiment.write_text(experiment.read_text().replace(
        "tags: [", "reviewed_by: human\ntags: [") + "\n## My notes\nKeep this annotation.\n")
    header = experiment.read_text().split("\n---\n", 1)[0]
    for key in ("question", "hypothesis", "protocol", "results"):
        draft["experiments"][0][key] = f"Revised {key} from source"
    calls.clear()
    second = agent.go("Read", paper_path="papers/battery.pdf")
    assert second.experiment_paths == first.experiment_paths
    assert calls == {"rebuild_index": 1}
    assert not (tmp_path / "research-wiki/query_pack.md").exists()
    content = experiment.read_text()
    assert content.split("\n---\n", 1)[0] == header
    assert "## My notes\nKeep this annotation." in content
    assert all(draft["experiments"][0][key] in content
               for key in ("question", "hypothesis", "protocol", "results"))
    assert "Lower error; Table 1" not in content


def test_source_and_published_quality_are_each_saved_once(tmp_path, monkeypatch):
    from paper_agent.agent import wiki, wiki_quality

    make_pdf(tmp_path, monkeypatch)
    writes = Counter()
    original = wiki.write_json

    def write(path, data):
        writes[path] += 1
        return original(path, data)

    monkeypatch.setattr(wiki, "write_json", write)
    monkeypatch.setattr(wiki_quality, "write_json", write)

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(analysis(datasets=[])))

    state = WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    draft_source, = (tmp_path / ".paper-agent/wiki-preprocessing").glob("*/source.json")
    assert writes[draft_source] == 1
    assert writes[tmp_path / state.quality_path] == 1
    assert json.loads((tmp_path / state.quality_path).read_text())["wiki_content_sha256"]


@pytest.mark.parametrize("problem", ["dataset", "classification", "coined_name"])
def test_preflight_repairs_mutable_errors_before_independent_review(tmp_path, monkeypatch, problem):
    from paper_agent.agent.wiki_prompts import GENERATION_PROMPT, REVIEW_PROMPT, REVISION_PROMPT

    make_pdf(tmp_path, monkeypatch)
    valid = analysis(datasets=[])
    broken = deepcopy(valid)
    if problem == "dataset":
        broken["datasets"] = [{"name": "UL", "evidence": "Source observation", "catalog_id": "missing"}]
    elif problem == "classification":
        broken["experiments"][0]["task"] = "soh"
    else:
        valid["coined_terms"] = broken["coined_terms"] = [{"name": "FancyNet", "description": "Voltage encoder"}]
        broken["idea"]["title"] = "FancyNet"
    stages = []
    original = WikiAgent._analyze

    def analyze(agent, system, user):
        stages.append(system)
        payload = json.loads(user)
        if system == GENERATION_PROMPT:
            return deepcopy(broken)
        if system == REVISION_PROMPT:
            assert payload["issues"]
            assert "dataset_catalog" in payload and "existing_classification_labels" in payload
            return deepcopy(valid)
        assert system == REVIEW_PROMPT
        assert payload["draft"]["datasets"] == []
        assert isinstance(payload["draft"]["experiments"][0]["task"], list)
        assert payload["draft"]["idea"]["title"] != "FancyNet"
        return original(agent, system, user)

    monkeypatch.setattr(WikiAgent, "_analyze", analyze)
    state = WikiAgent(tmp_path, model_client=object()).go("Read", paper_path="papers/battery.pdf")
    assert stages == [GENERATION_PROMPT, REVISION_PROMPT, REVIEW_PROMPT]
    assert (tmp_path / state.wiki_path).is_file()


def test_confirmed_arxiv_metadata_does_not_trigger_another_lookup(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis(datasets=[])
    draft["metadata"]["arxiv_id"] = "2401.12345"

    def unexpected_lookup(*args):
        pytest.fail("Already confirmed metadata must be reused")

    monkeypatch.setattr(research_wiki, "fetch_arxiv_metadata", unexpected_lookup)

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(draft))

    state = WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    assert "2401.12345" in (tmp_path / state.wiki_path).read_text()


def test_final_verification_detects_skipped_experiment_refresh(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis(datasets=[])

    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content=json.dumps(draft))

    agent = WikiAgent(tmp_path, model_client=Model())
    agent.go("Read", paper_path="papers/battery.pdf")
    draft["experiments"][0]["results"] = "New result that must be written"
    original = research_wiki.add_experiment

    def skip_existing(*args, **kwargs):
        return original(*args, **{**kwargs, "update_on_exist": False})

    monkeypatch.setattr(research_wiki, "add_experiment", skip_existing)
    with pytest.raises(RuntimeError, match="experiment write could not be verified"):
        agent.go("Read", paper_path="papers/battery.pdf")
