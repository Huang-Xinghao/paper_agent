"""Agent-visible image generation tool backed by the project MCP server."""
from __future__ import annotations

import hashlib
import json

from .core import ToolContext, ToolResult, tool


@tool()
def generate_image(ctx: ToolContext, prompt: str,
                   output_path: str = "figures/ai_generated/figure.png",
                   size: str = "1536x1024", quality: str = "high") -> ToolResult:
    """Generate a PNG through the configured image-generation MCP service.

    The output must live below figures/; the response is a structured receipt
    and never claims success unless a valid PNG was written.
    """
    try:
        target = ctx.resolve(output_path, write=True)
    except ValueError:
        return ToolResult("output_path must be inside figures/", True,
                          {"error_code": "IMAGE_OUTPUT_OUTSIDE_FIGURES"})
    figures = (ctx.workspace / "figures").resolve()
    if figures not in target.parents:
        return ToolResult("output_path must be inside figures/", True,
                          {"error_code": "IMAGE_OUTPUT_OUTSIDE_FIGURES"})
    if not prompt.strip():
        return ToolResult("prompt is required", True, {"error_code": "IMAGE_PROMPT_EMPTY"})
    try:
        from paper_agent.mcp.image_client import ImageMCPClient
        receipt = ImageMCPClient(ctx.workspace).generate_start(
            prompt, output_path=str(target.relative_to(ctx.workspace.resolve())), size=size, quality=quality)
    except Exception as exc:
        return ToolResult(json.dumps({"status": "failed", "error": str(exc)}), True,
                          {"error_code": "IMAGE_MCP_FAILED"})
    if receipt.get("status") != "done":
        return ToolResult(json.dumps(receipt, ensure_ascii=False), True,
                          {"error_code": "IMAGE_GENERATION_FAILED"})
    if not target.is_file() or target.stat().st_size < 100 or target.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        return ToolResult(json.dumps({"status": "failed", "error": "MCP returned no valid PNG", "receipt": receipt}), True,
                          {"error_code": "IMAGE_INVALID_OUTPUT"})
    receipt = {**receipt, "status": "completed", "output_path": str(target.relative_to(ctx.workspace.resolve())),
               "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
               "size": size, "quality": quality}
    return ToolResult(json.dumps(receipt, ensure_ascii=False), False,
                      {"output_path": receipt["output_path"], "model": receipt.get("model")})
