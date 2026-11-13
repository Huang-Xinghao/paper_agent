"""Tests must not connect to the developer's configured external MCP servers."""
import json

import pytest


@pytest.fixture(autouse=True)
def isolated_mcp_config(tmp_path, monkeypatch):
    config = tmp_path / 'test-mcp-config.json'
    config.write_text(json.dumps({'mcpServers': {}}))
    monkeypatch.setenv('PAPER_AGENT_MCP_CONFIG', str(config))
