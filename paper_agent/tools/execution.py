"""Bounded command and Skill execution helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .core import ToolContext, ToolResult, tool


@tool()
async def bash(ctx: ToolContext, command: str, cwd: str | None = None, timeout_seconds: int = 600) -> ToolResult:
    """Run a shell command in the workspace with bounded output and timeout."""
    workdir = ctx.resolve(cwd)
    if not workdir.is_dir():
        return ToolResult(f"Not a directory: {cwd or '.'}", True, {"error_code": "NOT_DIRECTORY"})
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
    return ToolResult(output, process.returncode != 0, {"returncode": process.returncode})


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
