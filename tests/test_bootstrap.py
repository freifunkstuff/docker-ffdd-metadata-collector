from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.bootstrap import build_seed_state, build_seed_states, load_meshviewer_document  # noqa: E402
from metadata_collector.models import FetchOutcome, NodeState, ParseResult  # noqa: E402
from metadata_collector.storage import YamlBackedMemoryStore  # noqa: E402


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _leipzig_node() -> dict:
    return {
        "node_id": "1727",
        "addresses": ["10.200.6.198"],
        "mac": "ff:dd:00:00:06:bf",
        "hostname": "weltladen (1727)",
        "contact": "info@einewelt-leipzig.de",
        "model": "Cudy WR3000 v1",
        "domain": "Leipzig",
        "group": "0",
        "nproc": 2,
        "is_online": True,
        "is_gateway": False,
        "firstseen": "2022-12-10T17:35:02.113679Z",
        "lastseen": "2026-06-21T11:58:01.814308401Z",
        "firmware": {"base": "Freifunk Dresden 25.12.0", "release": "9.0.2"},
        "autoupdater": {"enabled": True, "branch": "stable"},
        "location": {"latitude": 51.30871, "longitude": 12.37531, "altitude": 0},
        "gateway": "10.200.200.21",
        "uptime": "2026-06-20T12:00:00Z",
        "clients_wifi24": 0,
        "clients_wifi5": 1,
        "loadavg": 0.11,
        "memory_usage": 0.51,
    }


class BuildSeedStateTest(unittest.TestCase):
    def test_maps_core_fields_and_preserves_firstseen(self) -> None:
        state = build_seed_state(_leipzig_node(), NOW)
        assert state is not None
        self.assertEqual(state.node_id, "1727")
        self.assertEqual(state.primary_ip, "10.200.6.198")
        # firstseen (years old) is conserved
        self.assertEqual(state.first_seen_at, "2022-12-10T17:35:02.113679+00:00")
        # lastseen drives online/offline and snapshot lastSeen
        self.assertEqual(state.last_source_seen_at, "2026-06-21T11:58:01.814308+00:00")
        self.assertTrue(state.is_online(NOW, 600.0))

    def test_maps_info_and_stats(self) -> None:
        state = build_seed_state(_leipzig_node(), NOW)
        assert state is not None
        self.assertEqual(state.info["community"], "Leipzig")
        self.assertEqual(state.info["model"], "Cudy WR3000 v1")
        self.assertEqual(state.info["name"], "weltladen")  # node_id suffix stripped
        self.assertEqual(state.info["contact_email"], "info@einewelt-leipzig.de")
        self.assertEqual(state.info["node_type"], "node")
        self.assertEqual(state.info["auto_update"], True)
        self.assertEqual(state.info["cpu_count"], 2)
        self.assertEqual(state.info["firmware_base"], "Freifunk Dresden 25.12.0")
        self.assertAlmostEqual(state.info["location_latitude"], 51.30871)
        self.assertEqual(state.stats["clients_2g"], 0)
        self.assertEqual(state.stats["clients_5g"], 1)
        self.assertEqual(state.stats["uptime_seconds"], 86400)

    def test_gateway_becomes_server(self) -> None:
        node = _leipzig_node()
        node["is_gateway"] = True
        state = build_seed_state(node, NOW)
        assert state is not None
        self.assertEqual(state.node_type, "server")
        self.assertEqual(state.info["node_type"], "server")

    def test_handles_nanosecond_and_z_timestamps(self) -> None:
        state = build_seed_state(_leipzig_node(), NOW)
        assert state is not None
        # 9-digit fractional seconds must not crash parsing
        self.assertTrue(state.last_source_seen_at.endswith("+00:00"))

    def test_skips_node_without_id(self) -> None:
        node = _leipzig_node()
        del node["node_id"]
        self.assertIsNone(build_seed_state(node, NOW))

    def test_offline_node_is_offline(self) -> None:
        node = _leipzig_node()
        node["lastseen"] = "2026-06-01T00:00:00Z"
        state = build_seed_state(node, NOW)
        assert state is not None
        self.assertFalse(state.is_online(NOW, 600.0))


class SeedStatesStoreTest(unittest.TestCase):
    def _store(self, root: Path) -> YamlBackedMemoryStore:
        return YamlBackedMemoryStore(
            discovery_state_path=root / "discovery.yaml",
            node_info_dir=root / "info",
            node_status_dir=root / "status",
        )

    def test_seeds_persist_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.initialize()
            store.seed_states(build_seed_states({"nodes": [_leipzig_node()]}, NOW), NOW.isoformat())

            reloaded = self._store(root)
            reloaded.initialize()
            state = reloaded.get_node_state("1727")
            assert state is not None
            self.assertEqual(state.first_seen_at, "2022-12-10T17:35:02.113679+00:00")
            self.assertEqual(state.primary_ip, "10.200.6.198")
            self.assertEqual(state.info["model"], "Cudy WR3000 v1")

    def test_poll_keeps_conserved_firstseen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.initialize()
            store.seed_states(build_seed_states({"nodes": [_leipzig_node()]}, NOW), NOW.isoformat())

            outcome = FetchOutcome(
                node_id="1727",
                primary_ip="10.200.6.198",
                fetched_at=NOW.isoformat(),
                success=True,
                parse_result=ParseResult(
                    node_id="1727",
                    version="18",
                    timestamp=NOW.isoformat(),
                    node_type="node",
                    parser_name="GenericSysinfoParser",
                    info={"community": "Leipzig", "model": "Cudy WR3000 v1 (polled)"},
                    stats={"clients_2g": 3},
                ),
            )
            store.apply_fetch_outcome(outcome)
            state = store.get_node_state("1727")
            assert state is not None
            # poll refreshed the metadata but the years-old firstseen survived
            self.assertEqual(state.first_seen_at, "2022-12-10T17:35:02.113679+00:00")
            self.assertEqual(state.info["model"], "Cudy WR3000 v1 (polled)")

    def test_reseed_lowers_firstseen_and_keeps_polled_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root)
            store.initialize()
            # existing state already polled, with a newer first_seen than the seed
            existing = NodeState(
                node_id="1727",
                primary_ip="10.200.6.198",
                first_seen_at="2025-01-01T00:00:00+00:00",
                info={"model": "polled-model"},
            )
            store._states["1727"] = existing  # noqa: SLF001 - test seam

            store.seed_states(build_seed_states({"nodes": [_leipzig_node()]}, NOW), NOW.isoformat())
            state = store.get_node_state("1727")
            assert state is not None
            # first_seen lowered to the seed's older value, polled info untouched
            self.assertEqual(state.first_seen_at, "2022-12-10T17:35:02.113679+00:00")
            self.assertEqual(state.info["model"], "polled-model")


class LoadDocumentTest(unittest.TestCase):
    def test_loads_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mv.json"
            path.write_text(json.dumps({"nodes": [_leipzig_node()]}), encoding="utf-8")
            document = load_meshviewer_document(str(path))
            self.assertEqual(len(document["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
