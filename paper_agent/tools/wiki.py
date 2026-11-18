"""Read-only tools for querying the shared research wiki."""

from __future__ import annotations

from paper_agent.tools.wiki_metadata import metadata_path

import json
import hashlib
import os
from pathlib import Path
from .wiki_layout import corpus_root, entity_dirs, resolve_legacy_path
from typing import Any

from .core import ToolContext, ToolResult, tool
from .wiki_retrieval import (ENTITY_TYPES, excerpts, load_page, load_pages,
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


def resolve_raw_paper_root() -> Path:
    """The shared PDF library follows the configured Wiki location."""
    root = resolve_wiki_root()
    if not root.exists() or not any(root.iterdir()):
        return root / "literature" / "raw-paper"
    return (corpus_root(root) / "raw-paper").resolve()


def _wiki_root() -> Path:
    root = resolve_wiki_root()
    if not root.is_dir():
        raise FileNotFoundError(f"research-wiki is not initialized: {root}")
    return root


def _quality_records(root: Path, target: Path) -> list[dict]:
    """读取分页内容时也附带问题；Idea/Experiment 只沿论文来源边追溯两步。"""
    papers = {target} if target.parent.name == "papers" else set()
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
                    paper = resolve_legacy_path(root, "papers/" + edge["to"][6:] + ".md").resolve()
                    if paper.parent in entity_dirs(root):
                        papers.add(paper)
        except (OSError, ValueError, KeyError, TypeError):
            return [{"status": "unavailable", "reason": "论文来源关系无法读取，请核对 graph/edges.jsonl。"}]
    records = []
    for paper in sorted(papers):
        path = metadata_path(paper, "quality")
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
                    expected = report.get("wiki_content_sha256")
                    if expected and (not paper.is_file() or hashlib.sha256(paper.read_bytes()).hexdigest() != expected):
                        record.update(status="stale", reason="Wiki 正文已变更，旧质量报告不适用于当前版本。")
                except (OSError, ValueError, KeyError, TypeError):
                    record.update(status="unavailable", reason="质量报告无法读取，请先核对该论文的问题记录。")
        records.append(record)
    return records


def _search_wiki_lexical(ctx: ToolContext, query: str, entity_type: str | None = None,
                         limit: int = 10) -> ToolResult:
    """BM25 Wiki search with Chinese support and section/line locators. Default groups
    by source paper; entity_type selects ungrouped pages. Scores are not confidence.
    The public wrapper returns titles or stored abstracts for screening.
    """
    if not query.strip():
        return ToolResult("query must not be empty", True, {"error_code": "EMPTY_QUERY"})
    root = _wiki_root()
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
                            "note": "词法排名不表示方法适用性；请先阅读 Wiki 正文中的条件和限制。"}))


