"""MCP HTTP 协议、权限与管理边界；服务使用官方 SDK，数据全在隔离目录。"""

import asyncio
import importlib
import json
import socket
import threading
import time

import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ImageContent, ToolAnnotations

from paper_agent.mcp import registry, runtime, network
from paper_agent.tools.core import ToolContext

api = importlib.import_module("paper_agent.api.app")


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "metadata"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(tmp_path / "projects"))
    monkeypatch.setenv("PAPER_AGENT_MCP_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("PAPER_AGENT_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("PAPER_AGENT_PUBLIC_HOSTS", raising=False)
    monkeypatch.setattr(api, "terminal_projects", lambda: set())
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def service(monkeypatch):
    state = {"calls": 0}
    server = MCPServer("acceptance")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def read_paper(query: str) -> dict:
        """Return candidate paper metadata."""
        state["calls"] += 1
        return {
            "title": query,
            "source_url": "https://example.com/paper",
            "verified": False,
        }

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    def delete_paper(name: str) -> str:
        return "must never run"

    @server.tool()
    def image_result() -> CallToolResult:
        return CallToolResult(
            content=[ImageContent(type="image", data="aA==", mimeType="image/png")]
        )

    @server.tool()
    def large_result() -> str:
        return "x" * 31000

    @server.tool()
    def remote_failure() -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="Candidate unavailable")],
            isError=True,
        )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    monkeypatch.setenv("PAPER_AGENT_MCP_TRUSTED_ENDPOINTS", json.dumps([endpoint]))
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: runner.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.02)
    assert runner.started
    yield endpoint, state, server
    runner.should_exit = True
    thread.join(5)
    sock.close()


def document(url, **changes):
    return {
        "name": "论文候选查询",
        "url": url,
        "enabled": True,
        "auth_type": "none",
        "header_name": "X-API-Key",
        "phases": ["literature", "ideas"],
        "allowed_tools": [],
        "read_only_confirmed": False,
        **changes,
    }


def create(client, url, **changes):
    response = client.post("/api/mcp", json=document(url, **changes))
    assert response.status_code == 201, response.text
    return response.json()


def update(client, item, **changes):
    values = {key: item[key] for key in document(item["url"])}
    return client.put(
        "/api/mcp/" + item["id"],
        json={**values, **changes, "expected_etag": item["etag"]},
    )


def configured(client, service, names=("read_paper",)):
    item = create(client, service[0])
    response = client.post(
        f"/api/mcp/{item['id']}/discover", json={"expected_etag": item["etag"]}
    )
    assert response.status_code == 200, response.text
    item = response.json()
    response = update(client, item, allowed_tools=list(names), read_only_confirmed=True)
    assert response.status_code == 200, response.text
    return response.json()


def test_http_sdk_discovery_call_and_result_boundaries(workbench, service, tmp_path):
    item = configured(
        workbench,
        service,
        ("read_paper", "image_result", "large_result", "remote_failure"),
    )
    assert item["protocol"]
    assert not next(t for t in item["tools"] if t["name"] == "delete_paper")[
        "compatible"
    ]
    specs = {s.source["tool"]: s for s in runtime.tool_specs("literature", tmp_path)}
    result = asyncio.run(
        specs["read_paper"].handler(ToolContext(tmp_path), query="TimesFM")
    )
    assert not result.is_error, result.output
    data = json.loads(result.output)
    assert (data["structured"] or json.loads(data["text"][0]))["title"] == "TimesFM"
    assert data["trust"] == "external_data_not_verified_paper_evidence"
    assert data["source"]["definition_sha256"]
    for name, code in [
        ("image_result", "UNSUPPORTED_CONTENT"),
        ("large_result", "RESULT_TOO_LARGE"),
    ]:
        result = asyncio.run(specs[name].handler(ToolContext(tmp_path)))
        assert result.is_error and json.loads(result.output)["code"] == code
    result = asyncio.run(specs["remote_failure"].handler(ToolContext(tmp_path)))
    assert result.is_error and result.metadata["status"] == "remote_error"
    assert service[1]["calls"] == 1
    records = workbench.get(f"/api/mcp/{item['id']}/calls").json()["items"]
    assert len(records) == 4 and all("arguments" not in entry for entry in records)


