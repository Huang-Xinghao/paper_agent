"""启动仅供浏览器验收的真实 API 和官方 SDK HTTP 服务，写入隔离 E 盘目录。"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from cryptography.fernet import Fernet
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

root = Path(os.environ["PAPER_AGENT_MCP_TEST_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ["PAPER_AGENT_WEB_DATA"] = str(root / "management")
os.environ["PAPER_AGENT_PROJECT_ROOTS"] = str(root / "projects")
os.environ["PAPER_AGENT_MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
os.environ["PAPER_AGENT_MCP_TRUSTED_ENDPOINTS"] = '["http://127.0.0.1:8003/mcp"]'
os.environ.pop("PAPER_AGENT_PASSWORD_HASH", None)
os.environ.pop("PAPER_AGENT_PUBLIC_HOSTS", None)
server = MCPServer("Paper candidate test")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def search_papers(query: str) -> dict:
    """查询论文候选；必须补读原文确认方法适用条件。"""
    return {"title": query, "verified": False}


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def delete_notes(name: str) -> str:
    """删除资料；首版不应允许选择。"""
    return "never"


runner = uvicorn.Server(
    uvicorn.Config(
        server.streamable_http_app(stateless_http=True, json_response=True),
        host="127.0.0.1",
        port=8003,
        log_level="error",
    )
)
threading.Thread(target=runner.run, daemon=True).start()
import importlib

api = importlib.import_module("paper_agent.api.app")
api.terminal_projects = lambda: set()


@api.app.get("/api/mcp-test")
def marker():
    return {"fixture": "mcp-management-only"}


try:
    uvicorn.run(api.app, host="127.0.0.1", port=8002, log_level="error")
finally:
    runner.should_exit = True
