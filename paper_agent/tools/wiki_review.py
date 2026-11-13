"""Source-grounded Wiki checks and independent model review."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
from typing import Any, Callable
import unicodedata

from paper_agent.storage import write_json
from .paper_preprocessing import evidence_context, evidence_errors, source_context
from .research_wiki import PAPER_SECTIONS, CLASSIFICATION_FIELDS
from .wiki_prompts import (
    REVIEW_PROMPT,
    REVISION_PROMPT,
    REQUIRED_SECTIONS,
    REVIEW_RECHECK_PROMPT,
)


def visible_text(text: str) -> str:
    text = re.sub(r"\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)", r"\1", text)
    text = re.sub(
        r"</?(?:b|strong|i|em|span|sup|sub|p|br)(?:\s[^>]*)?/?>", "", text, flags=re.I
    )
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"\\([*`_{}\[\]()#+.!-])", r"\1", text)
    return html.unescape(unicodedata.normalize("NFC", text)).replace("\u00ad", "")


def normalized_text(text: str) -> str:
    return " ".join(visible_text(text).split())


def abstract_provenance(abstract: str, document: dict) -> dict | None:
    if not isinstance(abstract, str) or not abstract.strip():
        return None
    pages = document["pages"]
    # Preserve every lexical token and number. Only display markup and whitespace
    # are normalized; edited words, missing interior sentences and units still fail.
    for mode, normalize in [
        ("verbatim", lambda s: " ".join(s.split())),
        ("display_format", normalized_text),
    ]:
        needle = normalize(abstract)
        if not needle:
            continue
        for index in range(len(pages)):
            for width in (1, 2, 3):
                span = pages[index : index + width]
                if len(span) != width:
                    continue
                text = "\n".join(p["text"] for p in span)
                if needle in normalize(text):
                    return {
                        "match": mode,
                        "pages": [p["page"] for p in span],
                        "normalization": "source-display-v1",
                        "source_text": text,
                        "published_text": abstract,
                    }
    return None


def flow_diagram_error(structure) -> str | None:
    if not isinstance(structure, str):
        return "idea.structure must start with an actual fenced flow diagram"
    lines = structure.lstrip().splitlines()
    if not lines or not re.fullmatch(r"`{3,}(?:text|ascii)?\s*", lines[0]):
        return "idea.structure must start with an actual fenced flow diagram"
    fence = re.match(r"`+", lines[0])[0]
    end = next(
        (
            i
            for i, line in enumerate(lines[1:], 1)
            if re.fullmatch(r"`{" + str(len(fence)) + r",}\s*", line)
        ),
        None,
    )
    if end is None:
        return "idea.structure flow diagram has no closing fence"
    body = "\n".join(lines[1:end]).strip()
    if (
        not body
        or not re.search(r"->|→|=>|↓|\|", body)
        or len(re.findall(r"\w+", body)) < 2
    ):
        return "idea.structure requires a substantive input/process/output flow diagram"
    return None


QUALITY_VERSION = "wiki-quality-local-v7"
STATUSES = {"extracted", "not_found", "not_applicable", "parse_insufficient"}
VERDICTS = {"supported", "partial", "conflict", "insufficient"}
KINDS = {"input_condition", "main_result", "data_evaluation", "limitation"}
LABELS = {
    "extracted": "Extracted",
    "not_found": "Not explicitly reported in the source",
    "not_applicable": "Not applicable",
    "parse_insufficient": "Insufficient parsing; verification required",
}
PLACEHOLDER = re.compile(
    r"\bTODO\b|\bTBD\b|^\s*(?:Unknown|N/?A|待补充)\s*[.。]?\s*$", re.I
)


def request_json_analysis(model, system: str, user: str | list[dict]) -> dict:
    """Make an isolated, tool-free model request for review or evidence enrichment."""
    response = model.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], []
    )
    if response.tool_calls:
        raise ValueError("Wiki analysis model must return JSON, not tool calls")
    content = response.content.strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Wiki analysis model returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Wiki analysis model must return a JSON object")
    return result


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


def validate_entities(
    synthesis: dict[str, Any],
) -> tuple[dict[str, str], list[dict[str, str]]]:
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
    if (
        not isinstance(sections, dict)
        or not REQUIRED_SECTIONS <= set(sections)
        or not set(sections) <= PAPER_SECTIONS
    ):
        errors.append(
            "sections must use local permitted headings and include required headings"
        )
    if (
        not isinstance(states, dict)
        or not isinstance(sections, dict)
        or set(states) != set(sections)
    ):
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
    if (
        abstract != "Not available in the source PDF."
        and abstract_provenance(abstract, document) is None
    ):
        errors.append(
            "Abstract must be verbatim source text, allowing only display markup and whitespace normalization"
        )
    idea = draft.get("idea", {})
    structure = idea.get("structure", "") if isinstance(idea, dict) else ""
    diagram_error = flow_diagram_error(structure)
    if diagram_error:
        errors.append(diagram_error)
    if isinstance(idea, dict) and (
        "evidence" in idea
        or (
            isinstance(structure, str)
            and re.search(r"(?mi)^##?\s+Evidence\b", structure)
        )
    ):
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
    figure_candidates: list[dict] | None = None,
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
    verdicts = [
        *((c["id"], c) for c in claims),
        *sections.items(),
        ("entities", review.get("entities")),
        ("coverage", review.get("coverage")),
    ]
    if figure_candidates:
        verdicts.append(("figures", review.get("figures")))
    for label, record in verdicts:
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


def blocking_finding_errors(review: dict, draft: dict, document: dict) -> list[str]:
    """Require actionable, grounded objections before a model can block publication."""
    targets = {
        c.get("id")
        for c in review.get("claims", [])
        if isinstance(c, dict) and c.get("status") in VERDICTS - {"supported"}
    }
    targets.update(
        key
        for key, value in review.get("sections", {}).items()
        if isinstance(value, dict) and value.get("status") in VERDICTS - {"supported"}
    )
    targets.update(
        key
        for key in ("entities", "figures")
        if isinstance(review.get(key), dict)
        and review[key].get("status") in VERDICTS - {"supported"}
    )
    if not targets:
        return []
    findings = review.get("blocking_findings")
    if not isinstance(findings, list):
        return [
            "blocking verdicts require blocking_findings with exact fields, rules and source evidence"
        ]
    rules = {
        "source_support",
        "coverage",
        "english",
        "classification",
        "entity_structure",
        "verbatim_abstract",
        "generic_mechanism",
        "figure_selection",
        "bibliography",
    }
    errors, covered = [], set()
    for finding in findings:
        if not isinstance(finding, dict):
            errors.append("blocking finding must be an object")
            continue
        target, field, rule = (finding.get(key) for key in ("target", "field", "rule"))
        if (
            target not in targets
            or rule not in rules
            or not isinstance(field, str)
            or not field.startswith("/")
        ):
            errors.append(
                "blocking finding needs an existing verdict target, JSON field pointer and allowed rule"
            )
            continue
        try:
            value = draft
            for part in field[1:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            errors.append(
                f"blocking finding points to a nonexistent draft field: {field}"
            )
        if rule == "classification" and field.startswith("/datasets"):
            errors.append("dataset records do not require node classification")
        observed, expected = finding.get("observed"), finding.get("expected")
        if (
            not isinstance(observed, str)
            or not observed.strip()
            or not isinstance(expected, str)
            or not expected.strip()
            or observed.strip() == expected.strip()
        ):
            errors.append(
                "blocking finding must identify differing concrete observed and expected content"
            )
        evidence = finding.get("evidence")
        if rule in {"entity_structure", "english"} and evidence == []:
            pass
        else:
            errors.extend(evidence_errors(evidence, document))
        covered.add(target)
    if targets - covered:
        errors.append(
            f"blocking findings missing for verdict targets: {sorted(targets - covered)}"
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
            "empty_table": "No readable table content was extracted",
            "empty_formula": "No readable formula content was extracted",
            "unlinked_caption_or_note": "The caption or note has not been linked to its source content",
            "replacement_characters": "The text contains unrecognized replacement characters",
            "no_readable_text": "No readable text was extracted from this page",
        }
        issues.append(
            {
                "id": f"parser-{index + 1}",
                "origin": "ingestion",
                "kind": "parse_gap",
                "description": descriptions.get(
                    diagnostic["issue"], diagnostic["issue"]
                ),
                "impact": "The content or annotation linkage at this location has not been verified.",
                "guidance": "Check the original page before using this information and reparse if needed; the system has not filled the gap.",
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
                    "impact": "This section cannot yet be used as verified information.",
                    "guidance": "Check the source or reparse the relevant pages first.",
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
    figure_candidates: list[dict] | None = None,
    max_revisions: int = 1,
    previous_report: dict | None = None,
    allow_coverage_gaps: bool = False,
) -> tuple[dict, dict]:
    """At most one targeted revision; failed drafts cannot overwrite published Wiki pages."""
    if max_revisions not in (0, 1):
        raise ValueError("max_revisions must be 0 or 1")
    source = source_context(document)
    previous_report = previous_report or {}
    attempts = deepcopy(previous_report.get("attempts", []))
    paper_issues = {
        issue["id"]: deepcopy(issue)
        for issue in previous_report.get("issues", [])
        if issue.get("origin") == "paper"
    }
    issue_corrections = deepcopy(previous_report.get("issue_corrections", []))
    review_rechecked = False
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
                    "failed_stage": (
                        "review_recheck"
                        if system == REVIEW_RECHECK_PROMPT
                        else "review" if system == REVIEW_PROMPT else "revision"
                    ),
                    "error_type": type(exc).__name__,
                },
            )
            raise RuntimeError(
                f"Wiki evidence request failed; draft retained at {directory / 'quality.json'}"
            ) from exc

    for attempt in range(max_revisions + 1):
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
                        **(revision_context or {}),
                        "draft": draft,
                        "source_document": source,
                        "figure_candidates": figure_candidates or [],
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
            finding_errors = blocking_finding_errors(review, draft, document)
            if finding_errors and not review_rechecked:
                review_rechecked = True
                progress("recheck_review", issues=finding_errors)
                previous_review = review
                review = request(
                    REVIEW_RECHECK_PROMPT,
                    json.dumps(
                        {
                            **(revision_context or {}),
                            "draft": draft,
                            "source_document": source,
                            "figure_candidates": figure_candidates or [],
                            "previous_paper_issues": list(paper_issues.values()),
                            "previous_review": previous_review,
                            "review_validation_errors": finding_errors,
                        },
                        ensure_ascii=False,
                    ),
                )
                write_json(
                    directory / "review-recheck.json",
                    {
                        "previous_review": previous_review,
                        "errors": finding_errors,
                        "review": review,
                    },
                )
                finding_errors = blocking_finding_errors(review, draft, document)
            errors = (
                review_errors(review, draft, document, paper_issues, figure_candidates)
                + finding_errors
            )
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
                identity = f"review{len(attempts) + 1}-{issue['id']}"
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
                if errors or (gaps and attempt == 0 and not allow_coverage_gaps)
                else ("accepted_with_issues" if issues else "passed")
            ),
            "issues": issues,
            "issue_corrections": issue_corrections,
            "attempts": attempts,
            "verification_scope": "verbatim source checks and model evidence-support review; not replication",
        }
        write_json(directory / "quality.json", report)
        if not errors and (not gaps or attempt == 1 or allow_coverage_gaps):
            return draft, report
        if attempt < max_revisions:
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
                        "figure_candidates": figure_candidates or [],
                        "draft": draft,
                        "issues": errors,
                        "review": review,
                        "source_document": source,
                    },
                    ensure_ascii=False,
                ),
            )
            # Classification is revisable; citation identity remains immutable.
            classification = revision.get("metadata_classification", {})
            if not isinstance(classification, dict):
                raise ValueError("metadata_classification must be an object")
            if isinstance(draft.get("metadata"), dict):
                draft["metadata"].update(
                    {
                        key: classification[key]
                        for key in CLASSIFICATION_FIELDS
                        if key in classification
                    }
                )
            for key in (
                "sections",
                "section_status",
                "critical_claims",
                "idea",
                "datasets",
                "experiments",
                "figure_selection",
            ):
                if key in revision:
                    draft[key] = revision[key]
    raise ValueError(
        f"Wiki evidence review failed; draft retained at {directory / 'quality.json'}: "
        + "; ".join(errors)
    )
