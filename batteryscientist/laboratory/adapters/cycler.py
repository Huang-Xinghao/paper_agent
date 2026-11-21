"""Normalize cycler MCP data at the adapter boundary."""

import copy
import json
import math
import os
from datetime import datetime, timezone
from batteryscientist.integrations.mcp import MCPManager, load_servers, tool_name
from ..models import (
    Capabilities,
    ChannelState,
    ChannelView,
    DeviceView,
    SnapshotView,
    SourceView,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    return None


def _state(status: str) -> tuple[ChannelState, str]:
    values = {
        "charge": ("running", "充电中"),
        "charging": ("running", "充电中"),
        "discharge": ("running", "放电中"),
        "discharging": ("running", "放电中"),
        "rest": ("running", "静置中"),
        "running": ("running", "运行中"),
        "run": ("running", "运行中"),
        "working": ("running", "运行中"),
        "finish": ("finished", "已结束"),
        "finished": ("finished", "已结束"),
        "complete": ("finished", "已结束"),
        "completed": ("finished", "已结束"),
        "pause": ("paused", "已暂停"),
        "paused": ("paused", "已暂停"),
        "stop": ("stopped", "已停止"),
        "stopped": ("stopped", "已停止"),
        "idle": ("idle", "空闲"),
        "ready": ("idle", "就绪"),
        "error": ("error", "故障"),
        "fault": ("error", "故障"),
        "protect": ("error", "保护中"),
        "offline": ("offline", "离线"),
        "disconnected": ("offline", "离线"),
    }
    return values.get(status.strip().lower(), ("unknown", "未知"))


def _index(payload):
    return {
        item["channel_id"]: item
        for item in payload.get("channels", [])
        if isinstance(item, dict) and isinstance(item.get("channel_id"), str)
    }


def assemble_snapshot(
    health: dict,
    statuses: dict,
    measurements: dict,
    *,
    source: SourceView,
    capabilities: Capabilities,
    partial: bool = False,
):
    """Join by discovered channel ID only. Missing/invalid measurements stay null."""
    status_by_id, measurement_by_id = _index(statuses), _index(measurements)
    devices = {}
    seen = set()
    for entry in health.get("channels", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("channel_id"), str):
            continue
        identifier = entry.get("channel_id", "")
        parts = identifier.split("/")
        if len(parts) != 5 or parts[0] != "local" or identifier in seen:
            continue
        seen.add(identifier)
        _, device_type, device_id, unit, number = parts
        key = "/".join(parts[:3])
        device = devices.setdefault(
            key,
            DeviceView(
                id=key,
                device_id=device_id,
                device_type=device_type,
                source_id=source.id,
                vendor=source.vendor,
                name=f"{source.label} · {device_id}",
                kind=source.kind,
                capabilities=capabilities,
            ),
        )
        status_record = status_by_id.get(identifier, {})
        measure = measurement_by_id.get(identifier, {})
        status_fresh = (
            capabilities.status
            and bool(status_record)
            and not status_record.get("stale", False)
        )
        measure_fresh = (
            capabilities.measurements
            and bool(measure)
            and not measure.get("stale", False)
            and status_record.get("exists") is not False
        )
        status = (
            (status_record.get("status") if status_fresh else None)
            or (measure.get("status") if measure_fresh else None)
            or "unknown"
        )
        state, label = _state(status)
        if status_record.get("exists") is False:
            state, label, status = "offline", "离线", "offline"
        raw = measure.get("raw_attributes") or {}
        channel = ChannelView(
            channel_id=identifier,
            unit_id=unit,
            number=number,
            status=status,
            state=state,
            status_label=label,
            status_stale=not status_fresh and not measure_fresh,
            measurements_stale=not measure_fresh,
            observed_at=(
                measure.get("_observed_at", measurements.get("observed_at"))
                if measure_fresh
                else (
                    status_record.get("_observed_at", statuses.get("observed_at"))
                    if status_fresh
                    else None
                )
            ),
            cycle=(
                str(raw["cycle_id"])
                if raw.get("cycle_id") is not None and measure_fresh
                else None
            ),
            step_type=(
                str(raw["step_type"])
                if raw.get("step_type") not in (None, "--", "") and measure_fresh
                else None
            ),
            **{
                name: _number(measure.get(name)) if measure_fresh else None
                for name in ("voltage_v", "current_a", "capacity_ah", "energy_wh")
            },
        )
        device.channels.append(channel)
    stale = partial or any(
        (capabilities.status and c.status_stale)
        or (capabilities.measurements and c.measurements_stale)
        for d in devices.values()
        for c in d.channels
    )
    return SnapshotView(
        connection="online",
        source_id=source.id,
        label=source.label,
        capabilities=capabilities,
        observed_at=health.get("observed_at"),
        last_success_at=_now(),
        stale=stale,
        discovery_only=capabilities.discovery
        and not (capabilities.status or capabilities.measurements),
        status_available=capabilities.status,
        measurements_available=capabilities.measurements,
        message=(
            f"已读取{source.label}通道目录；实时状态和测量值尚未接入。"
            if capabilities.discovery
            and not (capabilities.status or capabilities.measurements)
            else ("部分通道数据未更新，请稍后刷新。" if stale else None)
        ),
        devices=list(devices.values()),
    )


class CyclerAdapter:
    """Shared read transport for compatible cyclers; no control operations."""

    source_id: str
    label: str
    vendor: str

    def servers(self):
        name = os.environ.get(
            f"BATTERYSCIENTIST_LAB_{self.source_id.upper()}_SERVER", self.source_id
        )
        return tuple(s for s in load_servers(phase="laboratory") if s.name == name)

    def source(self):
        return SourceView(
            id=self.source_id,
            label=self.label,
            vendor=self.vendor,
            kind="cycler",
            configured=bool(self.servers()),
        )

    async def read(self):
        source = self.source()
        servers = self.servers()
        if not servers:
            return SnapshotView(
                source_id=source.id,
                label=source.label,
                connection="unconfigured",
                status_available=False,
                measurements_available=False,
                message=f"尚未连接{source.label}。",
            )
        async with MCPManager(servers) as manager:
            tools = {spec.name: spec for spec in manager.tools}

            def remote(name):
                return tool_name(servers[0].name, f"{self.source_id}_{name}")

            async def call(name, **arguments):
                result = await tools[remote(name)].handler(None, **arguments)
                if result.is_error:
                    raise RuntimeError("Laboratory query failed")
                payload = (
                    result.structured_content
                    if result.structured_content is not None
                    else json.loads(result.output)
                )
                if not isinstance(payload, dict):
                    raise ValueError("Invalid laboratory response")
                return payload

            health = await call("health")
            if health.get("reachable") is not True:
                raise RuntimeError("Laboratory gateway not reachable")
            discovery_only = health.get("discovery_only", False)
            capabilities = Capabilities(
                discovery=True,
                status=not discovery_only
                and health.get("status_available", True)
                and remote("get_status") in tools,
                measurements=not discovery_only
                and health.get("measurements_available", True)
                and remote("get_measurements") in tools,
            )
            # Never call a feature that the gateway does not support.
            ids = list(
                dict.fromkeys(
                    c["channel_id"]
                    for c in health.get("channels", [])
                    if isinstance(c, dict) and isinstance(c.get("channel_id"), str)
                )
            )
            statuses, measurements = {"channels": []}, {"channels": []}
            partial = False
            for start in range(0, len(ids), 40):
                for name, supported, target in (
                    ("get_status", capabilities.status, statuses),
                    ("get_measurements", capabilities.measurements, measurements),
                ):
                    if not supported:
                        continue
                    try:
                        part = await call(name, channel_ids=ids[start : start + 40])
                        for channel in part.get("channels", []):
                            channel = copy.deepcopy(channel)
                            channel["_observed_at"] = part.get("observed_at")
                            target["channels"].append(channel)
                        target["observed_at"] = part.get("observed_at")
                    except Exception:
                        partial = True
            return assemble_snapshot(
                health,
                statuses,
                measurements,
                source=source,
                capabilities=capabilities,
                partial=partial,
            )
