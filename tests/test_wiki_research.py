"""Bounded retrieval contracts; fixtures are synthetic, not paper-quality claims."""

import json
from types import SimpleNamespace as NS

import pytest

from paper_agent.agent.model_client import (
    ModelClient,
    ModelResponse,
    ToolCall,
    limited_model_requests,
)
from paper_agent.agent.wiki_research import (
    collect_wiki_evidence,
    latest_evidence,
    evidence_handoff,
)
from paper_agent.agent.workflow import WorkflowRunner
from paper_agent.tools import ToolContext
from paper_agent.tools.wiki import read_wiki, search_wiki
from paper_agent.tools.wiki_hybrid import expand_wiki_relations


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    page = root / "papers/a.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Alpha\n## Method\nRelaxation predicts capacity.\n"
        "## Limitations / Failure Modes\nKnown future load is required.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))
    monkeypatch.delenv("PAPER_AGENT_EMBEDDING_DIR", raising=False)
    monkeypatch.delenv("PAPER_AGENT_EMBEDDING_PYTHON", raising=False)
    return root, page


def answer(**changes):
    return {
        "summary": "Capacity is different from lifetime; respect load conditions.",
        "assessment": "sufficient",
        "evidence": [
            {
                "claim": "Known load is an input constraint.",
                "role": "constraint",
                "wiki_path": "papers/a.md",
                "section": "Limitations / Failure Modes",
                "quote": "Known future load is required.",
            }
        ],
        "decisions": [
            {
                "wiki_path": "papers/a.md",
                "decision": "adapt",
                "reason": "Task has different output.",
            }
        ],
        "hypotheses": ["Transfer to lifetime needs validation."],
        "gaps": [],
        "next_action": "Human review",
        **changes,
    }


class Reader:
    def __init__(self, page=None):
        self.calls = 0
        self.page = page

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "read",
                        "read_wiki",
                        {
                            "path": str(self.page) if self.page else "papers/a.md",
                            "section": "Limitations / Failure Modes",
                        },
                    ),
                )
            )
        return ModelResponse(content=json.dumps(answer()))


def test_production_entry_uses_bounded_collector_and_keeps_versions(tmp_path, library):
    model = Reader()
    runner = WorkflowRunner(NS(project=tmp_path / "project", model=model))
    packet = runner._wiki_context("Relaxation capacity", stage="ideas")
    assert packet["status"] == "ready_for_review"
    assert packet["budget"]["model_calls"] == 2 and packet["budget"]["tool_calls"] == 2
    assert packet["evidence"][0]["start_line"] == 5
    assert packet["evidence"][0]["quality_records"][0]["status"] == "not_checked"
    assert (tmp_path / "project/.paper-agent/wiki-retrieval").is_dir()
    assert latest_evidence(tmp_path / "project")["evidence"]
    library[1].write_text(
        library[1].read_text() + "\nChanged version", encoding="utf-8"
    )
    reused = latest_evidence(tmp_path / "project")
    assert (
        reused["status"] == "stale"
        and not reused["evidence"]
        and not reused["decisions"]
    )
    assert "pages" not in reused


def test_hard_model_tool_graph_budgets(tmp_path, library):
    class Insistent:
        count = 0

        def complete(self, messages, tools):
            self.count += 1
            if self.count == 3:
                assert tools == []
                return ModelResponse(
                    content=json.dumps(
                        answer(evidence=[], decisions=[], assessment="insufficient")
                    )
                )
            names = ["expand_wiki_relations", "expand_wiki_relations"] + [
                "read_wiki"
            ] * 8
            return ModelResponse(
                tool_calls=tuple(
                    ToolCall(str(i), n, {"path": "papers/a.md"})
                    for i, n in enumerate(names)
                )
            )

    model = Insistent()
    p = collect_wiki_evidence(tmp_path / "p", model, "Relaxation")
    assert model.count == p["budget"]["model_calls"] == 3
    assert p["budget"]["tool_calls"] == 6 and p["budget"]["graph_calls"] == 1
    assert p["budget"]["delivered_chars"] <= 36000


def test_no_answer_and_isolated_graph(tmp_path, library):
    class Empty:
        def complete(self, messages, tools):
            return ModelResponse(
                content=json.dumps(
                    answer(
                        assessment="insufficient",
                        evidence=[],
                        decisions=[],
                        gaps=["No support"],
                    )
                )
            )

    p = collect_wiki_evidence(tmp_path / "p", Empty(), "zzqv987654321")
    assert p["status"] == "insufficient" and not p["evidence"]
    graph = json.loads(
        expand_wiki_relations(ToolContext(tmp_path), "papers/a.md").output
    )
    assert graph["neighbors"] == []
    assert json.loads(search_wiki(ToolContext(tmp_path), "Relaxation").output)[
        "matches"
    ]


def test_changed_page_rejected_even_for_absolute_read(tmp_path, library):
    root, page = library
    old = json.loads(read_wiki(ToolContext(tmp_path), "papers/a.md").output)[
        "content_sha256"
    ]

    class Updating(Reader):
        def complete(self, messages, tools):
            if not self.calls:
                page.write_text(page.read_text() + "\nNew version", encoding="utf-8")
            return super().complete(messages, tools)

    p = collect_wiki_evidence(tmp_path / "p", Updating(page), "Known future load")
    assert p["status"] == "insufficient" and not p["evidence"]
    assert any("changed" in e or "更新" in e for e in p["validation_errors"])
    assert (
        read_wiki(
            ToolContext(tmp_path), str(page), expected_content_sha256=old
        ).metadata["error_code"]
        == "WIKI_CONTENT_CHANGED"
    )


