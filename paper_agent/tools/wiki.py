"""Read-only tools for querying the shared research wiki."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, tool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


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
        candidates = (path for path in root.rglob("*.md")
                      if path.parent == root or path.parent.name in allowed_types)
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
        matches.append({"path": ctx.display_path(path.resolve()),
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
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, offset)
    end = min(len(lines), start + max(1, min(limit, 2000)))
    return ToolResult(_json({"path": ctx.display_path(target),
                             "total_lines": len(lines), "offset": start,
                             "content": "\n".join(lines[start:end]),
                             "next_offset": end if end < len(lines) else None}))
