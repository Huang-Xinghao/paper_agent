"""Small model adapter with a provider-neutral tool-calling response."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


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
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout)
        self.client = client

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 *, temperature: float = 0) -> ModelResponse:
        if self._responder is not None:
            return _normalise_response(self._responder(messages=messages, tools=tools,
                                                       model=self.model, temperature=temperature))
        request = {"model": self.model, "messages": messages,
                   "temperature": temperature, "timeout": self.timeout}
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
            response = self.client.chat.completions.create(**request)
            try:
                return _normalise_response(response)
            except InvalidToolCall:
                if attempt == 2:
                    raise
                request["messages"] = messages + [{
                    "role": "user",
                    "content": "Your last response contained an incomplete tool call and was not executed. "
                    "Return a valid function tool call with a function name and JSON object arguments, "
                    "or a normal text response if no tool is needed.",
                }]