def _search_candidates(ctx: ToolContext, query: str, entity_type: str | None = None,
                limit: int = 10, strategy: str = "auto", purpose: str = "evidence") -> ToolResult:
    """Search Wiki candidates with exact locations. strategy: auto, lexical, hybrid.
    auto fuses BM25/E5 when configured, otherwise uses BM25; degradation is explicit.
    Internal ranking channel; the public search_wiki returns screening results.
    Candidates are not validated support: read conditions and limitations before use.
    """
    if strategy not in {"auto", "lexical", "hybrid"} or purpose not in {"evidence", "method", "paper", "overview"}:
        return ToolResult("Unsupported search strategy or purpose", True)
    if not query.strip() or len(query) > 2000:
        return ToolResult("query must contain 1 to 2000 characters", True)
    if entity_type and entity_type not in ENTITY_TYPES:
        return ToolResult("Unsupported entity_type", True)
    limit = max(1, min(limit, 50))
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
            root = _wiki_root()
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
    if path.parent.name != "papers" or path.parent not in entity_dirs(root):
        return {}
    source = metadata_path(path, "source")
    if not source.is_file():
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
def search_wiki(ctx: ToolContext, query: str, entity_type: str | None = None,
                limit: int = 5, strategy: str = "auto", purpose: str = "evidence",
                include_relations: bool = True, view: str = "titles") -> ToolResult:
    """Discover Wiki pages without loading full bodies into the conversation.
    view: titles (default) returns titles and wiki_path identifiers; abstracts adds
    the complete existing Abstract section, with missing abstracts marked explicitly.
    No new summaries are generated. query can be keywords, a title or an identifier.
    limit defaults to 5, maximum 10. strategy: auto, lexical, hybrid.
    purpose: evidence, method, paper, overview. include_relations follows recorded
    graph links from the best hit to discover up to two related candidates,
    returned separately in related_matches so they never displace ranked matches.
    Use read_wiki(wiki_path) for a selected page's complete stored Markdown.
    Titles, abstracts, rankings and graph links alone do not verify scientific claims.
    """
    if view not in {"titles", "abstracts"}:
        return ToolResult("view must be titles or abstracts", True)
    limit = max(1, min(limit, 10))
    result = _search_candidates(ctx, query, entity_type=entity_type, limit=limit,
                                strategy=strategy,
                                purpose="evidence" if purpose == "overview" else purpose)
    if result.is_error:
        return result
    ranked = json.loads(result.output)
    root = _wiki_root()
    matches, seen = [], set()
    graph_matches = []

    def add_match(relative, *, origin="search", score=None):
        path = resolve_legacy_path(root, relative).resolve()
        destination = graph_matches if origin == "graph" else matches
        if (path in seen or len(destination) >= (2 if origin == "graph" else limit) or not path.is_file()
                or path.suffix != ".md"
                or path.parent not in entity_dirs(root)):
            return
        seen.add(path)
        page = load_page(path)
        match = {"title": page.title, "wiki_path": path.relative_to(root).as_posix(),
                 "entity_type": path.parent.name, "origin": origin,
                 "collection": path.parent.parent.name if path.parent.parent.name in {"literature", "autoresearch"} else "literature"}
        if score is not None:
            match["score"] = score
        if view == "abstracts":
            section = next((s for s in page.sections
                            if s.heading.casefold() in {"abstract", "摘要"}), None)
            abstract = ("\n".join(page.lines[section.start + 1:section.end]).strip()
                        if section else "")
            missing = not abstract or abstract.startswith(("_TODO", "_Not yet", "_To be", "<!--"))
            match.update(abstract="" if missing else abstract,
                         abstract_status="missing" if missing else "present")
        destination.append(match)

    candidates = ranked.get("matches", [])
    for match in candidates:
        add_match(match["wiki_path"], score=match.get("score"))
    relations, expansions = [], 0
    warnings = ranked.get("warnings", [])
    if include_relations and candidates:
        from .wiki_hybrid import expand_wiki_relations
        expansions = 1
        expanded = expand_wiki_relations(ctx, candidates[0]["wiki_path"], limit=2)
        if expanded.is_error:
            warnings.append("关系扩展不可用；保留文本检索结果。")
        else:
            graph = json.loads(expanded.output)
            warnings.extend(graph.get("warnings", []))
            for neighbor in graph.get("neighbors", []):
                relations.append({k: neighbor[k] for k in (
                    "wiki_path", "title", "relation", "from", "to", "status",
                    "graph_line", "via_source_idea",
                ) if k in neighbor})
                for relative in neighbor.get("paper_read_paths", []):
                    add_match(relative, origin="graph")
    data = {k: ranked[k] for k in (
        "query", "count", "ranking", "grouped_by_paper", "degraded", "semantic_status",
    ) if k in ranked}
    data.update(view=view, purpose=purpose, candidate_only=True, matches=matches,
                returned_count=len(matches), related_matches=graph_matches,
                relations=relations, relation_expansions=expansions,
                warnings=list(dict.fromkeys(warnings)),
                reading_policy="先按标题或已有摘要筛选，再用 read_wiki(wiki_path) 读取选中页面的完整 MD。缺少摘要不表示没有正文；复用已读内容，不反复重搜或重读同一页面。")
    return ToolResult(json.dumps(data, ensure_ascii=False))


@tool(read_only=True)
def read_wiki(ctx: ToolContext, wiki_path: str) -> ToolResult:
    """Read one selected Wiki page's complete stored Markdown, without truncation.
    Pass the exact relative wiki_path returned by search_wiki, e.g. papers/example.md.
    Returns content, title, citation path, fingerprint and quality/source records.
    Reuse this result instead of repeatedly reading the same page. Wiki evidence
    does not imply the original PDF has been checked in this run.
    """
    relative = Path(wiki_path)
    root = _wiki_root()
    path = resolve_legacy_path(root, relative).resolve()
    if (relative.is_absolute() or ".." in relative.parts or path.suffix != ".md"
            or path.parent not in entity_dirs(root)):
        return ToolResult("wiki_path must identify a Markdown page inside the Wiki entity directories", True)
    if not path.is_file():
        return ToolResult(f"Wiki page not found: {wiki_path}", True)
    page = load_page(path)
    return ToolResult(json.dumps({
        **paper_card(page, root),
        "quality_records": _quality_records(root, path),
        "source_record": _source_record(root, path),
    }, ensure_ascii=False))
