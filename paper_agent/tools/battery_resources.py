"""Read-only retrieval tools for the battery research knowledge base."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from .core import ToolContext, ToolResult, tool
from .data_access import registered_data_sources
from .dataset_inspection import inspect_data_file


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _tokens(text: str) -> set[str]:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", text)}
    # Dataset names often occur only in filenames such as Tongji_README.md.
    tokens.update(part for token in tuple(tokens) for part in re.split(r'[_-]+', token) if part)
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
    if "数据集" in relative.parts[:-1]:
        return "dataset"
    if {"电池输入", "电解液输入"} & set(relative.parts[:-1]):
        return "input"
    if {"电池模型", "电解液模型"} & set(relative.parts[:-1]):
        return "method"
    return "overview"


def _resource_id(relative: Path) -> str:
    return relative.with_suffix("").as_posix()


def _index(root: Path) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if (any(part.startswith(".") for part in relative.parts)
                or not path.resolve().is_relative_to(root)):
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


def _search(ctx: ToolContext, query: str, resource_types: list[str] | None,
            top_k: int, offset: int) -> ToolResult:
    root = _root(ctx)
    resources = _index(root)
    sources = registered_data_sources(ctx.knowledge_roots)
    terms = _tokens(query)
    allowed = set(resource_types or ())
    if allowed - {"dataset", "input", "method", "overview"}:
        raise ValueError("resource_types must contain dataset, input, method or overview")
    ranked = []
    for item in resources:
        if allowed and item["type"] not in allowed:
            continue
        score = len(terms & item["tokens"])
        if score or not terms:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    limit = max(1, min(int(top_k), 30))
    selected = ranked[offset:offset + limit]
    matched_ids = {item["resource_id"] for _, item in ranked}
    matching_sources = [source for source in sources
                        if (not allowed or "dataset" in allowed)
                        and (not terms or source["resource_id"] in matched_ids
                             or terms & _tokens(f'{source["resource_id"]} {source["path"]}'))]
    return ToolResult(_json({
        "query": query, "resource_types": sorted(allowed),
        "knowledge_root": str(root), "indexed": len(resources),
        "count": len(selected), "total": len(ranked),
        "resources": [{**_public(item, score=score, query=query),
                       "data_sources": [source for source in sources
                                        if source["resource_id"] == item["resource_id"]]}
                      for score, item in selected],
        "sources": matching_sources[offset:offset + limit], "total_sources": len(matching_sources),
        "next_offset": offset + limit if offset + limit < max(len(ranked), len(matching_sources)) else None,
    }), metadata={"count": len(selected), "indexed": len(resources)})


def _read(ctx: ToolContext, resource_id: str, offset: int, limit: int) -> ToolResult:
    root = _root(ctx)
    relative = Path(resource_id + ".md")
    target = (root / relative).resolve()
    if (relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root)
            or any(part.startswith('.') for part in relative.parts) or not target.is_file()):
        return ToolResult(f"Battery resource not found: {resource_id}", True,
                          {"error_code": "RESOURCE_NOT_FOUND"})
    lines = target.read_text(encoding="utf-8").splitlines()
    limit = max(1, min(int(limit), 2000))
    selected = lines[offset:offset + limit]
    return ToolResult(_json({
        "resource_id": resource_id, "type": _resource_type(relative),
        "path": relative.as_posix(), "absolute_path": str(target),
        "citation_markdown": f'[{resource_id}](<{target}>)',
        "content": "\n".join(f"{offset + i + 1}: {line}" for i, line in enumerate(selected)),
        "total_lines": len(lines),
        "next_offset": offset + limit if offset + limit < len(lines) else None,
        "data_sources": [source for source in registered_data_sources(ctx.knowledge_roots)
                         if source["resource_id"] == resource_id],
    }), metadata={"resource_id": resource_id})


@tool(read_only=True)
def battery_resources(ctx: ToolContext, action: Literal["search", "read", "inspect"],
                      query: str = "", resource_id: str = "", path: str = "",
                      resource_types: list[str] | None = None, top_k: int = 8,
                      offset: int = 0, limit: int = 200) -> ToolResult:
    """One read-only entry for battery resources and registered local datasets.

    search: query keywords (empty lists the catalog), optional resource_types
    (dataset/input/method/overview), top_k<=30 and offset. Returns resource IDs,
    matching descriptions and registered data paths together; never opens samples.
    read: resource_id from search, offset/limit in lines, next_offset for continuation.
    inspect: path to one trusted JSON/CSV/PKL dataset file; returns bounded structural
    samples using the runner Python, not a scientific audit. Source data is read-only.
    Select one action per call. Read project files with ordinary file tools.
    """
    try:
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        if action == "search":
            if resource_id or path:
                raise ValueError("search uses query, not resource_id or path")
            return _search(ctx, query, resource_types, top_k, offset)
        if action == "read":
            if not resource_id or path or query:
                raise ValueError("read requires resource_id only")
            return _read(ctx, resource_id, offset, limit)
        if action == "inspect":
            if not path or resource_id or query:
                raise ValueError("inspect requires path only")
            return inspect_data_file(ctx, path)
        raise ValueError("action must be search, read or inspect")
    except (ValueError, OSError) as exc:
        return ToolResult(str(exc), True, {"error_code": type(exc).__name__})
