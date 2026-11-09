import json

import pytest

from paper_agent.agent.model_client import ModelResponse
from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.wiki_citations import (
    CITATION_ORIGIN, refresh_citations, save_references, validate_extraction,
)
from paper_agent.agent.wiki_relations import write_relations
from paper_agent.tools import research_wiki
from test_wiki_agent import analysis, make_pdf


QUOTE = "[1] Jones, B. Graph Forecasting. 2024. https://doi.org/10.1234/graph"


def reference(**changes):
    return {"label": "1", "title": "Graph Forecasting", "authors": ["Jones, B."],
            "year": 2024, "doi": "10.1234/graph", "arxiv_id": None,
            "pages": [3], "quote": QUOTE} | changes


def graph(root):
    return [json.loads(line) for line in (root / "graph/edges.jsonl").read_text().splitlines()]


def seed(root, title, doi="", author="B Jones", year=2024):
    return research_wiki.ingest_paper(str(root), title=title, authors=[author], year=year, doi=doi)


def test_agent_generic_descriptions_and_grounded_citations(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch, texts=("Introduction\nWe introduce FancyNet for battery state estimation.", "Methods", QUOTE))
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    old = seed(root, "Graph Forecasting", "https://doi.org/10.1234/graph")
    old_content = old.read_bytes()
    draft = analysis()
    draft["metadata"]["title"] = "FancyNet: Battery State Estimation"
    draft["coined_terms"] = [{"name": "FancyNet", "description": "电压驱动的状态估计网络"}]
    draft["sections"]["Abstract"] = "We introduce FancyNet for battery state estimation."
    draft["references"] = [reference()]

    class Model:
        def complete(self, messages, tools):
            data = json.loads(messages[1]["content"])
            if "documents" in data:
                return ModelResponse(content='{"relationships": []}')
            assert "never names or" in messages[0]["content"]
            return ModelResponse(content=json.dumps(draft))

    agent = WikiAgent(tmp_path, model_client=Model())
    result = agent.go("Read", paper_path="papers/battery.pdf")
    assert result.citation_count == 1
    citation, = [e for e in graph(root) if e["type"] == "cites"]
    assert (citation["from"], citation["to"]) == (f"paper:{result.slug}", f"paper:{old.stem}")
    assert citation["origin"] == CITATION_ORIGIN
    assert QUOTE in citation["evidence"] and "PDF pages [3]" in citation["evidence"]
    record = json.loads((root / "papers" / f"{result.slug}.references.json").read_text())
    assert record["coined_terms"] == draft["coined_terms"]
    assert "FancyNet" not in (tmp_path / result.idea_path).read_text()
    assert "## Abstract\nWe introduce FancyNet for battery state estimation." in (
        tmp_path / result.wiki_path).read_text()
    assert old.read_bytes() == old_content
    # A semantic refresh must not delete bibliographic edges.
    write_relations(root, f"paper:{result.slug}", [])
    assert citation in graph(root)
    # Rereading replaces only this paper's automatic citations and keeps manual ones.
    research_wiki.add_edge(str(root), f"paper:{result.slug}", f"paper:{old.stem}", "extends", "Manual")
    draft["references"] = []
    agent.go("Read", paper_path="papers/battery.pdf")
    assert not any(e["type"] == "cites" for e in graph(root))
    assert any(e["evidence"] == "Manual" for e in graph(root))


@pytest.mark.parametrize("location", ["section", "idea", "structure", "experiment", "classification"])
def test_coined_names_in_prose_rejected_before_wiki_writes(tmp_path, monkeypatch, location):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis()
    draft["coined_terms"] = [{"name": "AMDP", "description": "物理特征引导的专家路由"}]
    if location == "section":
        draft["sections"]["Method"] = "AMDP模块完成路由"
        draft["section_status"]["Method"] = draft["section_status"]["Dataset and Features"]
        draft["critical_claims"].append({"id": "input", "kind": "input_condition",
            "section": "Method", "statement": "Synthetic method",
            "evidence": [{"block_id": "p1-b0", "quote": "Introduction"}]})
    elif location == "idea":
        draft["idea"]["title"] = "使用AMDP预测状态"
    elif location == "structure":
        draft["idea"]["structure"] = "```text\nInput -> AMDP -> output\n```"
    elif location == "experiment":
        draft["experiments"][0]["design"] = "Evaluate AMDP"
    else:
        draft["metadata"]["method"] = ["AMDP"]

    class Model:
        def complete(self, *args):
            return ModelResponse(content=json.dumps(draft))

    with pytest.raises(ValueError, match="Author-coined name"):
        WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path="papers/battery.pdf")
    assert not (tmp_path / "research-wiki").exists()


@pytest.mark.parametrize("changes", [
    {"quote": "Invented bibliography entry"}, {"pages": [99]}, {"pages": [True]},
    {"doi": "10.1234/invented"}, {"arxiv_id": "2501.12345"},
    {"doi": "10.1234/grap"},
    {"title": "A different title"}, {"year": 2023}, {"authors": ["Someone Else"]},
])
def test_ungrounded_references_are_rejected(changes):
    draft = analysis() | {"references": [reference(**changes)]}
    with pytest.raises(ValueError):
        validate_extraction(draft, [{"page": 3, "text": QUOTE}], draft["sections"])


