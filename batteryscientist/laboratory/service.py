"""Read-only snapshots with per-source caching and isolated failures."""

import asyncio
import time
from .adapters import default_adapters
from .models import SnapshotView, SourceView


class SourceMonitor:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cached = None
        self.last_good = None
        self.expires = 0.0
        self.task = None

    async def snapshot(self):
        if self.cached is not None and time.monotonic() < self.expires:
            return self.cached.model_copy(deep=True)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.refresh())
        result = await asyncio.shield(self.task)
        return result.model_copy(deep=True)

    async def refresh(self):
        try:
            async with asyncio.timeout(20):
                result = await self.adapter.read()
            if result.connection == "online":
                self.last_good = result.model_copy(deep=True)
        except Exception:
            result = (
                self.last_good.model_copy(deep=True)
                if self.last_good
                else SnapshotView(
                    source_id=self.adapter.source_id,
                    label=self.adapter.label,
                    connection="offline",
                    status_available=False,
                    measurements_available=False,
                )
            )
            result.connection = "offline"
            result.stale = True
            result.message = "实验室连接中断，正在等待重新连接。" + (
                "当前展示上次接收的数据。" if result.devices else ""
            )
            for device in result.devices:
                for channel in device.channels:
                    channel.status_stale = channel.measurements_stale = True
        self.cached = result
        self.expires = time.monotonic() + 5
        return result

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class LaboratoryService:
    def __init__(self, adapters=None):
        self.adapters = default_adapters() if adapters is None else adapters
        self.monitors = {
            key: SourceMonitor(adapter) for key, adapter in self.adapters.items()
        }

    def sources(self):
        sources = [adapter.source() for adapter in self.adapters.values()]
        # A workstation is not a cycler with synthetic channels.
        sources.append(
            SourceView(
                id="coin-cell",
                label="自动化扣电",
                vendor="",
                kind="workstation",
                configured=False,
            )
        )
        return sources

    async def snapshot(self, source_id):
        if source_id == "coin-cell":
            return SnapshotView(
                source_id=source_id,
                label="自动化扣电",
                connection="unconfigured",
                status_available=False,
                measurements_available=False,
                message="暂未接入设备",
            )
        if source_id not in self.monitors:
            raise KeyError(source_id)
        return await self.monitors[source_id].snapshot()

    async def close(self):
        await asyncio.gather(*(monitor.close() for monitor in self.monitors.values()))
