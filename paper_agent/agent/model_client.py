"""Small model adapter with a provider-neutral tool-calling response."""

from __future__ import annotations

import json
import os
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


def _transient_request_error(exc: Exception) -> bool:
    import httpx
    from openai import APIConnectionError, APIStatusError

    if isinstance(exc, (APIConnectionError, httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    # Recent SDK versions use httpx2; streaming failures can escape SDK wrapping.
    try:
        import httpx2
    except ImportError:
        pass
    else:
        if isinstance(exc, httpx2.TransportError):
            return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or exc.status_code >= 500
    return (
        "upstream http/2 stream failed" in str(exc).lower()
        or "stream ended without a completion marker" in str(exc).lower())


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None


class InvalidToolCall(ValueError):
    """The provider returned an incomplete function call; nothing was executed."""


class OutputLimitExceeded(RuntimeError):
    """The response reached its output limit; partial tool calls must not run."""


def _stream_response(stream: Any, *, deadline: float | None = None) -> ModelResponse:
    """Assemble deltas before exposing any tool call to the execution loop."""
    content, calls = [], {}
    finished = False
    try:
        for chunk in stream:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Model connection retry window exhausted (300 seconds)")
            for choice in chunk.choices:
                if choice.index != 0:
                    continue
                delta = choice.delta
                if delta.content:
                    content.append(delta.content)
                for call in delta.tool_calls or ():
                    item = calls.setdefault(call.index, {
                        "id": "", "function": {"name": "", "arguments": ""}})
                    if call.id:
                        item["id"] += call.id
                    if call.function:
                        item["function"]["name"] += call.function.name or ""
                        item["function"]["arguments"] += call.function.arguments or ""
                if choice.finish_reason is not None:
                    if choice.finish_reason == "length":
                        raise OutputLimitExceeded("Model stream incomplete: finish_reason=length")
                    if choice.finish_reason not in {"stop", "tool_calls"}:
                        raise RuntimeError(f"Model stream incomplete: finish_reason={choice.finish_reason}")
                    finished = True
        if not finished:
            raise RuntimeError("Model stream ended without a completion marker; no tools were executed")
        try:
            return _normalise_response({"content": "".join(content),
                                        "tool_calls": [calls[i] for i in sorted(calls)]})
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidToolCall(f"Invalid streamed tool call: {exc}") from exc
    finally:
        stream.close()


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Load simple KEY=VALUE entries without overriding shell environment."""
    env_path = os.fspath(path)
    try:
        with open(env_path, encoding="utf-8") as env_file:
            lines = env_file.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _normalise_response(value: Any) -> ModelResponse:
    if isinstance(value, ModelResponse):
        return value
    if isinstance(value, str):
        return ModelResponse(content=value)
    if isinstance(value, Mapping):
        calls = []
        for index, call in enumerate(value.get("tool_calls") or ()):
            function = call.get("function", call)
            if not isinstance(function, Mapping) or not function.get("name"):
                raise InvalidToolCall("Tool call is missing its function name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            calls.append(ToolCall(str(call.get("id", f"call-{index}")),
                                  function["name"], dict(arguments)))
        return ModelResponse(str(value.get("content", "") or ""), tuple(calls), value)
    # This also handles an OpenAI ChatCompletion-like object without importing SDKs.
    choice = getattr(value, "choices", [None])[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise OutputLimitExceeded("Model response incomplete: finish_reason=length")
    message = getattr(getattr(value, "choices", [None])[0], "message", None)
    if message is None:
        raise TypeError(f"unsupported model response: {type(value)!r}")
    calls = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or ()):
        function = getattr(call, "function", None)
        if function is None or not getattr(function, "name", None):
            raise InvalidToolCall("Tool call is missing its function name")
        arguments = function.arguments
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(ToolCall(str(getattr(call, "id", f"call-{index}")),
                              function.name, dict(arguments)))
    return ModelResponse(getattr(message, "content", "") or "", tuple(calls), value)


class ModelClient:
    """OpenAI-compatible client whose transport can be replaced in tests."""

    def __init__(self, model: str = "gpt-4o", *, client: Any = None,
                 responder: Callable[..., Any] | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float | None = None):
        # Keep credentials out of project files.  The explicit arguments win,
        # followed by the paper-agent namespaced variables and OpenAI defaults.
        self.model = model or os.environ.get("PAPER_AGENT_MODEL", "gpt-4o")
        configured_timeout = timeout if timeout is not None else os.environ.get(
            "PAPER_AGENT_TIMEOUT_SECONDS", "120")
        try:
            self.timeout = float(configured_timeout)
        except (TypeError, ValueError):
            self.timeout = 120.0
        self._responder = responder
        if responder is not None:
            self.client = client
            return
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("install openai or inject a model client/responder") from exc
            api_key = api_key or os.environ.get("PAPER_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY")
            base_url = base_url or os.environ.get("PAPER_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            if not api_key or api_key.strip().lower() in {"your-api-key", "your_api_key", "changeme"}:
                raise RuntimeError(
                    "PAPER_AGENT_API_KEY is not configured. Copy .env.example to .env "
                    "and replace the placeholder with a valid API token."
                )
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout, max_retries=0)
        self.client = client

    def _request_response(self, request: dict[str, Any]) -> ModelResponse:
        """Retry the same model request; partial streams never reach tool execution."""
        deadline = None
        last_error = None
        retries = 0
        while True:
            options = dict(request)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Model connection retry window exhausted (300 seconds)") from last_error
                options["timeout"] = min(self.timeout, remaining)
            try:
                response = self.client.chat.completions.create(**options)
                if options["stream"] and not isinstance(response, Mapping) and not hasattr(response, "choices"):
                    return _stream_response(response, deadline=deadline)
                return _normalise_response(response)
            except Exception as exc:
                if not _transient_request_error(exc):
                    raise
                last_error = exc
                if deadline is None:
                    deadline = time.monotonic() + 300
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Model connection retry window exhausted (300 seconds)") from exc
                delay = min(2 ** min(retries + 1, 4), 15, remaining)
                retries += 1
                logging.getLogger(__name__).warning(
                    "模型连接失败，%.1f 秒后重试（第 %d 次，剩余重试窗口 %.1f 秒）：%s",
                    delay, retries, remaining, exc)
                time.sleep(delay)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 *, temperature: float = 0) -> ModelResponse:
        if self._responder is not None:
            return _normalise_response(self._responder(messages=messages, tools=tools,
                                                       model=self.model, temperature=temperature))
        streaming = os.environ.get("PAPER_AGENT_STREAM", "true").lower() not in {"0", "false", "no"}
        request = {"model": self.model, "messages": messages,
                   "temperature": temperature, "timeout": self.timeout, "stream": streaming}
        configured_max_tokens = os.environ.get("PAPER_AGENT_MAX_TOKENS")
        if configured_max_tokens:
            try:
                max_tokens = int(configured_max_tokens)
            except ValueError:
                max_tokens = 0
            if max_tokens > 0:
                request["max_tokens"] = max_tokens
        if tools:
            request.update(tools=tools, tool_choice="auto")
        for attempt in range(3):
            try:
                return self._request_response(request)
            except OutputLimitExceeded as exc:
                if attempt == 2:
                    raise OutputLimitExceeded(
                        "Model output was truncated (finish_reason=length) after 3 attempts; "
                        "no partial tool calls were executed. Set PAPER_AGENT_MAX_TOKENS "
                        "to an output budget supported by your provider and resume this node."
                    ) from exc
                request["messages"] = messages + [{
                    "role": "user",
                    "content": "Your previous response reached the output token limit and was discarded; "
                    "none of its tool calls were executed. Return a much shorter response. "
                    "Use small, complete tool calls and write long reports in multiple file edits "
                    "across turns. Do not repeat a full report or large batches of tool calls in one response.",
                }]
            except InvalidToolCall:
                if attempt == 2:
                    raise
                request["messages"] = messages + [{
                    "role": "user",
                    "content": "Your last response contained an incomplete tool call and was not executed. "
                    "Return a valid function tool call with a function name and JSON object arguments, "
                    "or a normal text response if no tool is needed.",
                }]