def test_unresolved_references_resolve_on_arrival_without_rewriting_notes(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    source = seed(root, "Source Paper", "10.1234/source")
    original = source.read_bytes()
    save_references(source, [], [reference()])
    assert refresh_citations(root)[f"paper:{source.stem}"] == 0
    assert len(list((root / "papers").glob("*.md"))) == 1
    target = seed(root, "Graph Forecasting", "10.1234/graph")
    assert refresh_citations(root)[f"paper:{source.stem}"] == 1
    assert graph(root)[0]["to"] == f"paper:{target.stem}"
    assert source.read_bytes() == original
    target.unlink()
    refresh_citations(root)
    assert graph(root) == []


def test_title_author_year_matching_conflicts_and_ambiguity(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    source = seed(root, "Source Paper")
    target = seed(root, "Graph Forecasting", "10.1234/graph")
    save_references(source, [], [reference(doi=None)])
    assert refresh_citations(root)[f"paper:{source.stem}"] == 1
    duplicate = target.with_name("duplicate.md")
    duplicate.write_text(target.read_text().replace("10.1234/graph", "10.1234/other"))
    assert refresh_citations(root)[f"paper:{source.stem}"] == 0
    # Exact identifiers disambiguate identical titles, but conflicting identifiers do not.
    save_references(source, [], [reference()])
    assert refresh_citations(root)[f"paper:{source.stem}"] == 1
    assert graph(root)[0]["to"] == f"paper:{target.stem}"
    save_references(source, [], [reference(doi="10.1234/wrong")])
    duplicate.unlink()
    assert refresh_citations(root)[f"paper:{source.stem}"] == 0


def test_cross_page_reference_and_arxiv_versions(tmp_path):
    quote = "[1] Jones, B. Graph Forecasting.\n2024. arXiv:2401.12345v2"
    draft = analysis() | {"references": [reference(doi=None, arxiv_id="2401.12345v2",
                                                  quote=quote, pages=[3, 4])]}
    terms, references = validate_extraction(draft, [
        {"page": 3, "text": quote.split("\n")[0]},
        {"page": 4, "text": quote.split("\n")[1]}], draft["sections"])
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    source = seed(root, "Source Paper")
    research_wiki.ingest_paper(str(root), title="Graph Forecasting", authors=["B Jones"], year=2024,
                              arxiv_id="2401.12345v1", prefetched_meta={})
    save_references(source, terms, references)
    assert refresh_citations(root)[f"paper:{source.stem}"] == 1


def test_manual_citation_is_preserved_and_automatic_edges_are_deduplicated(tmp_path):
    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    source = seed(root, "Source Paper")
    target = seed(root, "Graph Forecasting", "10.1234/graph")
    research_wiki.add_edge(str(root), f"paper:{source.stem}", f"paper:{target.stem}",
                          "cites", "Hand-checked reference")
    manual = graph(root)[0]
    save_references(source, [], [reference(), reference(label="2")])
    refresh_citations(root)
    assert graph(root) == [manual]
    save_references(source, [], [])
    refresh_citations(root)
    assert graph(root) == [manual]


def test_all_bibliography_owners_share_one_graph_commit(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "research-wiki"
    research_wiki.init_wiki(str(root))
    target = seed(root, "Graph Forecasting", "10.1234/graph")
    sources = [seed(root, title) for title in ("First Source Paper", "Second Source Paper", "Third Source Paper")]
    for source in sources:
        save_references(source, [], [reference()])
    research_wiki.add_edge(str(root), "paper:manual", f"paper:{target.stem}", "cites", "Manual")
    write_relations(root, "paper:semantic", [{"from": "paper:semantic", "to": f"paper:{target.stem}",
                                             "type": "shares_method", "evidence": "Semantic"}])
    retained = graph(root)
    commits = []
    original = Path.replace

    def replace(path, destination):
        if Path(destination) == root / "graph/edges.jsonl":
            commits.append(destination)
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    counts = refresh_citations(root)
    assert len(commits) == 1
    assert all(counts[f"paper:{source.stem}"] == 1 for source in sources)
    assert all(edge in graph(root) for edge in retained)
    assert len([edge for edge in graph(root) if edge.get("origin") == CITATION_ORIGIN]) == 3
    for source in sources:
        save_references(source, [], [])
    commits.clear()
    refresh_citations(root)
    assert len(commits) == 1
    assert graph(root) == retained


def test_immutable_bibliography_error_stops_before_review(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    draft = analysis(datasets=[])
    draft["references"] = [reference()]
    calls = []

    def analyze(agent, system, user):
        calls.append(system)
        return draft

    monkeypatch.setattr(WikiAgent, "_analyze", analyze)
    with pytest.raises(ValueError, match="quote is not present"):
        WikiAgent(tmp_path, model_client=object()).go("Read", paper_path="papers/battery.pdf")
    assert len(calls) == 1
    assert not (tmp_path / "research-wiki").exists()
