"""Failure injection for evidence handling; model responses are deterministic substitutes."""

from copy import deepcopy
import hashlib
import json

import pytest

from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.wiki_quality import (
    analysis_errors,
    prepare_analysis,
    render_sections,
    review_errors,
)
from paper_agent.agent.model_client import ModelResponse
from paper_agent.tools.paper_preprocessing import document_from_structured
from paper_agent.tools.research_wiki import PAPER_SECTIONS
from test_wiki_agent import analysis

SOURCE = (
    "Synthetic method: training requires history; inference needs no target history."
)
NOTE = "Synthetic evaluation: laboratory data only; baseline 10 mV, method 8 mV."


def document(sha=""):
    return document_from_structured(
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {"type": "paragraph_title", "content": "Method"},
                        {"type": "text", "content": SOURCE},
                        {
                            "type": "table",
                            "content": "baseline 10 mV, method 8 mV",
                            "captions": [
                                {"content": "Table 1. Synthetic RMSE evaluation"}
                            ],
                            "footnotes": [{"content": NOTE}],
                        },
                        {
                            "type": "text",
                            "content": "Synthetic main text: batch size 32. Appendix: best batch size 16.",
                        },
                    ],
                }
            ]
        },
        pdf_sha256=sha,
    )


def draft():
    result = analysis(datasets=[], experiments=[])
    ev = [{"block_id": "p1-b1", "quote": SOURCE}]
    result["section_status"] = {
        s: {
            "status": "extracted",
            "origin": "author_reported",
            "reason": "Synthetic evidence for contract testing",
            "evidence": deepcopy(ev),
        }
        for s in PAPER_SECTIONS
    }
    kinds = {
        "Method": "input_condition",
        "Key Results": "main_result",
        "Dataset and Features": "data_evaluation",
        "Experiments": "data_evaluation",
        "Limitations / Failure Modes": "limitation",
    }
    result["critical_claims"] = [
        {
            "id": f"c{i}",
            "kind": kind,
            "section": section,
            "statement": SOURCE,
            "evidence": deepcopy(ev),
        }
        for i, (section, kind) in enumerate(kinds.items())
    ]
    return result


def supported(result):
    verdict = {"status": "supported", "reason": "Synthetic review verdict"}
    return {
        "claims": [{"id": c["id"], **verdict} for c in result["critical_claims"]],
        "sections": {s: dict(verdict) for s in PAPER_SECTIONS},
        "entities": dict(verdict),
        "coverage": {**verdict, "missing": []},
        "issues": [],
    }


def issue(origin="ingestion", kind="coverage_gap"):
    return {
        "id": "i1",
        "origin": origin,
        "kind": kind,
        "description": "Synthetic omitted evaluation condition",
        "impact": "Affects baseline comparability",
        "guidance": "Check the cited source before selecting settings",
        "evidence": [{"block_id": "p1-b2-f0", "quote": NOTE}],
    }


def test_theory_paper_can_explicitly_omit_empirical_experiments():
    result = draft()
    for section in ("Dataset and Features", "Experiments"):
        result["sections"][section] = "不适用：本样本研究理论性质，没有经验实验。"
        result["section_status"][section] = {
            "status": "not_applicable",
            "origin": "platform_analysis",
            "reason": "本样本不包含经验实验。",
            "evidence": [],
        }
    result["critical_claims"] = [
        c for c in result["critical_claims"] if c["kind"] != "data_evaluation"
    ]
    assert analysis_errors(result, document()) == []
    assert review_errors(supported(result), result) == []
    rendered = render_sections(result, document())
    assert (
        "不适用" in rendered["Experiments"]
        and "平台分析（非作者声明）" in rendered["Experiments"]
    )
    assert not result["experiments"]


def test_placeholders_bad_quotations_and_missing_condition_claim_are_detected():
    result = draft()
    result["sections"]["Method"] = "TODO"
    result["critical_claims"][0]["evidence"][0]["quote"] = "Fabricated quotation"
    errors = analysis_errors(result, document())
    assert any("template" in e for e in errors)
    assert any("verbatim" in e for e in errors)
    result = draft()
    result["critical_claims"] = result["critical_claims"][1:]
    assert any("input_condition" in e for e in analysis_errors(result, document()))


