"""Vendor-independent, read-only laboratory views."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class LaboratoryView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Capabilities(LaboratoryView):
    discovery: bool = False
    status: bool = False
    measurements: bool = False
    records: bool = False
    protocols: bool = False


class SourceView(LaboratoryView):
    id: str
    label: str
    vendor: str
    kind: Literal["cycler", "workstation"]
    configured: bool = False


ChannelState = Literal[
    "running", "finished", "paused", "stopped", "idle", "error", "offline", "unknown"
]


class ChannelView(LaboratoryView):
    channel_id: str
    unit_id: str
    number: str
    status: str = "unknown"
    state: ChannelState = "unknown"
    status_label: str = "未知"
    status_stale: bool = True
    measurements_stale: bool = True
    observed_at: str | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    capacity_ah: float | None = None
    energy_wh: float | None = None
    cycle: str | None = None
    step_type: str | None = None


class DeviceView(LaboratoryView):
    source_id: str = ""
    vendor: str = ""
    name: str = ""
    kind: str = "cycler"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    id: str
    device_id: str
    device_type: str
    channels: list[ChannelView] = Field(default_factory=list)


class SnapshotView(LaboratoryView):
    source_id: str = ""
    label: str = ""
    capabilities: Capabilities = Field(default_factory=Capabilities)
    connection: Literal["online", "offline", "unconfigured"]
    observed_at: str | None = None
    last_success_at: str | None = None
    stale: bool = False
    message: str | None = None
    devices: list[DeviceView] = Field(default_factory=list)
    discovery_only: bool = False
    status_available: bool = False
    measurements_available: bool = False
