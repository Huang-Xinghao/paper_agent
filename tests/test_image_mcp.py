"""Image transport stays independent of the deleted Codex bridge."""

import json

import pytest

from paper_agent.mcp.image_client import ImageMCPClient
from paper_agent.tools.core import ToolContext
from paper_agent.tools.image_generation import generate_image, image_generation_state


def test_image_status_needs_no_codex_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "/nonexistent/codex")
    assert ImageMCPClient(tmp_path, timeout=5).generate_status("missing") == {
        "jobId": "missing", "status": "unknown",
    }
    assert not (tmp_path / ".paper-agent" / "codex-state").exists()


@pytest.mark.parametrize("mode", ["timeout", "failed", "invalid_png"])
def test_five_failures_open_persistent_code_fallback(tmp_path, monkeypatch, mode):
    calls = []

    def generate(self, prompt, **kwargs):
        calls.append(kwargs)
        if mode == "timeout":
            raise RuntimeError("Image MCP generate_start timed out after 600s")
        return {"status": "failed", "error": "upstream unavailable"} if mode == "failed" else {"status": "done"}

    monkeypatch.setattr(ImageMCPClient, "generate_start", generate)
    for count in range(1, 6):
        result = generate_image(ToolContext(tmp_path), "Architecture")
        payload = json.loads(result.output)
        assert result.is_error
        assert payload["failure_count"] == count
        assert payload["status"] == ("fallback_required" if count == 5 else "failed")
    # A fresh context and a changed filename cannot bypass the project limit.
    result = generate_image(ToolContext(tmp_path), "Try again", output_path="figures/new.png")
    assert json.loads(result.output)["status"] == "fallback_required"
    assert "figure_1_fallback.json" in json.loads(result.output)["next_action"]
    assert len(calls) == 5
    assert len(image_generation_state(tmp_path)["failures"]) == 5


def test_image_success_resets_failures_and_bad_arguments_do_not_count(tmp_path, monkeypatch):
    def fail(self, prompt, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(ImageMCPClient, "generate_start", fail)
    generate_image(ToolContext(tmp_path), "Architecture")
    generate_image(ToolContext(tmp_path), "")
    generate_image(ToolContext(tmp_path), "Architecture", output_path="outside.png")
    assert len(image_generation_state(tmp_path)["failures"]) == 1

    def succeed(self, prompt, **kwargs):
        output = tmp_path / kwargs["output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        return {"status": "done", "jobId": "test"}

    monkeypatch.setattr(ImageMCPClient, "generate_start", succeed)
    assert not generate_image(ToolContext(tmp_path), "Architecture").is_error
    assert image_generation_state(tmp_path)["failures"] == []