def test_explicit_numeric_derivations_are_recalculated_from_source_operands():
    result = draft()
    claim = result["critical_claims"][1]
    claim["calculation"] = {
        "operation": "relative_change_percent",
        "operands": [
            {"value": n, "block_id": "p1-b2", "quote": "baseline 10 mV, method 8 mV"}
            for n in (10, 8)
        ],
        "value": -20,
    }
    assert analysis_errors(result, document()) == []
    claim["calculation"]["value"] = -25
    assert any("result mismatch" in e for e in analysis_errors(result, document()))
    claim["calculation"]["operands"][0]["value"] = 15
    assert any(
        "not in its source quote" in e for e in analysis_errors(result, document())
    )


def test_source_first_omission_triggers_revision_even_when_all_claims_are_supported(
    tmp_path,
):
    result = draft()
    calls = []

    def analyze(system, user):
        payload = json.loads(user)
        calls.append(payload)
        assert any(b["text"] == NOTE for b in payload["source_document"]["blocks"])
        if "claim_context" in payload:
            review = supported(payload["draft"])
            if len(calls) == 1:
                review["coverage"].update(
                    status="partial",
                    missing=["i1"],
                )
                review["issues"] = [issue()]
            return review
        assert "source-first omissions" in " ".join(payload["issues"])
        corrected = deepcopy(payload["draft"])
        corrected["sections"]["Limitations / Failure Modes"] = NOTE
        corrected["section_status"]["Limitations / Failure Modes"]["evidence"] = [
            {"block_id": "p1-b2-f0", "quote": NOTE}
        ]
        corrected["critical_claims"][-1].update(
            statement=NOTE, evidence=[{"block_id": "p1-b2-f0", "quote": NOTE}]
        )
        return corrected

    revised, report = prepare_analysis(
        result, document(), analyze, tmp_path, lambda *a, **k: None
    )
    assert len(calls) == 3 and report["status"] == "passed"
    assert len(report["attempts"]) == 2
    assert revised["critical_claims"][-1]["statement"] == NOTE
    assert (tmp_path / "draft.json").is_file() and (tmp_path / "source.json").is_file()


