"""Bounded command and Skill execution helpers."""

from __future__ import annotations

import asyncio
import json
import sys
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
    """Read one file below an activated Skill's references directory."""
    root = _skill_root(ctx, name)
    target = (root / "references" / resource).resolve()
    references = (root / "references").resolve()
    if not target.is_relative_to(references) or not target.is_file():
        return ToolResult(f"Skill resource not found: {resource}", True, {"error_code": "RESOURCE_NOT_FOUND"})
    return ToolResult(target.read_text(encoding="utf-8"), metadata={"skill": name, "resource": resource})


@tool()
async def skill_script_run(ctx: ToolContext, name: str, script: str, input: dict | None = None,
                           timeout_seconds: int = 120) -> ToolResult:
    """Run a Python adapter under an activated Skill and parse JSON output."""
    root = _skill_root(ctx, name)
    target = (root / "scripts" / script).resolve()
    scripts = (root / "scripts").resolve()
    if not target.is_relative_to(scripts) or not target.is_file() or target.suffix != ".py":
        return ToolResult(f"Skill script not found: {script}", True, {"error_code": "SCRIPT_NOT_FOUND"})
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(target), cwd=root, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(input or {}).encode("utf-8")), timeout=max(1, min(timeout_seconds, 600))
        )
    except asyncio.TimeoutError:
        process.kill(); await process.wait()
        return ToolResult("Skill script timed out", True, {"error_code": "SCRIPT_TIMEOUT"})
    text = stdout.decode("utf-8", errors="replace")[-12000:]
    if process.returncode != 0:
        return ToolResult(stderr.decode("utf-8", errors="replace")[-12000:] or text, True,
                          {"error_code": "SCRIPT_FAILED", "returncode": process.returncode})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ToolResult("Skill script must output a JSON object", True, {"error_code": "INVALID_JSON"})
    if not isinstance(payload, dict):
        return ToolResult("Skill script must output a JSON object", True, {"error_code": "INVALID_JSON"})
    return ToolResult(json.dumps(payload, ensure_ascii=False), metadata={"skill": name, "script": script})
