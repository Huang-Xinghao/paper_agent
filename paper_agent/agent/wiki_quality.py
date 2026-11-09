"""Evidence contracts, bounded repair and independent review before Wiki publication."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from paper_agent.tools.paper_preprocessing import (
    evidence_context,
    evidence_errors,
    source_context,
)
from paper_agent.tools.research_wiki import PAPER_SECTIONS

from .wiki_prompts import REVIEW_PROMPT, REVISION_PROMPT, REQUIRED_SECTIONS


QUALITY_VERSION = "wiki-quality-local-v4"
STATUSES = {"extracted", "not_found", "not_applicable", "parse_insufficient"}
VERDICTS = {"supported", "partial", "conflict", "insufficient"}
KINDS = {"input_condition", "main_result", "data_evaluation", "limitation"}
LABELS = {
    "extracted": "已提取",
    "not_found": "原文未找到明确报告",
    "not_applicable": "不适用",
    "parse_insufficient": "解析不足，待核验",
}
PLACEHOLDER = re.compile(
    r"\bTODO\b|\bTBD\b|^\s*(?:Unknown|N/?A|待补充)\s*[.。]?\s*$", re.I
)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", encoding="utf-8", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        except Exception:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _calculation_errors(calculation, document):
    if not isinstance(calculation, dict):
        return ["calculation must be an object"]
    operands = calculation.get("operands")
    if not isinstance(operands, list) or len(operands) != 2:
        return ["calculation needs exactly two source operands"]
    errors = evidence_errors(operands, document)
    for operand in operands:
        if not isinstance(operand, dict):
            continue
        value = operand.get("value")
        if type(value) not in (int, float) or not math.isfinite(value):
            errors.append("calculation operands must be finite numbers")
            continue
        quote = operand.get("quote")
        numbers = re.findall(
            r"(?<![\w.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
            quote if isinstance(quote, str) else "",
        )
        if not any(float(n) == value for n in numbers):
            errors.append("calculation operand is not in its source quote")
    if errors:
        return errors
    first, second = [o["value"] for o in operands]
    operation = calculation.get("operation")
    if operation == "difference":
        expected = second - first
    elif operation == "relative_change_percent" and first != 0:
        expected = 100 * (second - first) / abs(first)
    else:
        return ["unsupported calculation or zero denominator"]
    value = calculation.get("value")
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not math.isclose(value, expected, abs_tol=1e-6, rel_tol=1e-6)
    ):
        return [f"calculation result mismatch; expected {expected}"]
    return []


def validate_entities(synthesis: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, str]]]:
    idea = synthesis.get("idea")
    experiments = synthesis.get("experiments")
    if not isinstance(idea, dict) or not isinstance(experiments, list):
        raise ValueError("Synthesis must contain an idea object and experiments array")
    for key in ("title", "thesis", "structure"):
        if not isinstance(idea.get(key), str) or not idea[key].strip():
            raise ValueError(f"Idea missing {key}")
    validated = []
    seen = set()
    for number, experiment in enumerate(experiments, 1):
        if not isinstance(experiment, dict):
            raise ValueError(f"Experiment {number} must be an object")
        for key in ("title", "question", "hypothesis", "protocol", "results"):
            if not isinstance(experiment.get(key), str) or not experiment[key].strip():
                raise ValueError(f"Experiment {number} missing {key}")
        name = re.sub(r"\s+", " ", experiment["title"].strip()).casefold()
        if name in seen:
            raise ValueError(f"Duplicate experiment: {experiment['title']}")
        seen.add(name)
        validated.append(experiment)
    if len(validated) > 3:
        raise ValueError("Synthesis has more than 3 experiments")
    return idea, validated


def analysis_errors(draft: dict, document: dict) -> list[str]:
    errors = []
    sections, states, claims = (
        draft.get(k) for k in ("sections", "section_status", "critical_claims")
    )
    if (not isinstance(sections, dict) or not REQUIRED_SECTIONS <= set(sections)
            or not set(sections) <= PAPER_SECTIONS):
        errors.append("sections must use local permitted headings and include required headings")
    if not isinstance(states, dict) or not isinstance(sections, dict) or set(states) != set(sections):
        errors.append("section_status must cover exactly the present Wiki headings")
    if errors:
        return errors
    for heading, record in states.items():
        body = sections[heading]
        if not isinstance(body, str) or not body.strip() or PLACEHOLDER.search(body):
            errors.append(f"{heading}: empty/template content is not allowed")
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("status"), str)
            or record["status"] not in STATUSES
        ):
            errors.append(f"{heading}: invalid information status")
            continue
        if record.get("origin") not in ("author_reported", "platform_analysis"):
            errors.append(
                f"{heading}: distinguish author report from platform analysis"
            )
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            errors.append(f"{heading}: explanation required")
        if record["status"] == "extracted" or record.get("evidence"):
            errors.extend(
                f"{heading}: {error}"
                for error in evidence_errors(record.get("evidence"), document)
            )
        elif not isinstance(record.get("evidence"), list):
            errors.append(
                f"{heading}: evidence array required, use [] for no source evidence"
            )
    abstract = sections.get("Abstract", "")
    normalize = lambda text: re.sub(r"\s+", " ", text).strip()
    if (abstract != "Not available in the source PDF."
            and isinstance(abstract, str) and normalize(abstract) not in normalize("\n".join(p["text"] for p in document["pages"]))):
        errors.append("Abstract must be verbatim source text, allowing only line wrapping")
    idea = draft.get("idea", {})
    structure = idea.get("structure", "") if isinstance(idea, dict) else ""
    if not isinstance(structure, str) or not re.match(r"^\s*```(?:text|ascii)?\s*\n[^`]+\n```", structure):
        errors.append("idea.structure must start with an actual fenced flow diagram")
    if isinstance(idea, dict) and ("evidence" in idea or (isinstance(structure, str) and re.search(r"(?mi)^##?\s+Evidence\b", structure))):
        errors.append("Idea must not include an Evidence field or heading")
    try:
        validate_entities(draft)
    except ValueError as exc:
        errors.append(str(exc))
    if not isinstance(claims, list):
        return errors + ["critical_claims array required"]
    for heading, field in (("Dataset and Features", "datasets"),):
        if (
            isinstance(states.get(heading), dict)
            and states[heading].get("status") == "not_applicable"
            and draft.get(field)
        ):
            errors.append(f"{heading}: not_applicable conflicts with nonempty {field}")
    ids = set()
    for claim in claims:
        if (
            not isinstance(claim, dict)
            or not isinstance(claim.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", claim["id"])
        ):
            errors.append("claim must have a short stable string ID")
            continue
        if claim["id"] in ids:
            errors.append(f"duplicate claim ID: {claim['id']}")
        ids.add(claim["id"])
        if (
            not isinstance(claim.get("kind"), str)
            or claim["kind"] not in KINDS
            or not isinstance(claim.get("section"), str)
            or claim["section"] not in sections
        ):
            errors.append(f"{claim['id']}: invalid kind or section")
        if (
            not isinstance(claim.get("statement"), str)
            or not claim["statement"].strip()
            or PLACEHOLDER.search(claim["statement"])
        ):
            errors.append(f"{claim['id']}: substantive statement required")
        errors.extend(
            f"{claim['id']}: {error}"
            for error in evidence_errors(claim.get("evidence"), document)
        )
        if "calculation" in claim:
            errors.extend(
                f"{claim['id']}: {error}"
                for error in _calculation_errors(claim["calculation"], document)
            )
    required_kinds = {
        "One-line thesis": "main_result",
        "Method": "input_condition",
        "Dataset and Features": "data_evaluation",
        "Limitations / Failure Modes": "limitation",
    }
    for heading, kind in required_kinds.items():
        if (
            isinstance(states.get(heading), dict)
            and states[heading].get("status") == "extracted"
            and not any(
                isinstance(c, dict)
                and c.get("section") == heading
                and c.get("kind") == kind
                for c in claims
            )
        ):
            errors.append(f"{heading}: missing decision-changing {kind} claim")
    return errors


def review_errors(
    review: dict,
    draft: dict,
    document: dict | None = None,
    previous_paper_issues: dict | None = None,
) -> list[str]:
    claims, sections = review.get("claims"), review.get("sections")
    expected = {c["id"] for c in draft["critical_claims"]}
    if (
        not isinstance(claims, list)
        or any(
            not isinstance(c, dict) or not isinstance(c.get("id"), str) for c in claims
        )
        or {c.get("id") for c in claims} != expected
        or len(claims) != len(expected)
    ):
        return ["review must cover every claim ID exactly once"]
    if not isinstance(sections, dict) or set(sections) != set(draft["sections"]):
        return ["review must cover all present sections"]
    errors = []
    for label, record in [
        *((c["id"], c) for c in claims),
        *sections.items(),
        ("entities", review.get("entities")),
        ("coverage", review.get("coverage")),
    ]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("status"), str)
            or record["status"] not in VERDICTS
            or not isinstance(record.get("reason"), str)
            or not record["reason"].strip()
        ):
            errors.append(f"{label}: invalid review verdict or missing explanation")
        elif label != "coverage" and record["status"] != "supported":
            errors.append(f"{label}: {record['status']}: {record['reason']}")
    coverage = review.get("coverage", {})
    if not isinstance(coverage, dict) or not isinstance(coverage.get("missing"), list):
        errors.append("source-first coverage check must return a missing array")
    issues = review.get("issues")
    if not isinstance(issues, list):
        return errors + ["review issues must be an array"]
    ids, gaps = set(), set()
    for issue in issues:
        if not isinstance(issue, dict):
            errors.append("issue must be an object")
            continue
        identity = issue.get("id")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity)
            or identity in ids
        ):
            errors.append("issue needs a unique short ID")
            continue
        ids.add(identity)
        kinds = {
            "paper": {"internal_inconsistency", "reporting_gap"},
            "ingestion": {"coverage_gap", "parse_gap"},
        }
        if (
            not isinstance(issue.get("origin"), str)
            or issue["origin"] not in kinds
            or not isinstance(issue.get("kind"), str)
            or issue["kind"] not in kinds[issue["origin"]]
        ):
            errors.append(f"{identity}: invalid issue origin/kind")
        for field in ("description", "impact", "guidance"):
            if not isinstance(issue.get(field), str) or not issue[field].strip():
                errors.append(f"{identity}: {field} required")
        evidence = issue.get("evidence")
        if not isinstance(evidence, list) or (
            not evidence and issue.get("kind") != "parse_gap"
        ):
            errors.append(f"{identity}: source evidence required")
        elif evidence and document is not None:
            errors.extend(
                f"{identity}: {e}" for e in evidence_errors(evidence, document)
            )
        if issue.get("kind") == "internal_inconsistency" and (
            not isinstance(evidence, list)
            or len(
                {
                    (e.get("block_id"), e.get("quote"))
                    for e in evidence
                    if isinstance(e, dict)
                    and isinstance(e.get("block_id"), str)
                    and isinstance(e.get("quote"), str)
                }
            )
            < 2
        ):
            errors.append(f"{identity}: cite both contradictory reports")
        if issue.get("kind") == "coverage_gap":
            gaps.add(identity)
    if isinstance(coverage, dict) and isinstance(coverage.get("missing"), list):
        missing = coverage["missing"]
        if any(not isinstance(i, str) for i in missing) or set(missing) != gaps:
            errors.append("coverage.missing must reference all coverage_gap issue IDs")
        if coverage.get("status") != "supported" and not gaps:
            errors.append("non-supported coverage needs a recorded coverage_gap")
        if gaps and coverage.get("status") == "supported":
            errors.append("supported coverage conflicts with unresolved coverage gaps")
    corrections = review.get("issue_corrections", [])
    if not isinstance(corrections, list):
        errors.append("issue_corrections must be an array")
    else:
        seen = set()
        for correction in corrections:
            if not isinstance(correction, dict) or not isinstance(
                correction.get("id"), str
            ):
                errors.append("correction must identify a previous paper issue")
                continue
            identity = correction["id"]
            if identity not in (previous_paper_issues or {}) or identity in seen:
                errors.append(
                    "correction must identify a previous paper issue exactly once"
                )
            seen.add(identity)
            if (
                not isinstance(correction.get("reason"), str)
                or not correction["reason"].strip()
            ):
                errors.append("correction needs an explicit reason")
            if document is not None:
                errors.extend(
                    f"correction {identity}: {e}"
                    for e in evidence_errors(correction.get("evidence"), document)
                )
    return errors


def collect_issues(review: dict, draft: dict, document: dict) -> list[dict]:
    """问题属于当前证据版本；历史复核意见留在 attempts，不混成当前未解决问题。"""
    issues = deepcopy(review.get("issues", []))
    lookup = {b["id"]: b for b in document["blocks"]}
    for issue in issues:
        issue.update(status="open", detected_by="model_review")
        for ev in issue["evidence"]:
            ev["page"] = lookup[ev["block_id"]]["page"]
    for index, diagnostic in enumerate(document.get("diagnostics", [])):
        descriptions = {
            "empty_table": "表格未提取到可读内容",
            "empty_formula": "公式未提取到可读内容",
            "unlinked_caption_or_note": "图注或脚注尚未确认归属",
            "replacement_characters": "文本含无法识别的替换字符",
            "no_readable_text": "该页未提取到可读文字",
        }
        issues.append(
            {
                "id": f"parser-{index + 1}",
                "origin": "ingestion",
                "kind": "parse_gap",
                "description": descriptions.get(
                    diagnostic["issue"], diagnostic["issue"]
                ),
                "impact": "该位置的内容或注释关联尚未核实。",
                "guidance": "使用该位置的信息前先核对原文页面，必要时重新解析；系统尚未自动补全。",
                "status": "open",
                "detected_by": "parser",
                "evidence": [],
                "location": diagnostic,
            }
        )
    for heading, record in draft["section_status"].items():
        if record["status"] == "parse_insufficient":
            issues.append(
                {
                    "id": f"section-{len(issues) + 1}",
                    "origin": "ingestion",
                    "kind": "parse_gap",
                    "description": f"{heading}: {record['reason']}",
                    "impact": "此栏目尚不能作为已核实信息使用。",
                    "guidance": "先核对原文或重新解析相关页面。",
                    "status": "open",
                    "detected_by": "section_status",
                    "evidence": record["evidence"],
                }
            )
    return issues


def prepare_analysis(
    draft: dict,
    document: dict,
    analyze,
    directory: Path,
    progress,
    *,
    model_name: str = "injected",
    source_saved: bool = False,
    validate: Callable[[dict], None] | None = None,
    revision_context: dict | None = None,
) -> tuple[dict, dict]:
    """At most one targeted revision; failed drafts cannot overwrite published Wiki pages."""
    source = source_context(document)
    attempts = []
    paper_issues = {}
    issue_corrections = []
    if not source_saved:
        write_json(directory / "source.json", document)

    def request(system: str, user: str) -> dict:
        try:
            return analyze(system, user)
        except Exception as exc:
            # 请求失败也留下可定位的草稿；不把服务端异常正文复制进公开质量报告。
            write_json(
                directory / "quality.json",
                {
                    "schema_version": QUALITY_VERSION,
                    "document_id": document["document_id"],
                    "pdf_sha256": document["pdf_sha256"],
                    "model": model_name,
                    "status": "request_failed",
                    "attempts": attempts,
                    "failed_stage": "review" if system == REVIEW_PROMPT else "revision",
                    "error_type": type(exc).__name__,
                },
            )
            raise RuntimeError(
                f"Wiki evidence request failed; draft retained at {directory / 'quality.json'}"
            ) from exc

    for attempt in range(2):
        write_json(directory / "draft.json", draft)
        errors = analysis_errors(draft, document)
        if not errors and validate is not None:
            try:
                validate(draft)
            except ValueError as exc:
                errors.append(str(exc))
        review = None
        if not errors:
            progress(
                "verify_evidence",
                claims=len(draft["critical_claims"]),
                attempt=attempt + 1,
            )
            review = request(
                REVIEW_PROMPT,
                json.dumps(
                    {
                        "draft": draft,
                        "source_document": source,
                        "previous_paper_issues": list(paper_issues.values()),
                        "claim_context": {
                            c["id"]: [
                                b["id"]
                                for b in evidence_context(c["evidence"], document)
                            ]
                            for c in draft["critical_claims"]
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            errors = review_errors(review, draft, document, paper_issues)
        # 覆盖遗漏先有限修订；仍有遗漏时随文公开，不把论文缺陷和事实错误混为阻断项。
        gaps = (
            review.get("coverage", {}).get("missing", [])
            if review and not errors
            else []
        )
        issues = (
            collect_issues(review, draft, document) if review and not errors else []
        )
        # 修改整理稿不能解决论文原文的疑点；纠正误报必须明确说明理由并留下证据。
        if review and not errors:
            for correction in review.get("issue_corrections", []):
                issue_corrections.append(
                    {"original_issue": paper_issues.pop(correction["id"]), **correction}
                )
        for issue in issues:
            if issue["origin"] == "paper":
                identity = f"review{attempt + 1}-{issue['id']}"
                paper_issues[identity] = {**issue, "id": identity}
        if not errors and review:
            current_keys = {
                (
                    i["kind"],
                    tuple(sorted((e["block_id"], e["quote"]) for e in i["evidence"])),
                )
                for i in issues
                if i["origin"] == "paper"
            }
            for identity, issue in paper_issues.items():
                key = (
                    issue["kind"],
                    tuple(
                        sorted((e["block_id"], e["quote"]) for e in issue["evidence"])
                    ),
                )
                if key not in current_keys:
                    retained = deepcopy(issue)
                    issues.append(retained)
        attempts.append({"draft": deepcopy(draft), "review": review, "errors": errors})
        report = {
            "schema_version": QUALITY_VERSION,
            "document_id": document["document_id"],
            "model": model_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pdf_sha256": document["pdf_sha256"],
            "status": (
                "needs_revision"
                if errors or (gaps and attempt == 0)
                else ("accepted_with_issues" if issues else "passed")
            ),
            "issues": issues,
            "issue_corrections": issue_corrections,
            "attempts": attempts,
            "verification_scope": "verbatim source checks and model evidence-support review; not replication",
        }
        write_json(directory / "quality.json", report)
        if not errors and (not gaps or attempt == 1):
            return draft, report
        if attempt == 0:
            if gaps and not errors:
                errors = [
                    "source-first omissions: "
                    + json.dumps(review["issues"], ensure_ascii=False)
                ]
            progress("revise_evidence", issues=errors)
            revision = request(
                REVISION_PROMPT,
                json.dumps(
                    {
                        **(revision_context or {}),
                        "draft": draft,
                        "issues": errors,
                        "review": review,
                        "source_document": source,
                    },
                    ensure_ascii=False,
                ),
            )
            # 引用元数据不是本阶段的修订对象，防止核验时悄悄替换已确认的论文身份。
            for key in (
                "sections",
                "section_status",
                "critical_claims",
                "idea",
                "datasets",
                "experiments",
            ):
                if key in revision:
                    draft[key] = revision[key]
    raise ValueError(
        f"Wiki evidence review failed; draft retained at {directory / 'quality.json'}: "
        + "; ".join(errors)
    )
