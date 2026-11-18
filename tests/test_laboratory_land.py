"""LAND inventory must never be presented as live device telemetry."""

import unittest
from paper_agent.api.laboratory import assemble_snapshot, BtsMonitor


class LandSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def inventory(self):
        return assemble_snapshot(
            {
                "observed_at": "2026-11-16T10:00:00Z",
                "discovery_only": True,
                "status_available": False,
                "measurements_available": False,
                "channels": [{"channel_id": "local/LAND/windows/3/1"}],
            },
            {"channels": []},
            {"channels": []},
        )

    def test_discovery_has_unknown_state_and_no_measurements(self):
        result = self.inventory()
        self.assertTrue(result.discovery_only)
        self.assertFalse(result.status_available)
        self.assertFalse(result.measurements_available)
        channel = result.devices[0].channels[0]
        self.assertEqual((channel.unit_id, channel.number), ("3", "1"))
        self.assertEqual(channel.state, "unknown")
        self.assertIsNone(channel.voltage_v)
        self.assertTrue(channel.status_stale)

    async def test_disconnect_retains_inventory_as_stale(self):
        from unittest.mock import patch

        monitor = BtsMonitor("land")
        monitor._last_good = self.inventory()
        with patch(
            "paper_agent.api.laboratory.load_servers",
            side_effect=OSError("disconnected"),
        ):
            result = await monitor.snapshot()
        self.assertEqual(result.connection, "offline")
        self.assertTrue(result.stale)
        self.assertTrue(result.discovery_only)
        self.assertEqual(len(result.devices[0].channels), 1)
        self.assertTrue(result.devices[0].channels[0].measurements_stale)
        await monitor.close()

    def test_bts_capabilities_keep_existing_defaults(self):
        result = assemble_snapshot(
            {"channels": [{"channel_id": "local/27/239/1/1"}]},
            {"channels": [{"channel_id": "local/27/239/1/1", "status": "charge"}]},
            {"channels": [{"channel_id": "local/27/239/1/1", "voltage_v": 3.7}]},
        )
        self.assertFalse(result.discovery_only)
        self.assertTrue(result.status_available)
        self.assertEqual(result.devices[0].channels[0].state, "running")
        self.assertEqual(result.devices[0].channels[0].voltage_v, 3.7)