def test_secrets_validation_and_concurrent_edit(workbench):
    secret = "sensitive-never-echo-this"
    item = create(
        workbench,
        "https://example.com/mcp",
        auth_type="bearer",
        secret_action="replace",
        secret=secret,
    )
    assert secret not in json.dumps(item) and item["has_credential"]
    assert registry.headers(item["id"]) == {"Authorization": "Bearer " + secret}
    with registry.connection() as db:
        assert (
            secret not in db.execute("SELECT secret FROM mcp_connections").fetchone()[0]
        )
    changed = update(workbench, item, name="页面 A 保存").json()
    assert update(workbench, item, name="页面 B 旧版本").status_code == 409
    assert registry.get(item["id"])["name"] == "页面 A 保存"
    assert registry.headers(item["id"])["Authorization"].endswith(secret)
    response = workbench.post(
        "/api/mcp",
        json=document("https://example.com/mcp", header_name="Host", secret=secret),
    )
    assert response.status_code == 422 and secret not in response.text
    response = update(workbench, changed, secret_action="clear")
    assert response.status_code == 200 and not response.json()["has_credential"]


def test_phase_snapshot_revocation_and_reapproval(workbench, service, tmp_path):
    item = configured(workbench, service)
    with registry.use_run("test-phase-run"):
        snap = registry.snapshot("literature")
        specs = runtime.tool_specs("literature", tmp_path, snapshot=snap)
        changed = update(workbench, item, name="新阶段名称").json()
        assert registry.snapshot("literature")[0]["name"] != changed["name"]
        assert registry.snapshot("ideas")[0]["name"] == changed["name"]
        disabled = update(workbench, changed, enabled=False).json()
        result = asyncio.run(specs[0].handler(ToolContext(tmp_path), query="no call"))
        assert result.is_error and json.loads(result.output)["code"] == "REVOKED"
        assert not service[1]["calls"]
        enabled = update(workbench, disabled, enabled=True).json()
        altered = [dict(t) for t in enabled["tools"]]
        altered[0]["fingerprint"] = "changed"
        published = registry.publish(
            enabled["id"], enabled["etag"], altered, enabled["protocol"]
        )
        if altered[0]["name"] == "read_paper":
            assert "read_paper" not in published["allowed_tools"]


def test_arguments_cannot_replace_bound_tool_and_budget(workbench, service, tmp_path):
    configured(workbench, service)
    with registry.use_run("budget-run"):
        spec = runtime.tool_specs("literature", tmp_path)[0]
        result = asyncio.run(
            spec.handler(ToolContext(tmp_path), _item={"url": "http://127.0.0.1"})
        )
        # Unknown parameters never substitute the connection or bypass validation.
        assert result.is_error, result.output
        assert service[1]["calls"] == 0
        for _ in range(5):
            asyncio.run(spec.handler(ToolContext(tmp_path), query="x"))
        result = asyncio.run(spec.handler(ToolContext(tmp_path), query="x"))
        assert json.loads(result.output)["code"] == "BUDGET_EXHAUSTED"
        assert service[1]["calls"] == 5


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://169.254.169.254/mcp",
        "https://u:p@example.com/mcp",
        "https://example.com/mcp?key=secret",
        "https://example.com:8443/mcp",
    ],
)
def test_ssrf_and_secret_in_url_rejected(workbench, url):
    assert workbench.post("/api/mcp", json=document(url)).status_code == 400


def test_dns_rebinding_blocked(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()

        async def lookup(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]

        monkeypatch.setattr(loop, "getaddrinfo", lookup)
        with pytest.raises(registry.MCPError, match="受限网络"):
            await network.resolve_target("https://safe.example/mcp")

    asyncio.run(check())


def test_phase_agent_invokes_http_tool_and_handoff_removes_it(
    workbench, service, tmp_path
):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent.model_client import ModelResponse, ToolCall
    from paper_agent.agent.wiki_research import evidence_handoff

    configured(workbench, service)

    class Model:
        step = 0

        def complete(self, messages, tools):
            self.step += 1
            external = [s for s in tools if s["function"]["name"].startswith("mcp_")]
            assert len(external) == 1
            if self.step == 1:
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read",
                            external[0]["function"]["name"],
                            {"query": "TimesFM"},
                        ),
                    )
                )
            assert any(
                "external_data_not_verified_paper_evidence" in m.get("content", "")
                for m in messages
                if m["role"] == "tool"
            )
            return ModelResponse(
                content="Candidate found; original evidence still required."
            )

    agent = PhaseAgent(
        tmp_path, phase="literature", model_client=Model(), max_iterations=2
    )
    with registry.use_run("agent-phase"):
        result = agent.run(
            "Find candidate",
            phase="literature",
            system_prompt="Read only.",
            completion_check=lambda: None,
        )
    assert service[1]["calls"] == 1 and result.iterations == 2
    with evidence_handoff():
        agent.configure(phase="literature")
        assert all(not t.source for t in agent.tools)