def test_unresolved_review_preserves_existing_wiki_and_keeps_failed_draft(
    tmp_path, monkeypatch
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    doc = document(hashlib.sha256(pdf.read_bytes()).hexdigest())
    monkeypatch.setattr(
        "paper_agent.agent.wiki.read_mineru_paper",
        lambda *a: {
            "document": doc,
            "pages": doc["pages"],
            "status": "completed",
            "figures": [],
        },
    )
    root = tmp_path / "research-wiki/papers"
    root.mkdir(parents=True)
    existing = root / "existing.md"
    existing.write_text("Existing user-maintained content", encoding="utf-8")

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            if "claim_context" in payload:
                review = supported(payload["draft"])
                review["claims"][0].update(
                    status="partial", reason="Training-history condition omitted"
                )
                return ModelResponse(content=json.dumps(review))
            return ModelResponse(content=json.dumps(draft()))

    model = Model()
    with pytest.raises(ValueError, match="Wiki evidence review failed"):
        WikiAgent(tmp_path, model_client=model).go("Read", paper_path=pdf)
    assert model.calls == 4
    assert existing.read_text() == "Existing user-maintained content"
    assert list(root.iterdir()) == [existing]
    reports = list((tmp_path / ".paper-agent/wiki-preprocessing").rglob("quality.json"))
    assert (
        len(reports) == 1
        and json.loads(reports[0].read_text())["status"] == "needs_revision"
    )


def test_successful_ingest_publishes_source_identity_and_quality_report(
    tmp_path, monkeypatch
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    doc = document(hashlib.sha256(pdf.read_bytes()).hexdigest())
    monkeypatch.setattr(
        "paper_agent.agent.wiki.read_mineru_paper",
        lambda *a: {
            "document": doc,
            "pages": doc["pages"],
            "status": "completed",
            "figures": [],
        },
    )

    class Model:
        def complete(self, messages, tools):
            payload = json.loads(messages[1]["content"])
            return ModelResponse(
                content=json.dumps(
                    supported(payload["draft"])
                    if "claim_context" in payload
                    else draft()
                )
            )

    state = WikiAgent(tmp_path, model_client=Model()).go("Read", paper_path=pdf)
    page = tmp_path / state.wiki_path
    provenance = json.loads(page.with_suffix(".source.json").read_text())
    assert provenance["document_id"] == doc["document_id"]
    assert provenance["content_origin"] == "paper_reported"
    evidence = json.loads((tmp_path / state.evidence_path).read_text())
    assert any(b["text"] == NOTE for b in evidence["blocks"])
    report = json.loads((tmp_path / state.quality_path).read_text())
    assert (
        report["status"] == "passed"
        and report["attempts"][0]["review"]["coverage"]["missing"] == []
    )
    assert "信息状态：已提取" in page.read_text() and "p1-b1" in page.read_text()
    for relative in (state.wiki_path, state.idea_path, *state.experiment_paths):
        content = (tmp_path / relative).read_text()
        assert "TODO" not in content and "summarize here" not in content


def test_review_cannot_skip_claims_or_sections_or_omit_coverage():
    result = draft()
    report = supported(result)
    report["claims"].pop()
    assert review_errors(report, result)


def test_paper_conflict_is_recorded_without_repair_and_requires_both_sources(tmp_path):
    result, doc = draft(), document()
    review = supported(result)
    conflict = issue("paper", "internal_inconsistency")
    conflict.update(
        description="Synthetic main/appendix batch-size conflict",
        evidence=[
            {"block_id": "p1-b3", "quote": "batch size 32"},
            {"block_id": "p1-b3", "quote": "best batch size 16"},
        ],
    )
    review["issues"] = [conflict]
    calls = []

    def analyze(*args):
        calls.append(args)
        return review

    _, report = prepare_analysis(result, doc, analyze, tmp_path, lambda *a, **k: None)
    assert len(calls) == 1 and report["status"] == "accepted_with_issues"
    assert report["issues"][0]["origin"] == "paper"
    rendered = render_sections(result, doc, report)["Limitations / Failure Modes"]
    assert (
        "论文本身的报告问题" in rendered
        and "batch size 32" in rendered
        and "best batch size 16" in rendered
    )
    review["issues"][0]["evidence"].pop()
    assert any("both contradictory" in e for e in review_errors(review, result, doc))


def test_remaining_coverage_gap_is_accepted_and_preserved_after_one_revision(tmp_path):
    result = draft()
    calls = []

    def analyze(system, user):
        calls.append(system)
        if "claim_context" not in json.loads(user):
            return result
        review = supported(result)
        review["coverage"].update(status="partial", missing=["i1"])
        review["issues"] = [issue()]
        return review

    _, report = prepare_analysis(
        result, document(), analyze, tmp_path, lambda *a, **k: None
    )
    assert len(calls) == 3 and len(report["attempts"]) == 2
    assert report["status"] == "accepted_with_issues"
    assert report["issues"][0]["kind"] == "coverage_gap"
    assert report["issues"][0]["evidence"][0]["page"] == 1


def test_issue_with_fabricated_evidence_still_blocks_publication(tmp_path):
    result = draft()

    def analyze(system, user):
        if "claim_context" not in json.loads(user):
            return result
        review = supported(result)
        bad = issue("paper", "reporting_gap")
        bad["evidence"][0]["quote"] = "not present in the original"
        review["issues"] = [bad]
        return review

    with pytest.raises(ValueError, match="verbatim"):
        prepare_analysis(result, document(), analyze, tmp_path, lambda *a, **k: None)


def test_parser_diagnostics_are_processing_issues_and_remain_visible(tmp_path):
    result, doc = draft(), document()
    doc["diagnostics"] = [
        {
            "issue": "empty_table",
            "page": 1,
            "block_id": "p1-b2",
            "action": "inspect_page_or_reparse",
        }
    ]
    _, report = prepare_analysis(
        result, doc, lambda *a: supported(result), tmp_path, lambda *a, **k: None
    )
    assert report["status"] == "accepted_with_issues"
    assert report["issues"][0]["origin"] == "ingestion"
    assert report["issues"][0]["detected_by"] == "parser"
    assert (
        "入库整理与解析问题"
        in render_sections(result, doc, report)["Limitations / Failure Modes"]
    )


def test_draft_revision_cannot_silently_close_a_paper_issue(tmp_path):
    result = draft()
    calls = []
    paper_issue = issue("paper", "reporting_gap")
    paper_issue["id"] = "paper1"

    def analyze(system, user):
        calls.append(system)
        if "claim_context" not in json.loads(user):
            return result
        review = supported(result)
        if len(calls) == 1:
            review["coverage"].update(status="partial", missing=["i1"])
            review["issues"] = [paper_issue, issue()]
        return review

    _, report = prepare_analysis(
        result, document(), analyze, tmp_path, lambda *a, **k: None
    )
    assert len(calls) == 3
    assert report["status"] == "accepted_with_issues"
    assert len(report["issues"]) == 1 and report["issues"][0]["origin"] == "paper"


def test_explicit_evidenced_correction_can_retract_a_model_false_alarm(tmp_path):
    result = draft()
    calls = []

    def analyze(system, user):
        calls.append(system)
        payload = json.loads(user)
        if "claim_context" not in payload:
            return result
        review = supported(result)
        if len(calls) == 1:
            paper = issue("paper", "reporting_gap")
            paper["id"] = "paper1"
            review["issues"] = [paper, issue()]
            review["coverage"].update(status="partial", missing=["i1"])
        else:
            prior = payload["previous_paper_issues"][0]
            review["issue_corrections"] = [
                {
                    "id": prior["id"],
                    "reason": "Synthetic false alarm: the condition is explicitly reported.",
                    "evidence": [{"block_id": "p1-b2-f0", "quote": NOTE}],
                }
            ]
        return review

    _, report = prepare_analysis(
        result, document(), analyze, tmp_path, lambda *a, **k: None
    )
    assert report["status"] == "passed" and not report["issues"]
    assert len(report["issue_corrections"]) == 1
    assert (
        "平台误报更正"
        in render_sections(result, document(), report)["Limitations / Failure Modes"]
    )
    invalid = supported(result)
    invalid["issue_corrections"] = [
        {"id": "unknown", "reason": "Unsupported dismissal", "evidence": []}
    ]
    assert review_errors(invalid, result, document())
    report = supported(result)
    report["coverage"].pop("missing")
    assert review_errors(report, result)


def test_review_connection_failure_keeps_draft_and_reports_failure_stage(tmp_path):
    def fail(*args):
        raise TimeoutError("Synthetic timeout")

    with pytest.raises(RuntimeError, match="draft retained"):
        prepare_analysis(draft(), document(), fail, tmp_path, lambda *a, **k: None)
    report = json.loads((tmp_path / "quality.json").read_text())
    assert report["status"] == "request_failed" and report["failed_stage"] == "review"
    assert (tmp_path / "draft.json").is_file()


@pytest.mark.parametrize("status", ["not_found", "parse_insufficient"])
def test_missing_information_states_are_preserved_with_reasons(status):
    result = draft()
    heading = "Dataset and Features"
    reason = (
        "检查了原文相关部分，没有明确报告数据划分。"
        if status == "not_found"
        else "相关表格没有可读内容，需检查页面。"
    )
    result["sections"][heading] = reason
    result["section_status"][heading].update(status=status, reason=reason, evidence=[])
    result["critical_claims"] = [
        c for c in result["critical_claims"] if c["section"] != heading
    ]
    assert analysis_errors(result, document()) == []
    assert reason in render_sections(result, document())[heading]
