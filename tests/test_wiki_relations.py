import json

import pytest

from paper_agent.agent.model_client import ModelResponse
from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.wiki_relations import discover_relations, write_relations, ORIGIN
from paper_agent.tools import research_wiki
from test_wiki_agent import analysis, make_pdf


def seed(root):
    research_wiki.init_wiki(str(root))
    old = research_wiki.ingest_paper(str(root), title="Graph Forecasting",
                                     authors=["B Jones"], year=2024)
    research_wiki.update_paper_sections(str(root), old.stem, {
        "Method": "Graph message passing models interactions [p. 2].",
        "Problem / Gap": "Forecast interacting generic time series [p. 1]."})
    return old


def relation(source, target, kind="may_apply_to"):
    return {"from": source, "to": target, "type": kind,
            "source_quote": "Voltage can estimate state [p. 1]",
            "target_quote": "Graph message passing models interactions [p. 2].",
            "reason": "Transfer hypothesis: augment voltage-state estimation with a graph "
                      "of interacting cells; requires measured cell interactions and new "
                      "battery experiments. Evidence is author-reported, not independently verified."}


def edges(root):
    return [json.loads(line) for line in (root / "graph/edges.jsonl").read_text().splitlines()]


def test_ingest_cross_field_relations_and_per_node_attributes(tmp_path, monkeypatch):
    make_pdf(tmp_path, monkeypatch)
    root = tmp_path / "research-wiki"
    old = seed(root)
    old_id = f"paper:{old.stem}"
    old_idea = research_wiki.upsert_idea(
        str(root), f"{old.stem}-paper-idea", "Graph interactions",
        thesis="Graph message passing models interactions [p. 2].", based_on=[old_id])
    old_idea_id = f"idea:{old_idea.stem}"
    research_wiki.update_node_classification(old, {
        "scenario": [], "task": ["时间序列预测"], "research_field": ["机器学习", "通用时间序列"], "method": ["图神经网络"], "data_type": ["图"]})
    draft = analysis()
    draft["metadata"].update(research_field=["电池", " 电芯 ", "电芯"],
                             method=["图神经网络"], data_type=["时间序列"])
    draft["sections"]["Method"] = "Graph message passing models interactions [p. 2]."
    draft["idea"].update(research_field=["电池"], method=["图神经网络"])

    class Model:
        calls = 0
        empty = False

        def complete(self, messages, tools):
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            if "documents" not in payload:
                assert payload["existing_classification_labels"]["method"] == ["图神经网络"]
                return ModelResponse(content=json.dumps(draft))
            assert old_id in payload["documents"]
            assert old_idea_id in payload["documents"]
            assert "Forecast interacting generic time series" in payload["documents"][old_id]
            idea_edge = relation(payload["current_idea"], old_id)
            paper_edge = relation(payload["current_paper"], old_id, "shares_method")
            paper_edge["source_quote"] = "Graph message passing models interactions [p. 2]."
            reverse_transfer = relation(old_idea_id, payload["current_paper"])
            reverse_transfer.update(source_quote="Graph message passing models interactions [p. 2].",
                                    target_quote="Voltage features [p. 2]")
            return ModelResponse(content=json.dumps({"relationships": [] if self.empty
                                                      else [idea_edge, paper_edge, reverse_transfer]}))

    model = Model()
    agent = WikiAgent(tmp_path, model_client=model)
    result = agent.go("Read", paper_path="papers/battery.pdf")
    assert model.calls == 2 and result.relationship_count == 3
    automatic = [e for e in edges(root) if e.get("origin") == ORIGIN]
    assert {e["type"] for e in automatic} == {"shares_method", "may_apply_to"}
    assert all(old.name in e["evidence"] or old_idea.name in e["evidence"] for e in automatic)
    assert any(e["from"] == old_idea_id and e["to"] == f"paper:{result.slug}" for e in automatic)
    assert "Transfer hypothesis (unverified)" in automatic[0]["evidence"]
    paper = (tmp_path / result.wiki_path).read_text()
    assert 'research_field: ["电池", "电芯"]' in paper
    assert "research_object:" not in paper
    assert 'method: ["图神经网络"]' in paper
    # Experiment labels are not copied from the paper.
    assert 'method: []' in (tmp_path / result.experiment_paths[0]).read_text()
    assert not list((root / "claims").glob("*.md"))
    research_wiki.add_edge(str(root), old_id, f"paper:{result.slug}", "extends", "Manual edge")
    model.empty = True
    agent.go("Read", paper_path="papers/battery.pdf")
    assert not [e for e in edges(root) if e.get("origin") == ORIGIN]
    assert any(e["evidence"] == "Manual edge" for e in edges(root))
    assert "shares_method" not in (root / "query_pack.md").read_text()


@pytest.mark.parametrize("change", [
    {"to": "paper:missing"}, {"source_quote": "Fabricated excerpt"},
    {"target_quote": "title:"}, {"type": "supports"}, {"reason": ""},
    {"from": "claim:invented"},
])
def test_invalid_relation_is_rejected_without_changing_graph(tmp_path, change):
    root = tmp_path / "research-wiki"
    old = seed(root)
    current = research_wiki.ingest_paper(str(root), title="Voltage State",
                                         authors=["A Smith"], year=2025)
    idea = research_wiki.upsert_idea(str(root), "voltage", "Voltage",
                                    thesis="Voltage can estimate state [p. 1]")
    original = (root / "graph/edges.jsonl").read_text()
    edge = relation("idea:voltage", f"paper:{old.stem}") | change
    with pytest.raises(ValueError):
        discover_relations(root, current, idea, lambda *_: {"relationships": [edge]})
    assert (root / "graph/edges.jsonl").read_text() == original


def test_shortlist_reads_selected_notes_and_rejects_unknown_ids(tmp_path):
    root = tmp_path / "research-wiki"
    current = seed(root)
    idea = research_wiki.upsert_idea(str(root), "voltage", "Voltage", thesis="Evidence")
    others = [research_wiki.ingest_paper(str(root), title=f"Forecast Study {n}",
                                        authors=["Author"], year=2000 + n)
              for n in range(10)]
    selected = f"paper:{others[-1].stem}"
    calls = []

    def analyze(prompt, payload):
        data = json.loads(payload)
        calls.append(data)
        if "existing" in data:
            return {"candidates": [selected]}
        assert set(data["documents"]) == {f"paper:{current.stem}", "idea:voltage", selected}
        return {"relationships": []}

    assert discover_relations(root, current, idea, analyze) == []
    assert len(calls) == 2
    with pytest.raises(ValueError, match="candidate IDs"):
        discover_relations(root, current, idea, lambda *_: {"candidates": ["paper:missing"]})


def test_relation_refresh_preserves_other_owners_and_deduplicates_manual_edges(tmp_path):
    root = tmp_path / "research-wiki"
    seed(root)
    first = {"from": "paper:a", "to": "paper:b", "type": "shares_method", "evidence": "First"}
    second = first | {"to": "paper:c"}
    write_relations(root, "paper:a", [first])
    write_relations(root, "paper:c", [second])
    write_relations(root, "paper:a", [])
    assert len(edges(root)) == 1 and edges(root)[0]["owner"] == "paper:c"
    research_wiki.add_edge(str(root), "paper:a", "paper:b", "shares_method", "Manual")
    write_relations(root, "paper:a", [first])
    assert len(edges(root)) == 2
    assert any(e["evidence"] == "Manual" and "origin" not in e for e in edges(root))