def test_semantic_failure_is_explicit(tmp_path, library, monkeypatch):
    from paper_agent.tools import wiki_hybrid

    monkeypatch.setenv("PAPER_AGENT_EMBEDDING_DIR", "missing")
    monkeypatch.setenv("PAPER_AGENT_EMBEDDING_PYTHON", "missing")
    monkeypatch.setattr(
        wiki_hybrid,
        "semantic_ranks",
        lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable encoder")),
    )
    data = json.loads(
        search_wiki(ToolContext(tmp_path), "Relaxation", strategy="auto").output
    )
    assert (
        data["degraded"]
        and data["warnings"]
        and data["semantic_status"] == "unavailable"
    )
    assert search_wiki(ToolContext(tmp_path), "Relaxation", strategy="hybrid").is_error


def test_isolated_graph_still_allows_text_retrieval(tmp_path, library):
    data = json.loads(
        expand_wiki_relations(ToolContext(tmp_path), "papers/a.md").output
    )
    assert not data["neighbors"]
    assert json.loads(search_wiki(ToolContext(tmp_path), "Known future load").output)[
        "matches"
    ]


@pytest.mark.parametrize(
    "role,canonical",
    [
        ("boundary", "constraint"),
        ("condition", "constraint"),
        ("counterexample", "counterevidence"),
    ],
)
def test_known_legacy_role_aliases_keep_audit_trace(tmp_path, library, role, canonical):
    from paper_agent.agent.wiki_research import validate_packet, observed_passages

    data = json.loads(read_wiki(ToolContext(tmp_path), "papers/a.md").output)
    raw = answer()
    raw["evidence"][0]["role"] = role
    evidence, errors = validate_packet(raw, observed_passages(data), library[0])
    assert not errors and evidence[0]["role"] == canonical
    assert evidence[0]["role_normalized_from"] == role
    assert raw["evidence"][0]["role"] == role


@pytest.mark.parametrize("invalid", [None, [None], "bad", [{"quote": "invented"}]])
def test_malformed_evidence_saved_as_failure(tmp_path, library, invalid):
    class Bad:
        def complete(self, messages, tools):
            return ModelResponse(
                content=json.dumps(answer(evidence=invalid, decisions=None, gaps=None))
            )

    packet = collect_wiki_evidence(tmp_path / "p", Bad(), "Relaxation")
    assert packet["status"] == "insufficient" and packet["validation_errors"]


def test_failed_model_attempt_is_recorded(tmp_path, library):
    class Failing:
        def complete(self, messages, tools):
            raise TimeoutError("Provider unavailable")

    packet = collect_wiki_evidence(tmp_path / "p", Failing(), "Relaxation")
    assert packet["budget"]["model_calls"] == 1
    assert packet["status"] == "insufficient"


def test_request_scope_prevents_network_retry_and_limits_output():
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise TimeoutError("failed")
        return {"content": "ok"}

    model = ModelClient(client=NS(chat=NS(completions=NS(create=create))))
    with limited_model_requests(2, 4000) as budget:
        with pytest.raises(TimeoutError):
            model.complete([], [])
        assert model.complete([], []).content == "ok"
        with pytest.raises(RuntimeError, match="budget"):
            model.complete([], [])
        assert budget.used == 2
    assert len(requests) == 2 and requests[0]["stream"] is False
    assert requests[0]["max_tokens"] == 4000


def test_report_writing_cannot_start_another_wiki_tool_loop(tmp_path, library):
    from paper_agent.agent.loop import PhaseAgent

    agent = PhaseAgent(tmp_path / "p", model_client=Reader(), phase="ideas")
    assert "search_wiki" in agent._by_name
    with evidence_handoff():
        agent.configure(phase="ideas")
        assert "search_wiki" not in agent._by_name and "read_wiki" not in agent._by_name
        assert "write" in agent._by_name
    agent.configure(phase="ideas")
    assert "search_wiki" in agent._by_name


def test_idea_workflow_retrieves_then_writes_with_same_agent(
    tmp_path, library, monkeypatch
):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent.workflow import WorkflowState

    for name in ("literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md"):
        p = tmp_path / "project" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Task boundaries remain fixed.", encoding="utf-8")

    class ResearchWriter(Reader):
        writes = 0

        def complete(self, messages, tools):
            if (
                any(t["function"]["name"] == "search_wiki" for t in tools)
                or self.calls < 2
            ):
                return super().complete(messages, tools)
            assert all(
                t["function"]["name"]
                not in {
                    "search_wiki",
                    "read_wiki",
                    "expand_wiki_relations",
                    "browse_wiki",
                }
                for t in tools
            )
            assert "Known future load is required." in messages[1]["content"]
            assert "data-search/DATA_REPORT.md" in messages[1]["content"]
            if not self.writes:
                self.writes += 1
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "write",
                            "write",
                            {
                                "path": "idea-stage/IDEA_REPORT.md",
                                "content": "Hypothesis needs human review.",
                            },
                        ),
                    )
                )
            return ModelResponse(content="Draft ready")

    agent = PhaseAgent(
        tmp_path / "project",
        model_client=ResearchWriter(),
        phase="ideas",
        max_iterations=2,
    )
    runner = WorkflowRunner(agent)
    monkeypatch.setattr(runner, "_validate_phase_outputs", lambda phase: None)
    result = runner._run_ideas(WorkflowState("fixture"), "Relaxation")
    assert result.artifact == "Hypothesis needs human review."
    assert (
        result.metadata["research_sources"]["wiki_retrieval"]["budget"]["model_calls"]
        == 2
    )
