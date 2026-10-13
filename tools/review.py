"""Codex-backed independent reviewer used by the workflow runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from paper_agent.mcp.client import call_codex

from .core import ToolContext, ToolResult, tool


class CodexReviewer:
    """Small adapter that gives the workflow one deterministic review call."""

    def __init__(self, *, model: str | None = None, timeout: float | None = None,
                 sandbox: str = "read-only"):
        self.model = model or os.environ.get("PAPER_AGENT_REVIEW_MODEL", "gpt-6-astra")
        # Idea reports can be large and the reviewer runs with xhigh reasoning.
        # Keep the bridge alive long enough for a complete independent review;
        # callers can still lower this explicitly through the environment or
        # the constructor.
        configured_timeout = timeout if timeout is not None else os.environ.get(
            "PAPER_AGENT_REVIEW_TIMEOUT", "600")
        try:
            self.timeout = float(configured_timeout)
        except (TypeError, ValueError):
            self.timeout = 600.0
        self.sandbox = sandbox

    def review(self, workspace: Path, prompt: str) -> dict[str, Any]:
        return call_codex(workspace, prompt, model=self.model,
                          config={"model_reasoning_effort": os.environ.get(
                              "PAPER_AGENT_REVIEW_REASONING_EFFORT", "xhigh")},
                          sandbox=self.sandbox, timeout=self.timeout)


@tool()
def external_review(ctx: ToolContext, prompt: str, model: str | None = None,
                    system: str | None = None) -> ToolResult:
    """Send a review brief through the bundled ARIS-style Codex MCP bridge."""
    brief = f"{system}\n\n{prompt}" if system else prompt
    try:
        payload = CodexReviewer(model=model).review(ctx.workspace, brief)
    except Exception as exc:
        return ToolResult(f"External review failed: {exc}", True,
                          {"error_code": "EXTERNAL_REVIEW_FAILED", "provider": "codex-mcp"})
    content = payload.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return ToolResult("External review failed: reviewer returned an empty response", True,
                          {"error_code": "EXTERNAL_REVIEW_EMPTY", "provider": "codex-mcp"})
    return ToolResult(json.dumps({"model": model or os.environ.get(
        "PAPER_AGENT_REVIEW_MODEL", "gpt-6-astra"), "provider": "codex-mcp",
        "threadId": payload.get("threadId"), "response": content}, ensure_ascii=False),
                      metadata={"provider": "codex-mcp"})


__all__ = ["CodexReviewer", "external_review"]
