"""Read-only tools for querying the project research wiki."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, tool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _wiki_root(ctx: ToolContext) -> Path:
    root = ctx.resolve("research-wiki")
    if not root.is_dir():
        raise FileNotFoundError("research-wiki is not initialized in this workspace")
    return root


@tool(read_only=True)
def search_wiki(ctx: ToolContext, query: str, entity_type: str | None = None,
                limit: int = 10) -> ToolResult:
    """Search research-wiki markdown pages and return ranked matching excerpts."""
    if not query.strip():
        return ToolResult("query must not be empty", True, {"error_code": "EMPTY_QUERY"})
    root = _wiki_root(ctx)
    allowed_types = {"papers", "ideas", "experiments", "claims"}
    if entity_type and entity_type not in allowed_types:
        return ToolResult(f"Unsupported entity_type: {entity_type}", True,
                          {"error_code": "UNSUPPORTED_ENTITY_TYPE"})
    terms = [term.casefold() for term in re.findall(r"\w+", query) if len(term) > 1]
    if not terms:
        return ToolResult("query must contain searchable terms", True,
                          {"error_code": "EMPTY_QUERY"})
    if entity_type:
        candidates = (root / entity_type).glob("*.md") if (root / entity_type).is_dir() else ()
    else:
        candidates = root.rglob("*.md")
    matches: list[dict[str, Any]] = []
    for path in sorted(candidates):
        text = path.read_text(encoding="utf-8", errors="replace")
        folded = text.casefold()
        score = sum(folded.count(term) for term in terms)
        if score == 0:
            continue
        lines = text.splitlines()
        excerpt = next((line.strip() for line in lines
                        if any(term in line.casefold() for term in terms)), "")
        matches.append({"path": str(path.relative_to(ctx.workspace.resolve())),
                        "entity_type": path.parent.name if path.parent != root else "root",
                        "score": score, "excerpt": excerpt[:500]})
    matches.sort(key=lambda item: (-item["score"], item["path"]))
    return ToolResult(_json({"query": query, "count": len(matches),
                             "matches": matches[:max(1, min(limit, 50))]}))


@tool(read_only=True)
def read_wiki(ctx: ToolContext, path: str, offset: int = 0,
              limit: int = 240) -> ToolResult:
    """Read a research-wiki markdown page by line range."""
    root = _wiki_root(ctx)
    target = ctx.resolve(path)
    if not target.is_file() or not target.is_relative_to(root):
        return ToolResult(f"Wiki page not found: {path}", True,
                          {"error_code": "WIKI_PAGE_NOT_FOUND"})
    if target.suffix.lower() != ".md":
        return ToolResult(f"Wiki page must be markdown: {path}", True,
                          {"error_code": "WIKI_PAGE_NOT_MARKDOWN"})
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, offset)
    end = min(len(lines), start + max(1, min(limit, 2000)))
    return ToolResult(_json({"path": str(target.relative_to(ctx.workspace.resolve())),
                             "total_lines": len(lines), "offset": start,
                             "content": "\n".join(lines[start:end]),
                             "next_offset": end if end < len(lines) else None}))
