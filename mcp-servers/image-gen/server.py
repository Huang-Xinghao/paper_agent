#!/usr/bin/env python3
"""Small project-local MCP bridge for OpenAI image generation.

The bridge exposes start/status behind the agent-visible generate_image tool.
The workflow runner validates receipts and outputs; it does not generate images.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"


def _reply(request_id: Any, result: dict[str, Any] | None = None,
           error: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error:
        response["error"] = {"code": -32000, "message": error}
    else:
        response["result"] = result or {}
    return response


def _tool_result(payload: dict[str, Any], request_id: Any) -> dict[str, Any]:
    return _reply(request_id, {"structuredContent": payload, "content": [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
    ]})


def _workspace(raw: str | None) -> Path:
    root = Path(raw or os.getcwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _start(args: dict[str, Any]) -> dict[str, Any]:
    root = _workspace(args.get("cwd"))
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("prompt is required")
    output = (root / str(args.get("outputPath", "figures/ai_generated/figure1.png"))).resolve()
    allowed = (root / "figures").resolve()
    if allowed not in output.parents and output != allowed:
            raise ValueError("outputPath must be under figures")
    output.parent.mkdir(parents=True, exist_ok=True)
    job_id = "img-" + uuid.uuid4().hex[:12]
    try:
        from openai import OpenAI
        api_key = os.environ.get("PAPER_AGENT_IMAGE_API_KEY") or os.environ.get("PAPER_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("PAPER_AGENT_IMAGE_API_KEY or PAPER_AGENT_API_KEY is not configured")
        client = OpenAI(api_key=api_key, base_url=os.environ.get("PAPER_AGENT_IMAGE_BASE_URL") or os.environ.get("PAPER_AGENT_BASE_URL"))
        response = client.images.generate(
            model=os.environ.get("PAPER_AGENT_IMAGE_MODEL", "gpt-image-2"),
            prompt=prompt,
            size=str(args.get("size", "1536x1024")),
            quality=str(args.get("quality", "high")),
        )
        encoded = getattr(response.data[0], "b64_json", None)
        if not encoded:
            raise RuntimeError("image API returned no b64_json image")
        output.write_bytes(base64.b64decode(encoded))
    except Exception as exc:
        receipt = {"jobId": job_id, "status": "failed", "error": str(exc), "outputPath": str(output)}
        (root / ".paper-agent" / "image-jobs").mkdir(parents=True, exist_ok=True)
        (root / ".paper-agent" / "image-jobs" / f"{job_id}.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt
    receipt = {"jobId": job_id, "status": "done", "outputPath": str(output.relative_to(root)), "model": os.environ.get("PAPER_AGENT_IMAGE_MODEL", "gpt-image-2")}
    (root / ".paper-agent" / "image-jobs").mkdir(parents=True, exist_ok=True)
    (root / ".paper-agent" / "image-jobs" / f"{job_id}.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        method, request_id = request.get("method"), request.get("id")
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "paper-agent-image-gen", "version": "0.1.0"}}
                print(json.dumps(_reply(request_id, result)), flush=True)
            elif method == "notifications/initialized":
                continue
            elif method == "tools/call":
                params = request.get("params", {})
                name, args = params.get("name"), params.get("arguments") or {}
                if name == "generate_start":
                    payload = _start(args)
                elif name == "generate_status":
                    root = _workspace(args.get("cwd"))
                    path = root / ".paper-agent" / "image-jobs" / f"{args.get('jobId')}.json"
                    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"jobId": args.get("jobId"), "status": "unknown"}
                else:
                    raise ValueError(f"unknown tool: {name}")
                print(json.dumps(_tool_result(payload, request_id), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps(_reply(request_id, error=str(exc))), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
