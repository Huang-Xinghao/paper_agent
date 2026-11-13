"""BTS tool registry adapter. AgentLoop/phase exposure remains explicit."""
from __future__ import annotations

import json
from .core import ToolContext, ToolResult, builtin_tools, tool
from paper_agent.mcp.bts_client import call_bts


async def _call(name, args):
    answer = await call_bts(name, args)
    content = answer.get('structuredContent')
    output = json.dumps(content, ensure_ascii=False) if content is not None else '\n'.join(
        item.get('text', '') for item in answer.get('content', []) if item.get('type') == 'text')
    return ToolResult(output, bool(answer.get('isError')), {'transport': 'mcp', 'tool': name})


@tool(read_only=True)
async def bts_health(ctx: ToolContext) -> ToolResult:
    """Check Windows BTS bridge connectivity, device inventory, and control availability."""
    return await _call('bts_health', {})


@tool(read_only=True)
async def bts_list_channels(ctx: ToolContext) -> ToolResult:
    """Discover current BTS channels; an empty list means no channels available."""
    return await _call('bts_list_channels', {})


@tool(read_only=True)
async def bts_get_status(ctx: ToolContext, channel_ids: list[str]) -> ToolResult:
    """Read current status of discovered BTS channel IDs (1..40)."""
    return await _call('bts_get_status', {'channel_ids': channel_ids})


@tool(read_only=True)
async def bts_get_measurements(ctx: ToolContext, channel_ids: list[str]) -> ToolResult:
    """Read BTS current A, voltage V, capacity Ah and energy Wh; missing values are null."""
    return await _call('bts_get_measurements', {'channel_ids': channel_ids})


@tool(read_only=True)
async def bts_list_protocols(ctx: ToolContext) -> ToolResult:
    """List protocol IDs registered on Windows."""
    return await _call('bts_list_protocols', {})


@tool()
async def bts_register_protocol(ctx: ToolContext, protocol_xml: str) -> ToolResult:
    """Register XML on Windows. Syntax validation does not certify BTS compatibility."""
    return await _call('bts_register_protocol', {'protocol_xml': protocol_xml})


@tool()
async def bts_start(ctx: ToolContext, channel_ids: list[str], protocol_id: str,
                    idempotency_key: str, barcode: str = '') -> ToolResult:
    """Queue BTS start. Use a unique stable idempotency_key; poll returned operation_id."""
    return await _call('bts_start', dict(channel_ids=channel_ids, protocol_id=protocol_id,
                                       idempotency_key=idempotency_key, barcode=barcode))


@tool()
async def bts_stop(ctx: ToolContext, channel_ids: list[str], idempotency_key: str) -> ToolResult:
    """Queue BTS stop; inspect operation and physical channel status afterwards."""
    return await _call('bts_stop', dict(channel_ids=channel_ids, idempotency_key=idempotency_key))


@tool()
async def bts_resume(ctx: ToolContext, channel_ids: list[str], idempotency_key: str) -> ToolResult:
    """Queue BTS continue; never blindly retry an uncertain command with a new key."""
    return await _call('bts_resume', dict(channel_ids=channel_ids, idempotency_key=idempotency_key))


@tool(read_only=True)
async def bts_get_operation(ctx: ToolContext, operation_id: str, refresh: bool = False) -> ToolResult:
    """Read durable operation receipt; refresh optionally reads current device status."""
    return await _call('bts_get_operation', dict(operation_id=operation_id, refresh=refresh))


@tool(read_only=True)
async def bts_read_records(ctx: ToolContext, channel_id: str, test_id: int,
                           start: int = 1, count: int = 100) -> ToolResult:
    """Read <=1000 vendor records; use fixed test_id for historical paging (0 means current test)."""
    return await _call('bts_read_records', dict(channel_id=channel_id, test_id=test_id, start=start, count=count))


def bts_tools(*, include_control: bool = False):
    """Pass returned specs to AgentLoop(tools=...) or PhaseAgent.add_tool explicitly."""
    return tuple(t for t in builtin_tools() if t.name.startswith('bts_') and (include_control or t.read_only))
