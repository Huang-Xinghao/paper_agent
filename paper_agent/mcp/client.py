"""Minimal stdio MCP client for the bundled ARIS-style Codex bridge."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CODEX_SERVER = Path(__file__).resolve().parents[2] / "mcp-servers" / "codex-exec" / "server.py"
PROTOCOL_VERSION = "2024-11-05"


def _codex_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    for directory in (".vscode-server", ".vscode"):
        extensions = Path.home() / directory / "extensions"
        candidates = sorted(extensions.glob("openai.chatgpt-*/bin/*/codex"), reverse=True)
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    raise RuntimeError("Codex reviewer executable not found. Install codex on PATH or set CODEX_BIN to its executable path.")


def _request(method: str, request_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def call_codex(root: Path, prompt: str, *, model: str | None = None,
               config: dict[str, Any] | None = None, sandbox: str = "read-only",
               timeout: float | None = None, server: Path = CODEX_SERVER) -> dict[str, Any]:
    """Run one Codex turn through the local MCP server and return its payload."""
    return _call(root, "codex", {"prompt": prompt, "model": model,
                  "config": config or {}, "sandbox": sandbox,
                  "cwd": str(root.resolve())}, timeout=timeout, server=server)


def call_codex_reply(root: Path, thread_id: str, prompt: str, *, timeout: float | None = None,
                     server: Path = CODEX_SERVER) -> dict[str, Any]:
    """Continue an existing Codex thread."""
    return _call(root, "codex-reply", {"threadId": thread_id, "prompt": prompt},
                 timeout=timeout, server=server)


def _call(root: Path, name: str, arguments: dict[str, Any], *, timeout: float | None,
          server: Path) -> dict[str, Any]:
    arguments = {key: value for key, value in arguments.items() if value is not None}
    requests = [
        _request("initialize", 1, {"protocolVersion": PROTOCOL_VERSION,
                                   "capabilities": {},
                                   "clientInfo": {"name": "paper-agent", "version": "0.1.0"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _request("tools/call", 2, {"name": name, "arguments": arguments}),
    ]
    state_dir = root.resolve() / ".paper-agent" / "codex-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_BIN"] = _codex_binary()
    env["CODEX_EXEC_STATE_DIR"] = str(state_dir)
    env["CODEX_EXEC_DEBUG_LOG"] = str(state_dir / "debug.log")
    limit = timeout if timeout is not None else float(os.environ.get("PAPER_AGENT_REVIEW_TIMEOUT", "600"))
    if limit <= 0:
        raise ValueError("MCP timeout must be greater than zero")
    try:
        result = subprocess.run([sys.executable, str(server)],
            input="\n".join(json.dumps(message, ensure_ascii=False) for message in requests) + "\n",
            text=True, capture_output=True, cwd=root, env=env, timeout=limit, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"MCP {name} request timed out after {limit:g}s") from exc
    if result.returncode:
        raise RuntimeError(f"MCP server exited {result.returncode}: {result.stderr.strip()[-2000:]}")
    messages: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if line.strip():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("MCP server returned invalid JSON-RPC") from exc
            if isinstance(value, dict):
                messages.append(value)
    if not any(message.get("id") == 1 and "result" in message for message in messages):
        raise RuntimeError("MCP initialize failed")
    response = next((message for message in messages if message.get("id") == 2), None)
    if response is None:
        raise RuntimeError("MCP tool returned no response")
    if "error" in response:
        raise RuntimeError(str(response["error"]))
    body = response.get("result") or {}
    if body.get("isError"):
        parts = body.get("content") or []
        raise RuntimeError(str(parts[0].get("text") if parts else "MCP tool failed"))
    payload = body.get("structuredContent")
    if isinstance(payload, dict):
        return payload
    parts = body.get("content") or []
    if parts and parts[0].get("type") == "text":
        try:
            payload = json.loads(parts[0]["text"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MCP tool returned invalid JSON payload") from exc
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("MCP tool returned no structured payload")


__all__ = ["CODEX_SERVER", "call_codex", "call_codex_reply"]
