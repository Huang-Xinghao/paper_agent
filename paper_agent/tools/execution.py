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
    if not name or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Invalid skill name")
    for base in ctx.skill_dirs:
        candidate = (base / name).resolve()
        if candidate.is_relative_to(base.resolve()) and (candidate / "SKILL.md").is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"Skill not found: {name}")


@tool(read_only=True)
def skill(ctx: ToolContext, name: str) -> ToolResult:
    """Load the SKILL.md for an activated paper workflow Skill."""
    if ctx.skill_snapshot is not None:
        from paper_agent.skill_registry import export_markdown, usage
        item = ctx.skill_snapshot["skills"].get(name)
        if not item or not item["enabled"]:
            return ToolResult(f"Skill not found or disabled: {name}", True)
        return ToolResult(export_markdown(item), metadata={"skill": name, **usage([item])[0]})
    root = _skill_root(ctx, name)
    return ToolResult(root.joinpath("SKILL.md").read_text(encoding="utf-8"), metadata={"skill": name})


@tool(read_only=True)
def skill_resource_read(ctx: ToolContext, name: str, resource: str) -> ToolResult:
    """Read a skill resource with an explicit references/ or templates/ path."""
    relative = Path(resource)
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative.parts[0] not in {"references", "templates"}):
        return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
    if ctx.skill_snapshot is not None:
        item = ctx.skill_snapshot["skills"].get(name)
        content = item.get("resources", {}).get(relative.as_posix()) if item and item["enabled"] else None
        if content is None:
            return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
        return ToolResult(content, metadata={"skill": name, "resource": resource,
                                            "version": item["version"], "base_release": item.get("base_release")})
    root = _skill_root(ctx, name)
    directory = (root / relative.parts[0]).resolve()
    target = (root / relative).resolve()
    if (not directory.is_relative_to(root) or not target.is_relative_to(directory)
            or not target.is_file()):
        return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
    return ToolResult(target.read_text(encoding="utf-8"), metadata={"skill": name, "resource": resource})
