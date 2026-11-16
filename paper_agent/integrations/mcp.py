"""Configured MCP tools, discovered dynamically and scoped to one agent run."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from paper_agent.tools.core import ToolContext, ToolResult, ToolSpec


@dataclass(frozen=True)
class ServerConfig:
    name: str
    options: dict[str, Any] = field(repr=False)
    directory: Path


def load_servers(
    path: str | Path | None = None, *, phase: str
) -> tuple[ServerConfig, ...]:
    """Only read the user-owned config; never execute workspace-provided config."""
    explicit = path is not None or "PAPER_AGENT_MCP_CONFIG" in os.environ
    path = Path(
        path
        or os.environ.get("PAPER_AGENT_MCP_CONFIG", "~/.config/paper-agent/mcp.json")
    ).expanduser()
    if not path.exists() and not explicit:
        return ()
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"mcpServers"}:
        raise ValueError("MCP config must contain an mcpServers object")
    servers = document["mcpServers"]
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")
    result = []
    allowed = {
        "transport",
        "url",
        "headers",
        "bearer_token_file",
        "command",
        "args",
        "env",
        "cwd",
        "timeout",
        "enabled",
        "phases",
        "allow_tools",
    }
    for name, options in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError(
                "MCP server names must use 1..32 letters, digits, underscores or hyphens"
            )
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError(f"MCP server {name}: unsupported configuration fields")
        if type(options.get("enabled", True)) is not bool:
            raise ValueError(f"MCP server {name}: enabled must be boolean")
        for key in ("phases", "allow_tools", "args"):
            if key in options and (
                not isinstance(options[key], list)
                or any(not isinstance(v, str) for v in options[key])
            ):
                raise ValueError(f"MCP server {name}: {key} must be a list of strings")
        transport = options.get(
            "transport", "stdio" if "command" in options else "http"
        )
        if transport not in {"http", "stdio"}:
            raise ValueError(
                f"MCP server {name}: supported transports are http and stdio"
            )
        if transport == "stdio":
            if not isinstance(options.get("command"), str) or not options["command"]:
                raise ValueError(f"MCP server {name}: command is required")
            if any(key in options for key in ("url", "headers", "bearer_token_file")):
                raise ValueError(
                    f"MCP server {name}: HTTP options cannot be used with stdio"
                )
        else:
            url = options.get("url")
            if not isinstance(url, str):
                raise ValueError(f"MCP server {name}: url is required")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError(f"MCP server {name}: invalid HTTP endpoint")
            if any(key in options for key in ("command", "args", "env", "cwd")):
                raise ValueError(
                    f"MCP server {name}: stdio options cannot be used with HTTP"
                )
        for key in ("headers", "env"):
            value = options.get(key, {})
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in value.items()
            ):
                raise ValueError(
                    f"MCP server {name}: {key} must map strings to strings"
                )
        for key in ("cwd", "bearer_token_file"):
            if key in options and (
                not isinstance(options[key], str) or not options[key]
            ):
                raise ValueError(f"MCP server {name}: {key} must be a nonempty path")
        timeout = options.get("timeout", 90)
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(f"MCP server {name}: timeout must be finite and positive")
        if not options.get("enabled", True) or (
            "phases" in options and phase not in options["phases"]
        ):
            continue
        if options.get("allow_tools") == []:
            continue
        result.append(
            ServerConfig(
                name, {**options, "transport": transport}, path.resolve().parent
            )
        )
    return tuple(result)


def _expand(value: str) -> str:
    def substitute(match):
        key = match.group(1)
        if key not in os.environ:
            raise ValueError(f"MCP environment variable is missing: {key}")
        return os.environ[key]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)


def _path(server: ServerConfig, value: str) -> Path:
    path = Path(_expand(value)).expanduser()
    return path if path.is_absolute() else server.directory / path


def tool_name(server: str, remote: str) -> str:
    """Stable model-compatible names, including for long/non-ASCII remote names."""
    raw = f"{server}__{remote}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if clean == raw and len(clean) <= 64:
        return clean
    return clean[:51] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def convert_result(answer, *, server: str, name: str) -> ToolResult:
    parts, images = [], []
    for item in answer.content:
        block = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        if item.type == "text":
            if answer.structuredContent is not None:
                try:
                    if json.loads(item.text) == answer.structuredContent:
                        continue
                except ValueError:
                    pass
            parts.append(item.text)
        elif item.type == "image":
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{item.mimeType};base64,{item.data}"},
                }
            )
        else:
            # Preserve resources/audio/other content in the textual receipt.
            parts.append(json.dumps(block, ensure_ascii=False))
    if answer.structuredContent is not None:
        parts.insert(0, json.dumps(answer.structuredContent, ensure_ascii=False))
    return ToolResult(
        "\n".join(parts),
        bool(answer.isError),
        {"transport": "mcp", "server": server, "tool": name},
        images,
        answer.structuredContent,
    )


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return ", ".join(dict.fromkeys(_error_kind(item) for item in exc.exceptions))
    response = getattr(exc, "response", None)
    status = f" HTTP {response.status_code}" if response is not None else ""
    return type(exc).__name__ + status


class MCPManager:
    """Each SDK session lives in its own task, including all cancel scopes.

    Transport failures must not cancel the model loop. Requests cross a queue;
    only the session's owner task enters/exits SDK context managers.
    """

    def __init__(self, servers: tuple[ServerConfig, ...], *, allow_unavailable: bool = False):
        self.servers = servers
        self.allow_unavailable = allow_unavailable
        self.unavailable: list[str] = []
        self.tools: tuple[ToolSpec, ...] = ()
        self._workers: list[asyncio.Task] = []

    @classmethod
    def from_config(cls, *, phase: str, path: str | Path | None = None,
                    allow_unavailable: bool = False):
        return cls(load_servers(path, phase=phase), allow_unavailable=allow_unavailable)

    async def __aenter__(self):
        if not self.servers:
            return self
        try:
            import mcp
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:
            raise RuntimeError(
                'MCP support requires: pip install -e ".[mcp]"'
            ) from None
        specs = []
        try:
            for server in self.servers:
                ready = asyncio.get_running_loop().create_future()
                queue = asyncio.Queue()
                worker = asyncio.create_task(
                    self._serve(server, queue, ready), name=f"mcp:{server.name}"
                )
                self._workers.append(worker)
                try:
                    discovered = await ready
                except Exception as exc:
                    if not self.allow_unavailable:
                        raise
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    self._workers.remove(worker)
                    # Do not expose exception messages: URLs/headers can contain credentials.
                    self.unavailable.append(
                        f"MCP server {server.name}: connection/discovery failed ({_error_kind(exc)})")
                    continue
                allow = server.options.get("allow_tools")
                if allow is not None and set(allow) - {r.name for r in discovered}:
                    raise ValueError("Configured MCP tool is not advertised by server")
                for remote in discovered:
                    if allow is None or remote.name in allow:
                        specs.append(self._spec(server, worker, queue, remote))
            if len({spec.name for spec in specs}) != len(specs):
                raise ValueError("MCP tool name collision")
            self.tools = tuple(specs)
            return self
        except BaseException as exc:
            await self.__aexit__(None, None, None)
            if not isinstance(exc, Exception):
                raise
            # SDK/HTTP errors may contain credential-bearing URLs or headers.
            raise RuntimeError(
                f"MCP server {server.name}: connection/discovery failed ({_error_kind(exc)})"
            ) from None

    async def _serve(self, server, queue, ready):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx

        current = None
        try:
            async with AsyncExitStack() as stack:
                options, timeout = server.options, server.options.get("timeout", 90)
                if options["transport"] == "stdio":
                    parameters = StdioServerParameters(
                        command=_expand(options["command"]),
                        args=[_expand(arg) for arg in options.get("args", [])],
                        env={k: _expand(v) for k, v in options.get("env", {}).items()},
                        cwd=_path(server, options["cwd"]) if "cwd" in options else None,
                    )
                    streams = await stack.enter_async_context(stdio_client(parameters))
                else:
                    headers = {
                        k: _expand(v) for k, v in options.get("headers", {}).items()
                    }
                    if "bearer_token_file" in options:
                        token = (
                            _path(server, options["bearer_token_file"])
                            .read_text()
                            .strip()
                        )
                        if not token:
                            raise ValueError("Empty MCP bearer token file")
                        headers["Authorization"] = "Bearer " + token
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=headers,
                            timeout=httpx.Timeout(timeout),
                            trust_env=False,
                            follow_redirects=False,
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(options["url"], http_client=client)
                    )
                session = await stack.enter_async_context(
                    ClientSession(
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(seconds=timeout),
                    )
                )
                await session.initialize()
                cursor, seen_cursors, names = None, set(), set()
                discovered = []
                while True:
                    page = await session.list_tools(cursor=cursor)
                    for remote in page.tools:
                        if remote.name in names:
                            raise ValueError("Duplicate MCP tool name")
                        names.add(remote.name)
                        discovered.append(remote)
                    cursor = page.nextCursor
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise ValueError("Repeated MCP tools/list pagination cursor")
                    seen_cursors.add(cursor)
                ready.set_result(discovered)
                while True:
                    name, arguments, current = await queue.get()
                    if current.cancelled():
                        current = None
                        continue
                    try:
                        answer = await session.call_tool(name, arguments)
                    except Exception as exc:
                        if not current.done():
                            current.set_exception(exc)
                    else:
                        if not current.done():
                            current.set_result(answer)
                    current = None
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            if current is not None and not current.done():
                current.set_exception(exc)
        finally:
            while not queue.empty():
                _, _, pending = queue.get_nowait()
                if not pending.done():
                    pending.set_exception(RuntimeError("MCP connection closed"))

    def _spec(self, server, worker, queue, remote):
        async def handler(ctx: ToolContext, **arguments):
            try:
                if worker.done():
                    raise RuntimeError("MCP connection closed")
                future = asyncio.get_running_loop().create_future()
                # Queue synchronously so a failing worker cannot miss this future.
                queue.put_nowait((remote.name, arguments, future))
                # One call only. A timeout may follow execution; never replay it.
                answer = await future
                return convert_result(answer, server=server.name, name=remote.name)
            except Exception as exc:
                return ToolResult(
                    f"MCP server {server.name}, tool {remote.name}: call failed "
                    f"({_error_kind(exc)}); outcome may be unknown; request was not retried.",
                    True,
                    {"transport": "mcp", "server": server.name, "tool": remote.name},
                )

        return ToolSpec(
            tool_name(server.name, remote.name),
            remote.description or remote.name,
            handler,
            bool(remote.annotations and remote.annotations.readOnlyHint),
            copy.deepcopy(remote.inputSchema),
        )

    async def __aexit__(self, *exc_info):
        for worker in self._workers:
            worker.cancel()
        try:
            await asyncio.gather(*self._workers, return_exceptions=True)
        finally:
            self._workers.clear()
            self.tools = ()


def main():
    """Inspect discovered tools without asking a model to call them."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--phase", default="experiment_bridge")
    args = parser.parse_args()

    async def discover():
        async with MCPManager.from_config(
            phase=args.phase, path=args.config
        ) as manager:
            print(
                json.dumps(
                    [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                            "readOnlyHint": t.read_only,
                        }
                        for t in manager.tools
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )

    asyncio.run(discover())


if __name__ == "__main__":
    main()
