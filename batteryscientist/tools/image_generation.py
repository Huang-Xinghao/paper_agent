"""Agent-visible image tool using the shared external-service adapter."""
from __future__ import annotations

import asyncio
import json

from batteryscientist.integrations.images import generate
from .core import ToolContext, ToolResult, tool


@tool()
async def generate_image(ctx: ToolContext, prompt: str,
                         output_path: str = 'figures/ai_generated/figure.png',
                         size: str = '1536x1024', quality: str = 'high') -> ToolResult:
    """Generate a PNG under figures/. On failure inspect the cause or draw with code.
    A failed or interrupted API request is not retried automatically.
    """
    try:
        ctx.resolve(output_path, write=True)
        receipt = await asyncio.to_thread(generate, ctx.workspace, prompt, output_path,
                                          size=size, quality=quality)
    except Exception as exc:
        return ToolResult(str(exc), True)
    return ToolResult(json.dumps(receipt, ensure_ascii=False), metadata=receipt)
