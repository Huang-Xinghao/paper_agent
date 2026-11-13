"""Client for the project-local image generation MCP bridge."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parents[2] / "mcp-servers" / "image-gen" / "server.py"


class ImageMCPClient:
    def __init__(self, project: Path, *, timeout: float = 600):
        self.project = project.resolve()
        self.timeout = timeout

    def generate_start(self, prompt: str, *, output_path: str = "figures/ai_generated/figure1.png",
                       size: str = "1536x1024", quality: str = "high") -> dict[str, Any]:
        return call(self.project, "generate_start", {
            "prompt": prompt, "cwd": str(self.project), "outputPath": output_path,
            "size": size, "quality": quality,
        }, timeout=self.timeout, server=SERVER)

    def generate_status(self, job_id: str) -> dict[str, Any]:
        return call(self.project, "generate_status", {
            "jobId": job_id, "cwd": str(self.project),
        }, timeout=self.timeout, server=SERVER)


def call(root: Path, name: str, arguments: dict[str, Any], *,
         timeout: float, server: Path) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP timeout must be finite and greater than zero")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "paper-agent-image", "version": "0.1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
    ]
    try:
        result = subprocess.run([sys.executable, str(server)], cwd=root,
            input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n",
            text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Image MCP {name} timed out after {timeout:g}s") from exc
    if result.returncode:
        raise RuntimeError(f"Image MCP server exited {result.returncode}: {result.stderr[-2000:]}")
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if not any(item.get("id") == 1 and "result" in item for item in messages):
        raise RuntimeError("Image MCP initialize failed")
    response = next((item for item in messages if item.get("id") == 2), None)
    if response is None:
        raise RuntimeError("Image MCP tool returned no response")
    if "error" in response:
        raise RuntimeError(str(response["error"]))
    body = response.get("result") or {}
    if body.get("isError"):
        raise RuntimeError(str(body.get("content")))
    payload = body.get("structuredContent")
    if not isinstance(payload, dict):
        raise RuntimeError("Image MCP tool returned no structured payload")
    return payload
