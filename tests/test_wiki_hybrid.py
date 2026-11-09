import json
from pathlib import Path

from paper_agent.tools.core import ToolContext
from paper_agent.tools.wiki_hybrid import expand_wiki_relations, search_wiki_hybrid


def write_page(root, folder, name, body):
    path = root / folder / (name + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_hybrid_channels_fuse_and_preserve_real_excerpt(tmp_path, monkeypatch):
    from paper_agent.tools import wiki_hybrid

    root = tmp_path / "wiki"
    first = write_page(
        root,
        "papers",
        "a",
        "# Alpha\n\n## Method\n历史循环记录必须存在。\n\n## Limitations\n未来负载作为已知输入。\n",
    )
    second = write_page(
        root, "papers", "b", "# Beta\n\n## Method\n没有过去的测量，也能开始预测。\n"
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))

    def dense(chunks, query):
        return (
            [
                (i, 0.9 if c.page.path == second else 0.5)
                for i, c in sorted(
                    enumerate(chunks), key=lambda pair: pair[1].page.path != second
                )
            ],
            {"model": "test"},
        )

    monkeypatch.setattr(wiki_hybrid, "semantic_ranks", dense)
    result = search_wiki_hybrid(ToolContext(tmp_path), "历史循环记录", limit=2)
    assert not result.is_error
    data = json.loads(result.output)
    assert {m["wiki_path"] for m in data["matches"]} == {"papers/a.md", "papers/b.md"}
    channels = {
        channel
        for m in data["matches"]
        for hit in m["hits"]
        for channel in hit["channels"]
    }
    assert channels >= {"bm25", "e5"}
    for match in data["matches"]:
        for hit in match["hits"]:
            lines = (root / hit["wiki_path"]).read_text(encoding="utf-8").splitlines()
            source = "\n".join(lines[hit["excerpt_start_line"] - 1 :])
            assert source[hit["excerpt_start_char"] :].startswith(hit["excerpt"])


def test_exact_name_skips_semantic_and_missing_encoder_is_explicit(
    tmp_path, monkeypatch
):
    root = tmp_path / "wiki"
    write_page(
        root,
        "papers",
        "imoe",
        "# iMOE: prediction of battery trajectory\n\n## Method\nNeeds future load.\n",
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))
    monkeypatch.delenv("PAPER_AGENT_EMBEDDING_PYTHON", raising=False)
    exact = search_wiki_hybrid(ToolContext(tmp_path), "iMOE")
    assert json.loads(exact.output)["semantic_status"] == "skipped_exact"
    absent = search_wiki_hybrid(ToolContext(tmp_path), "unknown operating conditions")
    assert absent.is_error
    assert json.loads(absent.output)["semantic_status"] == "unavailable"
    lexical = search_wiki_hybrid(ToolContext(tmp_path), "future load", semantic=False)
    assert not lexical.is_error
    assert json.loads(lexical.output)["semantic_status"] == "explicitly_disabled"


def test_graph_preserves_direction_hypothesis_and_provenance(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    write_page(root, "papers", "a", "# Source\n## Method\nsource")
    write_page(root, "papers", "b", "# Target\n## Limitations\nnot validated")
    write_page(root, "papers", "isolated", "# Isolated\n## Method\nno edges")
    write_page(root, "ideas", "a-idea", "# Source idea\n## Method\nsource idea")
    graph = root / "graph/edges.jsonl"
    graph.parent.mkdir()
    edges = [
        {
            "from": "idea:a-idea",
            "to": "paper:a",
            "type": "inspired_by",
            "evidence": "source provenance",
        },
        {
            "from": "idea:a-idea",
            "to": "paper:b",
            "type": "may_apply_to",
            "evidence": "Unverified hypothesis",
        },
    ]
    graph.write_text(
        "\n".join(json.dumps(e) for e in edges) + '\n{"from":"missing"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))
    outward = json.loads(
        expand_wiki_relations(
            ToolContext(tmp_path), "papers/a.md", "may_apply_to", "out"
        ).output
    )
    hit = outward["neighbors"][0]
    assert hit["wiki_path"] == "papers/b.md"
    assert hit["from"] == "idea:a-idea" and hit["to"] == "paper:b"
    assert hit["status"] == "unverified_transfer_hypothesis"
    assert hit["via_source_idea"]["graph_line"] == 1 and hit["graph_line"] == 2
    assert outward["warnings"]
    assert hit["paper_read_paths"] == ["papers/b.md"]
    empty_type = json.loads(
        expand_wiki_relations(
            ToolContext(tmp_path), "papers/a.md", "shares_method"
        ).output
    )
    assert empty_type["neighbors"] == []
    assert empty_type["available_relations_at_start"]["may_apply_to"]["out"] == 1
    inward = json.loads(
        expand_wiki_relations(
            ToolContext(tmp_path), "papers/b.md", "may_apply_to", "in"
        ).output
    )
    assert inward["neighbors"][0]["traversal_direction"] == "in"
    assert inward["neighbors"][0]["paper_read_paths"] == ["papers/a.md"]
    isolated = json.loads(
        expand_wiki_relations(ToolContext(tmp_path), "papers/isolated.md").output
    )
    assert isolated["neighbors"] == []
    assert expand_wiki_relations(ToolContext(tmp_path), "../outside.md").is_error


def test_graph_limit_does_not_drop_total_or_invent_citations(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    for name in ("a", "b", "c"):
        write_page(root, "papers", name, f"# {name}\n## Method\nbody")
    graph = root / "graph/edges.jsonl"
    graph.parent.mkdir()
    graph.write_text(
        "\n".join(
            json.dumps(
                {
                    "from": "paper:a",
                    "to": f"paper:{name}",
                    "type": "shares_method",
                    "evidence": "recorded interpretation",
                }
            )
            for name in ("b", "c")
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))
    data = json.loads(
        expand_wiki_relations(ToolContext(tmp_path), "papers/a.md", limit=1).output
    )
    assert data["truncated"] and data["total"] == 2 and len(data["neighbors"]) == 1
    assert "cites" not in data["available_relation_types"]