def test_external_candidates_do_not_become_wiki_evidence(
    workbench, service, tmp_path, monkeypatch
):
    from paper_agent.agent.wiki_research import collect_wiki_evidence
    from paper_agent.agent.model_client import ModelResponse, ToolCall

    root = tmp_path / "wiki"
    (root / "papers").mkdir(parents=True)
    (root / "papers/a.md").write_text(
        "# TimesFM\n## Method\nCandidate evidence.", encoding="utf-8"
    )
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(root))
    configured(workbench, service)

    class Model:
        step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                external = next(
                    t for t in tools if t["function"]["name"].startswith("mcp_")
                )
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "external",
                            external["function"]["name"],
                            {"query": "TimesFM"},
                        ),
                    )
                )
            return ModelResponse(
                content=json.dumps(
                    {
                        "summary": "Candidate only",
                        "assessment": "insufficient",
                        "evidence": [],
                        "decisions": [],
                        "hypotheses": [],
                        "gaps": ["Need original PDF"],
                        "next_action": "Read source",
                    }
                )
            )

    with registry.use_run("wiki-collector"):
        packet = collect_wiki_evidence(
            tmp_path / "project", Model(), "TimesFM", stage="literature"
        )
    assert packet["budget"]["tool_calls"] == 2
    assert len(packet["external_candidates"]) == 1
    assert not packet["external_candidates"][0]["verified"]
    assert not packet["evidence"] and service[1]["calls"] == 1


def test_sdk_legacy_initialization_is_compatible(workbench, service, monkeypatch):
    real_client = runtime.Client

    def legacy_client(transport, **kwargs):
        kwargs["mode"] = "legacy"
        return real_client(transport, **kwargs)

    monkeypatch.setattr(runtime, "Client", legacy_client)
    item = create(workbench, service[0])
    response = workbench.post(
        f"/api/mcp/{item['id']}/discover", json={"expected_etag": item["etag"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tools"]


def test_transport_rejects_redirects_auth_and_oversize(monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse, Response

    app = FastAPI()

    @app.get("/redirect")
    def redirect():
        return RedirectResponse("http://169.254.169.254/secret")

    @app.get("/auth")
    def auth():
        return Response("sensitive service error", status_code=401)

    @app.get("/large")
    def large():
        return Response(b"x" * (network.MAX_RESPONSE + 1))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    monkeypatch.setenv(
        "PAPER_AGENT_MCP_TRUSTED_ENDPOINTS",
        json.dumps([base + path for path in ("/redirect", "/auth", "/large")]),
    )
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: runner.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.02)

    async def check():
        for path, code in [
            ("/redirect", "REDIRECT_DENIED"),
            ("/auth", "AUTH_REQUIRED"),
            ("/large", "RESULT_TOO_LARGE"),
        ]:
            async with await network.http_client(base + path, {}) as client:
                with pytest.raises(registry.MCPError) as error:
                    await client.get(base + path)
                assert error.value.code == code
                assert "sensitive" not in str(error.value)

    try:
        asyncio.run(check())
    finally:
        runner.should_exit = True
        thread.join(5)
        sock.close()


def test_pinned_backend_dials_validated_ip_not_hostname(monkeypatch):
    captured = []

    async def connect(self, host, port, *args, **kwargs):
        captured.append((host, port))
        return "stream"

    monkeypatch.setattr(network.AnyIOBackend, "connect_tcp", connect)
    backend = network.PinnedBackend("papers.example", ["8.8.8.8"])
    assert asyncio.run(backend.connect_tcp("papers.example", 443)) == "stream"
    assert captured == [("8.8.8.8", 443)]
    with pytest.raises(registry.MCPError):
        asyncio.run(backend.connect_tcp("other.example", 443))
