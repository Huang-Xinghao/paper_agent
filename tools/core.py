"""Small, workspace-scoped tool runtime used by the paper workflow."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    output: str = ""
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Awaitable[ToolResult] | ToolResult]
    read_only: bool = False


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    skill_dirs: tuple[Path, ...] = ()
    knowledge_roots: tuple[Path, ...] = ()

    def resolve_read(self, path: str | Path | None = None) -> Path:
        from .data_access import resolve_data_read
        return resolve_data_read(self.workspace, self.knowledge_roots,
                                 self.workspace if path in (None, "") else path)

    def display_path(self, path: Path) -> str:
        root = self.workspace.resolve()
        return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

    def resolve(self, path: str | Path | None = None, *, write: bool = False) -> Path:
        candidate = self.workspace if path in (None, "") else Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        root = self.workspace.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes workspace: {path}")
        if write and candidate in {root / "project-config.json", root / "experiments/resources.json"}:
            raise ValueError("compute configuration is user-owned; agent tools cannot edit it")
        if write and candidate.name == "":
            raise ValueError("a file path is required")
        return candidate

    def resolve_knowledge(self, path: str | Path) -> Path:
        """Resolve a read-only path below one of the configured knowledge roots."""
        candidate = Path(path).resolve()
        for root in self.knowledge_roots:
            root = root.resolve()
            if candidate.is_relative_to(root):
                return candidate
        raise ValueError(f"path is outside configured knowledge roots: {path}")


_TOOLS: dict[str, ToolSpec] = {}


def tool(*, read_only: bool = False):
    def decorate(handler: Callable[..., Awaitable[ToolResult] | ToolResult]):
        _TOOLS[handler.__name__] = ToolSpec(
            handler.__name__, inspect.getdoc(handler) or "", handler, read_only
        )
        return handler

    return decorate


def builtin_tools() -> tuple[ToolSpec, ...]:
    return tuple(_TOOLS[name] for name in sorted(_TOOLS))


async def invoke(tool_name: str, ctx: ToolContext, **arguments: Any) -> ToolResult:
    try:
        spec = _TOOLS[tool_name]
    except KeyError:
        return ToolResult(f"Unknown tool: {tool_name}", True, {"error_code": "UNKNOWN_TOOL"})
    try:
        result = spec.handler(ctx, **arguments)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:  # tool failures are returned to the model as data
        return ToolResult(str(exc), True, {"error_code": type(exc).__name__})


def _error(message: str, code: str) -> ToolResult:
    return ToolResult(message, True, {"error_code": code})


@tool(read_only=True)
def ls(ctx: ToolContext, path: str | None = None, limit: int = 500) -> ToolResult:
    """List files and directories under the workspace."""
    target = ctx.resolve_read(path)
    if not target.exists():
        return _error(f"Directory not found: {path or '.'}", "DIRECTORY_NOT_FOUND")
    if not target.is_dir():
        return _error(f"Not a directory: {path}", "NOT_DIRECTORY")
    entries = sorted((f"{p.name}/" if p.is_dir() else p.name) for p in target.iterdir())
    return ToolResult("\n".join(entries[: max(1, min(limit, 5000))]) or "(empty directory)")


@tool(read_only=True)
def read(ctx: ToolContext, path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    """Read a text file by line range."""
    target = ctx.resolve_read(path)
    if not target.exists():
        return _error(f"File not found: {path}", "FILE_NOT_FOUND")
    if target.is_dir():
        return _error(f"Cannot read directory: {path}", "IS_DIRECTORY")
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return _error(f"Not a UTF-8 text file: {path}", "BINARY_FILE")
    selected = lines[max(offset, 0): max(offset, 0) + max(1, min(limit, 2000))]
    return ToolResult("\n".join(f"{i + max(offset, 0) + 1}: {line}" for i, line in enumerate(selected)))


@tool()
def write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """Create or replace a UTF-8 text file inside the workspace."""
    target = ctx.resolve(path, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(f"Wrote {target.relative_to(ctx.workspace.resolve())}")


@tool()
def edit(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
    """Replace an exact text fragment in a workspace file."""
    target = ctx.resolve(path, write=True)
    if not target.is_file():
        return _error(f"File not found: {path}", "FILE_NOT_FOUND")
    content = target.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return _error("old text was not found", "TEXT_NOT_FOUND")
    if count > 1 and not replace_all:
        return _error(f"old text occurs {count} times; set replace_all=true", "AMBIGUOUS_REPLACEMENT")
    target.write_text(content.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
    return ToolResult(f"Edited {target.relative_to(ctx.workspace.resolve())}", metadata={"replacements": count if replace_all else 1})


@tool(read_only=True)
def find(ctx: ToolContext, pattern: str, path: str | None = None, limit: int = 200) -> ToolResult:
    """Find workspace files using a glob pattern."""
    target = ctx.resolve_read(path)
    matches = []
    for p in target.glob(pattern):
        try:
            ctx.resolve_read(p)
        except ValueError:
            continue
        matches.append(ctx.display_path(p))
    matches.sort()
    return ToolResult("\n".join(matches[: max(1, min(limit, 5000))]) or "No files found")


@tool(read_only=True)
def grep(ctx: ToolContext, pattern: str, path: str | None = None, limit: int = 200) -> ToolResult:
    """Search UTF-8 text files with a regular expression."""
    target = ctx.resolve_read(path)
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _error(f"invalid regex: {exc}", "INVALID_REGEX")
    matches: list[str] = []
    for file in files:
        try:
            ctx.resolve_read(file)
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError, ValueError):
            continue
        for number, line in enumerate(lines, 1):
            if regex.search(line):
                matches.append(f"{ctx.display_path(file)}:{number}:{line}")
                if len(matches) >= max(1, min(limit, 2000)):
                    return ToolResult("\n".join(matches), metadata={"truncated": True})
    return ToolResult("\n".join(matches) or "No matches")


@tool(read_only=True)
async def sleep(ctx: ToolContext, seconds: float) -> ToolResult:
    """Wait briefly for an external job; maximum 60 seconds."""
    await asyncio.sleep(max(0, min(seconds, 60)))
    return ToolResult(f"Slept for {max(0, min(seconds, 60))} seconds")
