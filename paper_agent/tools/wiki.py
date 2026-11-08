"""Read-only tools for querying the shared research wiki."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, tool
from .wiki_retrieval import (ENTITY_TYPES, excerpts, load_page, load_pages, location,
                             paper_card, paper_owners, rank_pages, tokenize)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _display_path(ctx: ToolContext, path: Path) -> str:
    workspace = ctx.workspace.resolve()
    return path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else str(path)


def resolve_wiki_root() -> Path:
    """Use the repository wiki unless PAPER_AGENT_WIKI_ROOT selects another root."""
    configured = os.environ.get("PAPER_AGENT_WIKI_ROOT")
    return (Path(configured).expanduser().resolve() if configured else
            Path(__file__).resolve().parents[2] / "research-wiki")


def _wiki_root(ctx: ToolContext) -> Path:
    root = resolve_wiki_root()
    if not root.is_dir():
        raise FileNotFoundError(f"research-wiki is not initialized: {root}")
    return root


def _quality_records(root: Path, target: Path) -> list[dict]:
    """读取分页内容时也附带问题；Idea/Experiment 只沿论文来源边追溯两步。"""
    papers = {target} if target.parent == root / "papers" else set()
    node_type = {"ideas": "idea", "experiments": "exp"}.get(target.parent.name)
    graph = root / "graph" / "edges.jsonl"
    if node_type and graph.is_file():
        try:
            edges = [json.loads(line) for line in graph.read_text(encoding="utf-8").splitlines() if line.strip()]
            nodes = {f"{node_type}:{target.stem}"}
            if node_type == "exp":
                nodes = {e["from"] for e in edges if e.get("type") == "tested_by" and e.get("to") in nodes}
            for edge in edges:
                if edge.get("type") == "inspired_by" and edge.get("from") in nodes and str(edge.get("to", "")).startswith("paper:"):
                    paper = (root / "papers" / (edge["to"][6:] + ".md")).resolve()
                    if paper.parent == (root / "papers").resolve():
                        papers.add(paper)
        except (OSError, ValueError, KeyError, TypeError):
            return [{"status": "unavailable", "reason": "论文来源关系无法读取，请核对 graph/edges.jsonl。"}]
    records = []
    for paper in sorted(papers):
        path = paper.with_suffix(".quality.json")
        record = {"paper_path": paper.relative_to(root).as_posix(), "status": "not_checked", "issues": []}
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                record.update(status="unavailable", reason="质量文件不在 Wiki 范围内。")
            else:
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    record.update(status=report["status"], document_id=report.get("document_id"),
                                  schema_version=report.get("schema_version"), issues=report.get("issues", []),
                                  issue_corrections=report.get("issue_corrections", []),
                                  report_path=path.relative_to(root).as_posix())
                except (OSError, ValueError, KeyError, TypeError):
                    record.update(status="unavailable", reason="质量报告无法读取，请先核对该论文的问题记录。")
        records.append(record)
    return records


def _search_wiki_lexical(ctx: ToolContext, query: str, entity_type: str | None = None,
                         limit: int = 10) -> ToolResult:
    """BM25 Wiki search with Chinese support and section/line locators. Default groups
    by source paper; entity_type selects ungrouped pages. Scores are not confidence.
    Use read_wiki(path=wiki_path, section=...) to inspect evidence and limitations.
    """
    if not query.strip():
        return ToolResult("query must not be empty", True, {"error_code": "EMPTY_QUERY"})
    root = _wiki_root(ctx)
    if entity_type and entity_type not in ENTITY_TYPES:
        return ToolResult(f"Unsupported entity_type: {entity_type}", True,
                          {"error_code": "UNSUPPORTED_ENTITY_TYPE"})
    if not tokenize(query):
        return ToolResult("query must contain searchable terms", True,
                          {"error_code": "EMPTY_QUERY"})
    pages = load_pages(root)
    owners, warnings = paper_owners(root, pages) if not entity_type else ({}, [])
    candidates = [p for p in pages if not entity_type or p.path.parent.name == entity_type]
    ranked = rank_pages(candidates, query)
    by_path = {p.path: p for p in pages}
    grouped: dict[Path, dict] = {}
    for page, score in ranked:
        for target in owners.get(page.path) or [page.path]:
            match = grouped.setdefault(target, {"page": by_path[target], "score": score, "hits": []})
            # 用最强单页得分，不累加实验页数量，避免同论文因派生页面多而占优。
            match["score"] = max(match["score"], score)
            if len(match["hits"]) < 3:
                match["hits"].append({"wiki_path": page.path.relative_to(root).as_posix(),
                                      "entity_type": page.path.parent.name,
                                      "score": round(score, 6), "excerpts": excerpts(page, query, root)})
    matches = sorted(grouped.values(), key=lambda m: (-m["score"], str(m["page"].path)))
    selected = matches[:max(1, min(limit, 50))]
    output = []
    for match in selected:
        page = match["page"]
        hits = match["hits"]
        first = next((e for hit in hits for e in hit["excerpts"]), None)
        output.append({"path": _display_path(ctx, page.path),
                       "wiki_path": page.path.relative_to(root).as_posix(),
                       "title": page.title, "entity_type": page.path.parent.name,
                       "score": round(match["score"], 6),
                       "excerpt": first["excerpt"] if first else page.title,
                       "hits": hits, "quality_records": _quality_records(root, page.path),
                       "source_record": _source_record(root, page.path)})
    return ToolResult(_json({"query": query, "count": len(matches), "matches": output,
                            "ranking": "bm25-cjk-v1", "grouped_by_paper": not bool(entity_type),
                            "warnings": warnings,
                            "note": "词法排名不表示方法适用性；请阅读条件、限制和原文。"}))


@tool(read_only=True)
def search_wiki(ctx: ToolContext, query: str, entity_type: str | None = None,
                limit: int = 10, strategy: str = "auto", purpose: str = "evidence") -> ToolResult:
    """Search Wiki candidates with exact locations. strategy: auto, lexical, hybrid.
    auto fuses BM25/E5 when configured, otherwise uses BM25; degradation is explicit.
    purpose: evidence, method, paper, overview. overview returns bounded live cards;
    follow next_offset with browse_wiki only when broader exploration is needed.
    Candidates are not validated support: read conditions and limitations before use.
    """
    if strategy not in {"auto", "lexical", "hybrid"} or purpose not in {"evidence", "method", "paper", "overview"}:
        return ToolResult("Unsupported search strategy or purpose", True)
    if not query.strip() or len(query) > 2000:
        return ToolResult("query must contain 1 to 2000 characters", True)
    if entity_type and entity_type not in ENTITY_TYPES:
        return ToolResult("Unsupported entity_type", True)
    limit = max(1, min(limit, 50))
    if purpose == "overview":
        result = browse_wiki(ctx, limit=min(limit, 8))
        if not result.is_error:
            data = json.loads(result.output)
            data.update(query=query, purpose=purpose, ranking="live-paper-cards", matches=[],
                        semantic_status="not_requested", candidate_only=True)
            result.output = _json(data)
        return result
    configured = bool(os.environ.get("PAPER_AGENT_EMBEDDING_DIR") and os.environ.get("PAPER_AGENT_EMBEDDING_PYTHON"))
    degradation = None
    if strategy == "hybrid" or (strategy == "auto" and configured):
        from .wiki_hybrid import search_wiki_hybrid
        result = search_wiki_hybrid(ctx, query, limit=limit, entity_type=entity_type)
        if result.is_error:
            if strategy == "hybrid":
                return result
            degradation = result.output
        else:
            data = json.loads(result.output)
            root = _wiki_root(ctx)
            for match in data["matches"]:
                match["path"] = _display_path(ctx, root / match["wiki_path"])
                match["entity_type"] = Path(match["wiki_path"]).parent.name
            data["count"] = len(data["matches"])
            data.update(purpose=purpose, candidate_only=True, degraded=False)
            result.output = _json(data)
            return result
    result = _search_wiki_lexical(ctx, query, entity_type, limit)
    if not result.is_error:
        data = json.loads(result.output)
        data.update(purpose=purpose, candidate_only=True, degraded=bool(degradation),
                    semantic_status="unavailable" if degradation else "not_configured" if strategy == "auto" else "explicitly_disabled")
        if degradation:
            data["warnings"].append("Semantic search failed; these are BM25-only candidates: " + degradation)
        result.output = _json(data)
    return result


def _source_record(root: Path, path: Path) -> dict:
    source = path.with_suffix(".source.json")
    if path.parent != root / "papers" or not source.is_file():
        return {"status": "not_available"}
    if not source.resolve().is_relative_to(root):
        return {"status": "unavailable"}
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
        return {"status": "available", **{k: record.get(k) for k in (
            "document_id", "pdf_sha256", "pdf_path", "evidence_path", "source", "content_origin")}}
    except (OSError, ValueError, AttributeError):
        return {"status": "unavailable"}


@tool(read_only=True)
def browse_wiki(ctx: ToolContext, offset: int = 0, limit: int = 20) -> ToolResult:
    """Browse live paper cards (thesis, problem, method, results, limitations).
    Follow next_offset to cover the whole library. Fields contain source excerpts,
    truncation markers and read_wiki locators, not newly generated summaries.
    """
    root = _wiki_root(ctx)
    pages = [p for p in load_pages(root) if p.path.parent.name == "papers"]
    start = max(0, offset)
    end = min(len(pages), start + max(1, min(limit, 50)))
    cards = [{**paper_card(p, root), "quality_records": _quality_records(root, p.path),
              "source_record": _source_record(root, p.path)} for p in pages[start:end]]
    return ToolResult(_json({"total": len(pages), "offset": start, "cards": cards,
                            "next_offset": end if end < len(pages) else None,
                            "ordering": "path", "selection": "all_papers_no_relevance_filter"}))


@tool(read_only=True)
def read_wiki(ctx: ToolContext, path: str, offset: int = 0,
              limit: int = 240, section: str | None = None,
              expected_content_sha256: str | None = None) -> ToolResult:
    """Read a Wiki page by 0-based offset, or exact section name. For section reads,
    offset is relative to that section. Returns 1-based line locators and a TOC.
    Supply expected_content_sha256 from search/read to reject stale source locations.
    """
    root = _wiki_root(ctx)
    target = Path(path)
    if not target.is_absolute():
        # Keep the research-wiki/... tool interface independent of project location.
        if target.parts and target.parts[0] == "research-wiki":
            target = Path(*target.parts[1:])
        target = root / target
    target = target.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return ToolResult(f"Wiki page not found: {path}", True,
                          {"error_code": "WIKI_PAGE_NOT_FOUND"})
    if target.suffix.lower() != ".md":
        return ToolResult(f"Wiki page must be markdown: {path}", True,
                          {"error_code": "WIKI_PAGE_NOT_MARKDOWN"})
    page = load_page(target)
    if expected_content_sha256 and expected_content_sha256 != page.fingerprint:
        return ToolResult(_json({"error": "Wiki content changed; search or read the new version before citing it",
                                 "wiki_path": target.relative_to(root).as_posix(),
                                 "expected_content_sha256": expected_content_sha256,
                                 "content_sha256": page.fingerprint}), True,
                          {"error_code": "WIKI_CONTENT_CHANGED"})
    lines = page.lines
    selected = next((s for s in page.sections if s.heading == section), None) if section else None
    if section and selected is None:
        return ToolResult(_json({"error": "section not found", "sections": [s.heading for s in page.sections]}),
                          True, {"error_code": "SECTION_NOT_FOUND"})
    base = selected.start if selected else 0
    stop = selected.end if selected else len(lines)
    start = min(stop, base + max(0, offset))
    end = min(stop, start + max(1, min(limit, 2000)))
    return ToolResult(_json({"path": _display_path(ctx, target),
                             "wiki_path": target.relative_to(root).as_posix(),
                             "total_lines": len(lines), "offset": start - base, "section": section,
                             "start_line": start + 1, "end_line": end,
                             "content": "\n".join(lines[start:end]),
                             "next_offset": end - base if end < stop else None,
                             "content_sha256": page.fingerprint,
                             "sections": [location(page, s, root) for s in page.sections],
                             "source_record": _source_record(root, target),
                             "quality_records": _quality_records(root, target)}))
