"""Read-only physical laboratory tools using the shared service and adapters."""

import json
from .core import ToolResult, tool
from batteryscientist.laboratory.service import LaboratoryService

_service = None


def service():
    global _service
    if _service is None:
        _service = LaboratoryService()
    return _service


@tool(read_only=True)
def laboratory_sources(ctx):
    """List physical laboratory source IDs, equipment kinds and configuration.
    Use laboratory_snapshot for supported capabilities and current observations.
    """
    return ToolResult(
        json.dumps([s.model_dump() for s in service().sources()], ensure_ascii=False)
    )


@tool(read_only=True)
async def laboratory_snapshot(ctx, source_id: str):
    """Read a laboratory snapshot by source ID from laboratory_sources.
    Capabilities describe observations, not permission to control devices.
    Online means the query service responded. Unsupported readings remain null;
    stale observations are not current. Does not start experiments.
    """
    try:
        result = await service().snapshot(source_id)
    except KeyError:
        return ToolResult(
            "Unknown laboratory source; call laboratory_sources first.", True
        )
    return ToolResult(
        result.model_dump_json(), structured_content=result.model_dump(mode="json")
    )
