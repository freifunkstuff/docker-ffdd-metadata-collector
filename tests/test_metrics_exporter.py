from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.metrics_exporter import VictoriametricsExporter  # noqa: E402
from metadata_collector.models import NodeState  # noqa: E402


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _node(node_id: str = "1727", **overrides) -> NodeState:
    info = {
        "name": "weltladen",
        "community": "Leipzig",
        "model": "Cudy WR3000 v1",
        "group": "0",
        "contact_email": "info@example.org",
        "auto_update": True,
        "firmware_base": "Freifunk Dresden 25.12.0",
        "firmware_release": "9.0.2",
    }
    stats = {
        "uptime_seconds": 123456,
        "load_avg_5": 0.11,
        "mem_total": 256000,
        "mem_free": 128000,
        "clients_2g": 2,
        "clients_5g": 3,
        "traffic_wifi_rx": 1000,
        "traffic_wifi_tx": 2000,
        "airtime_2g_busy": 10,
        "airtime_2g_active": 20,
        "airtime_2g_rx": 5,
        "airtime_2g_tx": 6,
        "airtime_5g_busy": 11,
        "airtime_5g_active": 21,
        "airtime_5g_rx": 7,
        "airtime_5g_tx": 8,
    }
    state = NodeState(
        node_id=node_id,
        primary_ip="10.200.6.198",
        first_seen_at="2022-12-10T17:35:02+00:00",
        last_success_at=NOW.isoformat(),
        info=info,
        stats=stats,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _lines(payload: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in payload.splitlines():
        name = line.split("{", 1)[0].split(" ", 1)[0]
        out.setdefault(name, line)
    return out


class NodeMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.exporter = VictoriametricsExporter(import_url="http://vm/api/v1/import/prometheus")

    def test_emits_underscore_names_and_labels(self) -> None:
        payload = self.exporter.build_payload([_node()], NOW)
        lines = _lines(payload)
        # underscore names, no dots
        for name in (
            "node_info",
            "node_time_up",
            "node_traffic_rx_bytes",
            "node_traffic_tx_bytes",
            "node_clients_wifi24",
            "node_clients_wifi5",
            "node_clients_total",
            "node_load",
            "node_memory_total",
            "node_memory_available",
            "node_memory_airtime_2g_busy",
            "node_memory_airtime_5g_tx",
        ):
            self.assertIn(name, lines)
        self.assertNotIn(".", payload.split(" ")[0])
        self.assertIn('nodeid="1727"', lines["node_info"])
        self.assertIn('domain="Leipzig"', lines["node_info"])
        self.assertIn('owner="info@example.org"', lines["node_info"])
        self.assertIn('autoupdater="enabled"', lines["node_info"])
        self.assertTrue(lines["node_info"].endswith(" 1"))

    def test_clients_total_is_sum(self) -> None:
        lines = _lines(self.exporter.build_payload([_node()], NOW))
        self.assertTrue(lines["node_clients_total"].endswith(" 5"))

    def test_autoupdater_disabled(self) -> None:
        node = _node()
        node.info["auto_update"] = False
        lines = _lines(self.exporter.build_payload([node], NOW))
        self.assertIn('autoupdater="disabled"', lines["node_info"])

    def test_missing_values_are_skipped(self) -> None:
        node = _node()
        node.stats.pop("load_avg_5")
        node.stats.pop("uptime_seconds")
        payload = self.exporter.build_payload([node], NOW)
        lines = _lines(payload)
        self.assertNotIn("node_load", lines)
        self.assertNotIn("node_time_up", lines)
        self.assertIn("node_info", lines)

    def test_stale_node_excluded(self) -> None:
        stale = _node(last_success_at=(NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(self.exporter.build_payload([stale], NOW), "")

    def test_node_without_stats_excluded(self) -> None:
        node = _node(stats={})
        self.assertEqual(self.exporter.build_payload([node], NOW), "")

    def test_community_filter(self) -> None:
        exporter = VictoriametricsExporter(import_url="http://vm", communities=frozenset({"dresden"}))
        self.assertEqual(exporter.build_payload([_node()], NOW), "")
        leipzig = VictoriametricsExporter(import_url="http://vm", communities=frozenset({"leipzig"}))
        self.assertIn("node_info", _lines(leipzig.build_payload([_node()], NOW)))

    def test_label_value_escaping(self) -> None:
        node = _node()
        node.info["name"] = 'we"ird\nname'
        payload = self.exporter.build_payload([node], NOW)
        self.assertIn('hostname="we\\"ird\\nname"', payload)


class LinkMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.exporter = VictoriametricsExporter(import_url="http://vm", link_max_age_seconds=900.0)

    def _states_with_link(self, left_ts: str, right_ts: str) -> list[NodeState]:
        link = {
            "type": "backbone",
            "left_node_id": "14",
            "right_node_id": "1727",
            "left_rq": 250,
            "left_tq": 99,
            "left_ts": left_ts,
            "right_rq": 240,
            "right_tq": 95,
            "right_ts": right_ts,
        }
        left = _node(node_id="14")
        left.info["name"] = "knoten14"
        left.links = [link]
        right = _node(node_id="1727")
        return [left, right]

    def test_link_tq_both_directions_with_dotless_labels(self) -> None:
        fresh = NOW.isoformat()
        payload = self.exporter.build_payload(self._states_with_link(fresh, fresh), NOW)
        link_lines = [ln for ln in payload.splitlines() if ln.startswith("link_tq")]
        self.assertEqual(len(link_lines), 2)
        joined = "\n".join(link_lines)
        self.assertIn('source_id="14"', joined)
        self.assertIn('target_id="1727"', joined)
        self.assertIn('source_hostname="knoten14"', joined)
        self.assertNotIn("source.id", joined)
        # value + explicit timestamp in millis
        ts_ms = int(NOW.timestamp() * 1000)
        self.assertTrue(any(ln.endswith(f" 99 {ts_ms}") for ln in link_lines))

    def test_stale_link_direction_dropped(self) -> None:
        fresh = NOW.isoformat()
        stale = (NOW - timedelta(minutes=20)).isoformat()
        payload = self.exporter.build_payload(self._states_with_link(fresh, stale), NOW)
        link_lines = [ln for ln in payload.splitlines() if ln.startswith("link_tq")]
        self.assertEqual(len(link_lines), 1)
        self.assertIn('source_id="14"', link_lines[0])


if __name__ == "__main__":
    unittest.main()
