"""Read-only retrieval tools for the battery research knowledge base."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, tool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _tokens(text: str) -> set[str]:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", text)}
    # Chinese technical documents do not have whitespace-delimited words.
    tokens.update(char for char in text if "\u4e00" <= char <= "\u9fff")
    return tokens


def _root(ctx: ToolContext) -> Path:
    if not ctx.knowledge_roots:
        raise ValueError("battery knowledge root is not configured")
    root = ctx.knowledge_roots[0].resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"battery knowledge root not found: {root}")
    return root


def _resource_type(relative: Path) -> str:
    name = relative.as_posix()
    if "数据集" in relative.parts[:-1]:
        return "dataset"
    if name.startswith("电池输入/"):
        return "input"
    if name.startswith("电池模型/"):
        return "method"
    return "overview"


def _resource_id(relative: Path) -> str:
    return relative.with_suffix("").as_posix()


def _index(root: Path) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        resources.append({
            "resource_id": _resource_id(relative),
            "type": _resource_type(relative),
            "title": next((line.lstrip("# ").strip() for line in text.splitlines()
                            if line.startswith("#")), path.stem),
            "path": relative.as_posix(),
            "text": text,
            "tokens": _tokens(f"{relative} {text}"),
        })
    return resources


def _public(item: dict[str, Any], *, score: int | None = None,
            query: str | None = None) -> dict[str, Any]:
    result = {"resource_id": item["resource_id"], "type": item["type"],
              "title": item["title"], "path": item["path"]}
    if score is not None:
        result["score"] = score
    if query:
        query_terms = _tokens(query)
        lines = [line.strip() for line in item["text"].splitlines()
                 if query_terms & _tokens(line)]
        result["matched_sections"] = lines[:5]
    return result


@tool(read_only=True)
def search_battery_resources(ctx: ToolContext, query: str, stage: str | None = None,
                             resource_types: list[str] | None = None,
                             top_k: int = 8) -> ToolResult:
    """Search the configured battery knowledge base and return cited resources."""
    if not query.strip():
        return ToolResult("query must not be empty", True, {"error_code": "EMPTY_QUERY"})
    try:
        root = _root(ctx)
        resources = _index(root)
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult(str(exc), True, {"error_code": "KNOWLEDGE_ROOT_UNAVAILABLE"})
    terms = _tokens(f"{query} {stage or ''}")
    allowed = set(resource_types or ())
    ranked = []
    for item in resources:
        if allowed and item["type"] not in allowed:
            continue
        score = len(terms & item["tokens"])
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    limit = max(1, min(int(top_k), 30))
    return ToolResult(_json({
        "query": query, "stage": stage, "resource_types": sorted(allowed),
        "knowledge_root": str(root), "indexed": len(resources),
        "count": min(len(ranked), limit),
        "resources": [_public(item, score=score, query=query) for score, item in ranked[:limit]],
        "note": "Resource presence does not prove that a dataset contains the required fields; verify locally.",
    }), metadata={"count": min(len(ranked), limit), "indexed": len(resources)})


@tool(read_only=True)
def read_battery_resource(ctx: ToolContext, resource_id: str, max_chars: int = 24000) -> ToolResult:
    """Read one resource returned by search_battery_resources by stable resource ID."""
    try:
        root = _root(ctx)
        matches = [item for item in _index(root) if item["resource_id"] == resource_id]
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult(str(exc), True, {"error_code": "KNOWLEDGE_ROOT_UNAVAILABLE"})
    if not matches:
        return ToolResult(f"Battery resource not found: {resource_id}", True,
                          {"error_code": "RESOURCE_NOT_FOUND"})
    item = matches[0]
    limit = max(1000, min(int(max_chars), 60000))
    text = item["text"]
    truncated = len(text) > limit
    return ToolResult(_json({
        "resource_id": item["resource_id"], "type": item["type"],
        "title": item["title"], "path": item["path"],
        "absolute_path": str((root / item["path"]).resolve()),
        "citation_markdown": f'[{item["resource_id"]}](<{(root / item["path"]).resolve()}>)',
        "content": text[:limit], "truncated": truncated,
    }), metadata={"resource_id": resource_id, "truncated": truncated})
