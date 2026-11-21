"""Read-only laboratory contracts; all device responses are simulated."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from batteryscientist.api.laboratory import router
from batteryscientist.integrations.mcp import tool_name
from batteryscientist.laboratory.adapters.bts import BtsAdapter
from batteryscientist.laboratory.adapters.land import LandAdapter
from batteryscientist.laboratory.adapters.cycler import assemble_snapshot
from batteryscientist.laboratory.models import Capabilities, SnapshotView, SourceView
from batteryscientist.laboratory.service import LaboratoryService, SourceMonitor
from batteryscientist.tools.core import ToolResult, builtin_tools
from batteryscientist.tools import laboratory as agent_tools
from batteryscientist.tools.profiles import tools_for_phase

IDS = [f"local/27/239/1/{i}" for i in range(1, 42)]


def health(**extra):
    return {
        "reachable": True,
        "observed_at": "2026-11-21T01:00:00Z",
        "channels": [{"channel_id": i} for i in IDS],
        **extra,
    }


def fake_manager(source_id, responses):
    calls = []

    async def call(name, **kwargs):
        calls.append((name, kwargs))
        value = responses[name]
        if callable(value):
            value = value(**kwargs)
        if isinstance(value, Exception):
            raise value
        return ToolResult(structured_content=value)

    specs = []
    for name in responses:

        async def handler(ctx, _name=name, **kwargs):
            return await call(_name, **kwargs)

        specs.append(
            SimpleNamespace(
                name=tool_name(source_id, f"{source_id}_{name}"), handler=handler
            )
        )
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(tools=specs)
    return manager, calls


class LaboratoryContracts(unittest.IsolatedAsyncioTestCase):
    async def read_adapter(self, adapter, responses):
        manager, calls = fake_manager(adapter.source_id, responses)
        server = SimpleNamespace(name=adapter.source_id)
        with (
            patch.object(adapter, "servers", return_value=(server,)),
            patch(
                "batteryscientist.laboratory.adapters.cycler.MCPManager",
                return_value=manager,
            ),
        ):
            result = await adapter.read()
        return result, calls

    async def test_discovery_only_never_queries_unsupported_features(self):
        result, calls = await self.read_adapter(
            LandAdapter(),
            {
                "health": health(discovery_only=True),
                "get_status": AssertionError("must not query"),
                "get_measurements": AssertionError("must not query"),
            },
        )
        self.assertEqual([c[0] for c in calls], ["health"])
        self.assertTrue(result.discovery_only)
        self.assertFalse(result.stale)
        self.assertFalse(result.capabilities.measurements)
        self.assertIsNone(result.devices[0].channels[0].voltage_v)
        self.assertEqual(result.devices[0].vendor, "LAND")

    async def test_missing_tool_is_not_advertised_as_supported(self):
        result, calls = await self.read_adapter(BtsAdapter(), {"health": health()})
        self.assertFalse(result.capabilities.status)
        self.assertFalse(result.capabilities.measurements)
        self.assertFalse(result.stale)
        self.assertEqual(len(calls), 1)

    async def test_batch_failure_preserves_good_readings_without_fabricating_missing_data(
        self,
    ):
        def readings(channel_ids):
            if channel_ids == IDS[40:]:
                raise RuntimeError("simulated batch failure")
            return {
                "observed_at": "2026-11-21T01:01:00Z",
                "channels": [
                    {
                        "channel_id": i,
                        "voltage_v": 3.7,
                        "current_a": 0,
                        "capacity_ah": True,
                        "energy_wh": float("nan"),
                    }
                    for i in channel_ids
                ],
            }

        result, calls = await self.read_adapter(
            BtsAdapter(),
            {
                "health": health(),
                "get_status": lambda channel_ids: {
                    "channels": [
                        {"channel_id": i, "status": "charge"} for i in channel_ids
                    ]
                },
                "get_measurements": readings,
            },
        )
        self.assertEqual(len(calls), 5)  # health + two features in two batches
        self.assertTrue(result.stale)
        first, last = result.devices[0].channels[0], result.devices[0].channels[-1]
        self.assertEqual(first.state, "running")
        self.assertEqual(first.voltage_v, 3.7)
        self.assertEqual(first.current_a, 0)
        self.assertIsNone(first.capacity_ah)
        self.assertIsNone(first.energy_wh)
        self.assertEqual(first.observed_at, "2026-11-21T01:01:00Z")
        self.assertIsNone(last.voltage_v)
        self.assertTrue(last.measurements_stale)
        self.assertFalse(last.status_stale)

    async def test_concurrent_queries_share_refresh_and_return_independent_copies(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def read():
            started.set()
            await release.wait()
            return SnapshotView(source_id="bts", connection="online")

        adapter = SimpleNamespace(
            source_id="bts", label="BTS", read=AsyncMock(side_effect=read)
        )
        monitor = SourceMonitor(adapter)
        one, two = asyncio.create_task(monitor.snapshot()), asyncio.create_task(
            monitor.snapshot()
        )
        await started.wait()
        release.set()
        first, second = await asyncio.gather(one, two)
        first.label = "modified"
        self.assertNotEqual(first.label, second.label)
        await monitor.snapshot()
        adapter.read.assert_awaited_once()
        await monitor.close()

    async def test_disconnect_retains_last_good_data_and_other_sources_keep_working(
        self,
    ):
        source = SourceView(id="bts", label="BTS", vendor="Neware", kind="cycler")
        good = assemble_snapshot(
            health(), {}, {}, source=source, capabilities=Capabilities(discovery=True)
        )
        bts = SimpleNamespace(
            source_id="bts",
            label="BTS",
            read=AsyncMock(side_effect=[good, RuntimeError("secret")]),
        )
        land = SimpleNamespace(
            source_id="land",
            label="LAND",
            read=AsyncMock(return_value=SnapshotView(connection="online")),
        )
        service = LaboratoryService({"bts": bts, "land": land})
        await service.snapshot("bts")
        service.monitors["bts"].expires = 0
        disconnected = await service.snapshot("bts")
        self.assertEqual(disconnected.connection, "offline")
        self.assertTrue(disconnected.stale)
        self.assertEqual(len(disconnected.devices[0].channels), 41)
        self.assertTrue(disconnected.devices[0].channels[0].status_stale)
        self.assertNotIn("secret", disconnected.message)
        self.assertEqual((await service.snapshot("land")).connection, "online")
        self.assertEqual(good.connection, "online")
        await service.close()

    async def test_unconfigured_sources_do_not_open_transport(self):
        adapter = BtsAdapter()
        with (
            patch.object(adapter, "servers", return_value=()),
            patch(
                "batteryscientist.laboratory.adapters.cycler.MCPManager"
            ) as transport,
        ):
            self.assertEqual((await adapter.read()).connection, "unconfigured")
            transport.assert_not_called()

    async def test_http_legacy_and_agent_use_same_snapshot(self):
        expected = SnapshotView(source_id="bts", label="BTS", connection="online")
        adapter = SimpleNamespace(
            source_id="bts",
            label="BTS",
            read=AsyncMock(return_value=expected),
            source=lambda: SourceView(
                id="bts", label="BTS", vendor="Neware", kind="cycler"
            ),
        )
        service = LaboratoryService({"bts": adapter})
        app = FastAPI()
        app.state.laboratory = service
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            canonical = await client.get("/api/laboratory/sources/bts/snapshot")
            legacy = await client.get("/api/laboratory/bts")
            self.assertEqual(canonical.status_code, 200)
            self.assertEqual(canonical.json(), legacy.json())
            self.assertEqual(
                (await client.get("/api/laboratory/sources/no/snapshot")).status_code,
                404,
            )
            self.assertEqual(
                len((await client.get("/api/laboratory/sources")).json()), 2
            )
        with patch.object(agent_tools, "_service", service):
            result = await agent_tools.laboratory_snapshot(None, "bts")
            self.assertEqual(result.structured_content, canonical.json())
            self.assertTrue(
                (await agent_tools.laboratory_snapshot(None, "unknown")).is_error
            )
        adapter.read.assert_awaited_once()
        for phase in ("experiment_plan", "experiment_bridge"):
            specs = {s.name: s for s in tools_for_phase(phase, builtin_tools())}
            self.assertTrue(specs["laboratory_sources"].read_only)
            self.assertTrue(specs["laboratory_snapshot"].read_only)
        await service.close()


if __name__ == "__main__":
    unittest.main()
