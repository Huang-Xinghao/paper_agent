"""Read-only physical laboratory snapshots, obtained through configured MCP tools."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from paper_agent.mcp.manager import MCPManager, load_servers, tool_name

router = APIRouter(prefix="/api/laboratory", tags=["laboratory"])

ChannelState = Literal[
    "running", "finished", "paused", "stopped", "idle", "error", "offline", "unknown"
]


class LaboratoryView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class BtsChannelView(LaboratoryView):
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


class BtsDeviceView(LaboratoryView):
    id: str
    device_id: str
    device_type: str
    channels: list[BtsChannelView] = Field(default_factory=list)


class BtsSnapshotView(LaboratoryView):
    connection: Literal["online", "offline", "unconfigured"]
    observed_at: str | None = None
    last_success_at: str | None = None
    stale: bool = False
    message: str | None = None
    devices: list[BtsDeviceView] = Field(default_factory=list)


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
    health: dict, statuses: dict, measurements: dict, *, partial: bool = False
):
    """Join by discovered channel ID only. Missing/invalid measurements stay null."""
    status_by_id, measurement_by_id = _index(statuses), _index(measurements)
    devices = {}
    seen = set()
    for entry in health.get("channels", []):
        identifier = entry.get("channel_id", "")
        parts = identifier.split("/")
        if len(parts) != 5 or parts[0] != "local" or identifier in seen:
            continue
        seen.add(identifier)
        _, device_type, device_id, unit, number = parts
        key = "/".join(parts[:3])
        device = devices.setdefault(
            key, BtsDeviceView(id=key, device_id=device_id, device_type=device_type)
        )
        status_record = status_by_id.get(identifier, {})
        measure = measurement_by_id.get(identifier, {})
        status_fresh = bool(status_record) and not status_record.get("stale", False)
        measure_fresh = (
            bool(measure)
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
        channel = BtsChannelView(
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
        c.status_stale or c.measurements_stale
        for d in devices.values()
        for c in d.channels
    )
    return BtsSnapshotView(
        connection="online",
        observed_at=health.get("observed_at"),
        last_success_at=_now(),
        stale=stale,
        message="部分通道数据未更新，请稍后刷新。" if stale else None,
        devices=list(devices.values()),
    )


class BtsMonitor:
    """Coalesce concurrent viewers; keep last observations visibly stale on disconnect."""

    def __init__(self):
        self._cached: BtsSnapshotView | None = None
        self._last_good: BtsSnapshotView | None = None
        self._expires = 0.0
        self._task: asyncio.Task | None = None

    async def snapshot(self):
        if self._cached is not None and time.monotonic() < self._expires:
            return self._cached
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh())
        return await asyncio.shield(self._task)

    async def _refresh(self):
        try:
            servers = tuple(
                s
                for s in load_servers(phase="laboratory")
                if s.name == os.environ.get("PAPER_AGENT_LAB_BTS_SERVER", "bts")
            )
            if not servers:
                result = BtsSnapshotView(
                    connection="unconfigured", message="尚未连接新威 BTS 设备。"
                )
            else:
                async with asyncio.timeout(20):
                    result = await self._read(servers)
                self._last_good = result.model_copy(deep=True)
        except Exception:
            result = (
                self._last_good.model_copy(deep=True)
                if self._last_good
                else BtsSnapshotView(connection="offline")
            )
            result.connection = "offline"
            result.stale = True
            result.message = "实验室连接中断，正在等待重新连接。" + (
                "当前展示上次接收的数据。" if result.devices else ""
            )
            for device in result.devices:
                for channel in device.channels:
                    channel.status_stale = channel.measurements_stale = True
        self._cached, self._expires = result, time.monotonic() + 5
        return result

    async def _read(self, servers):
        async with MCPManager(servers) as manager:
            tools = {spec.name: spec for spec in manager.tools}

            async def call(name, **arguments):
                result = await tools[tool_name(servers[0].name, name)].handler(
                    None, **arguments
                )
                if result.is_error:
                    raise RuntimeError("BTS query failed")
                # Structured JSON is first; an additional text receipt may follow.
                payload, _ = json.JSONDecoder().raw_decode(result.output.lstrip())
                if not isinstance(payload, dict):
                    raise ValueError("Invalid BTS response")
                return payload

            health = await call("bts_health")
            if health.get("reachable") is not True:
                raise RuntimeError("BTS not reachable")
            ids = list(
                dict.fromkeys(c["channel_id"] for c in health.get("channels", []))
            )
            statuses, measurements = {"channels": []}, {"channels": []}
            partial = False
            for start in range(0, len(ids), 40):
                for name, target in (
                    ("bts_get_status", statuses),
                    ("bts_get_measurements", measurements),
                ):
                    try:
                        part = await call(name, channel_ids=ids[start : start + 40])
                        # Retain each channel's own batch receive time.
                        for channel in part.get("channels", []):
                            channel = copy.deepcopy(channel)
                            channel["_observed_at"] = part.get("observed_at")
                            target["channels"].append(channel)
                        target["observed_at"] = part.get("observed_at")
                    except Exception:
                        partial = True
            return assemble_snapshot(health, statuses, measurements, partial=partial)

    async def close(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


@router.get("/bts", response_model=BtsSnapshotView)
async def bts_snapshot(request: Request):
    return await request.app.state.bts_monitor.snapshot()
