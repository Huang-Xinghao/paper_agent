"""Client for the project-local image generation MCP bridge."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import _call

SERVER = Path(__file__).resolve().parents[2] / "mcp-servers" / "image-gen" / "server.py"


class ImageMCPClient:
    def __init__(self, project: Path, *, timeout: float = 600):
        self.project = project.resolve()
        self.timeout = timeout

    def generate_start(self, prompt: str, *, output_path: str = "figures/ai_generated/figure1.png",
                       size: str = "1536x1024", quality: str = "high") -> dict[str, Any]:
        return _call(self.project, "generate_start", {
            "prompt": prompt, "cwd": str(self.project), "outputPath": output_path,
            "size": size, "quality": quality,
        }, timeout=self.timeout, server=SERVER)

    def generate_status(self, job_id: str) -> dict[str, Any]:
        return _call(self.project, "generate_status", {
            "jobId": job_id, "cwd": str(self.project),
        }, timeout=self.timeout, server=SERVER)

