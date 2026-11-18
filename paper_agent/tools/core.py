"""Small, workspace-scoped tool runtime used by the paper workflow."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import black


@dataclass
class ToolResult:
    output: str = ""
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    images: list[dict[str, Any]] = field(default_factory=list)
    structured_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Awaitable[ToolResult] | ToolResult]
    read_only: bool = False
    input_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    skill_dirs: tuple[Path, ...] = ()
    knowledge_roots: tuple[Path, ...] = ()
    on_progress: Callable | None = field(default=None, compare=False, repr=False)

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
        if write and candidate == root / "project-config.json":
            raise ValueError("compute configuration is user-owned; agent tools cannot edit it")
        if write and candidate.name == "":
            raise ValueError("a file path is required")
        return candidate


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
    """Read a text file by line range: offset is the number of lines to skip
    (zero-based), and limit is the number of lines to read (default 200, max 2000).
    Output includes one-based line numbers. These units are lines, unlike the
    coordinator's read_project_file, which uses characters.
    """
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


def _format_python(target: Path, content: str) -> tuple[str, str]:
    """Format Python sources; preserve incomplete code so it can be edited later."""
    if target.suffix != ".py":
        return content, ""
    try:
        return black.format_str(content, mode=black.FileMode()), " (Black formatted; read before exact-text edits)"
    except black.InvalidInput:
        return content, " (Black skipped: invalid or incomplete Python; saved unchanged)"


@tool()
def write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """Create or replace a UTF-8 file. Python files are Black-formatted; read back before exact-text edits."""
    target = ctx.resolve(path, write=True)
    content, notice = _format_python(target, content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(f"Wrote {target.relative_to(ctx.workspace.resolve())}{notice}")


@tool()
def edit(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
    """Replace an exact fragment, then Black-format Python files. Read current text before subsequent edits."""
    target = ctx.resolve(path, write=True)
    if not target.is_file():
        return _error(f"File not found: {path}", "FILE_NOT_FOUND")
    content = target.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return _error("old text was not found", "TEXT_NOT_FOUND")
    if count > 1 and not replace_all:
        return _error(f"old text occurs {count} times; set replace_all=true", "AMBIGUOUS_REPLACEMENT")
    content, notice = _format_python(target, content.replace(old, new, -1 if replace_all else 1))
    target.write_text(content, encoding="utf-8")
    return ToolResult(f"Edited {target.relative_to(ctx.workspace.resolve())}{notice}", metadata={"replacements": count if replace_all else 1})


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


@tool()
async def bash(ctx: ToolContext, command: str, cwd: str | None = None, timeout_seconds: int = 600) -> ToolResult:
    """Run a shell command in the workspace with bounded output and timeout.

    Failures return the exit code and changed, explicitly redirected workspace
    log tails. Chain dependent generation steps with && so failure is preserved.
    After generating previews, confirm success and list actual nonempty output
    files before calling view_image; never guess page filenames or page counts.
    """
    workdir = ctx.resolve(cwd)
    if not workdir.is_dir():
        return ToolResult(f"Not a directory: {cwd or '.'}", True, {"error_code": "NOT_DIRECTORY"})
    logs = _bash_redirected_logs(ctx, workdir, command)
    try:
        process = await asyncio.create_subprocess_shell(
            command, cwd=workdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1, min(timeout_seconds, 600)))
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ToolResult(f"Command timed out: {command}", True, {"error_code": "BASH_TIMEOUT"})
    output = (stdout + stderr).decode("utf-8", errors="replace")[-12000:]
    if process.returncode != 0:
        summaries = []
        for path, before in logs:
            try:
                # Recheck containment after execution in case a command replaced a path.
                path = ctx.resolve(path)
                stat = path.stat()
                if not path.is_file() or (stat.st_ino, stat.st_mtime_ns, stat.st_size) == before:
                    continue
                with path.open('rb') as stream:
                    stream.seek(max(0, stat.st_size - 2000))
                    tail = stream.read(2000).decode('utf-8', errors='replace')
                summaries.append(f"Log tail ({ctx.display_path(path)}):\n{tail}")
            except (OSError, ValueError):
                continue
        details = '\n\n'.join(summaries) or 'No updated redirected workspace log found; inspect the command log.'
        output = (f"Command failed (exit code {process.returncode}).\n"
                  "Stop dependent preview/image checks. Fix the error, rerun generation, "
                  "then confirm success and list actual nonempty output files before view_image.\n\n"
                  f"{details}\n\nConsole output:\n{output or '(empty)'}")[:12000]
    return ToolResult(output, process.returncode != 0, {"returncode": process.returncode})


def _bash_redirected_logs(ctx: ToolContext, workdir: Path, command: str):
    """Find literal log redirects only; never evaluate shell text or scan directories."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    logs = {}
    for operator, filename in zip(tokens, tokens[1:]):
        if operator not in {'>', '>>', '&>'}:
            continue
        if any(char in filename for char in '$`*?[]'):
            continue
        try:
            path = ctx.resolve(workdir / filename)
            if path.suffix.lower() not in {'.log', '.txt', '.out', '.err'}:
                continue
            stat = path.stat() if path.exists() else None
            logs[path] = (stat.st_ino, stat.st_mtime_ns, stat.st_size) if stat else None
        except (OSError, ValueError):
            continue
    return list(logs.items())[-2:]


def _skill_root(ctx: ToolContext, name: str) -> Path:
    for base in ctx.skill_dirs:
        candidate = base / name
        if (candidate / "SKILL.md").is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"Skill not found: {name}")


@tool(read_only=True)
def skill(ctx: ToolContext, name: str) -> ToolResult:
    """Load the SKILL.md for an activated paper workflow Skill."""
    root = _skill_root(ctx, name)
    return ToolResult(root.joinpath("SKILL.md").read_text(encoding="utf-8"), metadata={"skill": name})


@tool(read_only=True)
def skill_resource_read(ctx: ToolContext, name: str, resource: str) -> ToolResult:
    """Read a skill resource with an explicit references/ or templates/ path."""
    root = _skill_root(ctx, name)
    relative = Path(resource)
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative.parts[0] not in {"references", "templates"}):
        return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
    directory = (root / relative.parts[0]).resolve()
    target = (root / relative).resolve()
    if (not directory.is_relative_to(root) or not target.is_relative_to(directory)
            or not target.is_file()):
        return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
    return ToolResult(target.read_text(encoding="utf-8"), metadata={"skill": name, "resource": resource})
