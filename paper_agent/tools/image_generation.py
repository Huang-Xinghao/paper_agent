"""Agent-visible image generation tool backed by the project MCP server."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import ToolContext, ToolResult, tool

IMAGE_FAILURE_LIMIT = 5
IMAGE_ATTEMPTS_PATH = ".paper-agent/image-generation.json"


def image_generation_state(workspace: Path) -> dict:
    path = workspace / IMAGE_ATTEMPTS_PATH
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"failures": []}


def _save_state(workspace: Path, state: dict) -> None:
    path = workspace / IMAGE_ATTEMPTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _generation_failure(ctx: ToolContext, state: dict, error: str, code: str) -> ToolResult:
    state["failures"].append({"error": error})
    _save_state(ctx.workspace, state)
    return _failure_result(state, code)


def _failure_result(state: dict, code: str = "IMAGE_FALLBACK_REQUIRED") -> ToolResult:
    count = len(state["failures"])
    fallback = count >= IMAGE_FAILURE_LIMIT
    payload = {
        "status": "fallback_required" if fallback else "failed",
        "failure_count": count,
        "max_failures": IMAGE_FAILURE_LIMIT,
        "error": state["failures"][-1]["error"],
    }
    if fallback:
        payload["next_action"] = (
            "Stop calling the image API. Draw Figure 1 with code using the same figure guidelines; "
            "save the Python source, PDF and PNG under figures/. Write figures/figure_1_fallback.json "
            "with backend='code', source_path, pdf_path, output_path and source_sha256, pdf_sha256, "
            "sha256 for those files. Choose filenames under figures/ and declare the same PDF as figure_1.asset "
            "and PNG as figure_1.preview in figures/figure_manifest.json. "
            "Include the PDF in figures/latex_includes.tex and visually review it using the PNG preview "
            "with view_image and figures/visual_checks.json before finishing."
        )
    return ToolResult(json.dumps(payload, ensure_ascii=False), True,
                      {"error_code": "IMAGE_FALLBACK_REQUIRED" if fallback else code})


@tool()
def generate_image(ctx: ToolContext, prompt: str,
                   output_path: str = "figures/ai_generated/figure.png",
                   size: str = "1536x1324", quality: str = "high") -> ToolResult:
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
    state = image_generation_state(ctx.workspace)
    if len(state["failures"]) >= IMAGE_FAILURE_LIMIT:
        return _failure_result(state)
    try:
        from paper_agent.mcp.image_client import ImageMCPClient
        receipt = ImageMCPClient(ctx.workspace).generate_start(
            prompt, output_path=str(target.relative_to(ctx.workspace.resolve())), size=size, quality=quality)
    except Exception as exc:
        return _generation_failure(ctx, state, str(exc), "IMAGE_MCP_FAILED")
    if receipt.get("status") != "done":
        return _generation_failure(ctx, state, str(receipt.get("error", receipt)), "IMAGE_GENERATION_FAILED")
    if not target.is_file() or target.stat().st_size < 100 or target.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        return _generation_failure(ctx, state, "MCP returned no valid PNG", "IMAGE_INVALID_OUTPUT")
    _save_state(ctx.workspace, {"failures": []})
    receipt = {**receipt, "status": "completed", "output_path": str(target.relative_to(ctx.workspace.resolve())),
               "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
               "size": size, "quality": quality}
    return ToolResult(json.dumps(receipt, ensure_ascii=False), False,
                      {"output_path": receipt["output_path"], "model": receipt.get("model")})
