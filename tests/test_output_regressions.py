from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.meshviewer import build_meshviewer_document
from metadata_collector.models import NodeState
from metadata_collector.sysinfo_parsers import parse_payload


class OutputRegressionTests(unittest.TestCase):
    def state(self, **kwargs: object) -> NodeState:
        values = dict(
            node_id="1001", primary_ip="10.0.0.1",
            first_seen_at="2025-01-01T00:00:00+00:00",
            last_source_seen_at="2025-01-01T00:09:00+00:00",
            last_success_at="2025-01-01T00:09:30+00:00",
            info={"name": "Example", "community": "Dresden"},
            stats={"uptime_seconds": 120.0},
        )
        values.update(kwargs)
        return NodeState(**values)

    def export(self, state: NodeState, generated_at: str = "2025-01-01T00:10:00+00:00") -> dict:
        document = build_meshviewer_document(generated_at, [state], 600, 1800, 30)
        self.assertEqual(1, len(document["nodes"]))
        json.dumps(document, allow_nan=False)
        return document["nodes"][0]

    def test_repeat_export_keeps_measurement_based_boot_timestamp(self) -> None:
        state = self.state(last_fetch_at="2025-01-01T00:10:00+00:00")
        first = self.export(state)
        later = self.export(state, "2025-01-01T00:11:00+00:00")
        self.assertEqual("2025-01-01T00:07:30+00:00", first["uptime"])
        self.assertEqual(first["uptime"], later["uptime"])

    def test_missing_or_invalid_sample_timestamp_omits_only_boot_time(self) -> None:
        for timestamp in (None, "", "invalid", "nan", "inf", "1e300"):
            with self.subTest(timestamp=timestamp):
                node = self.export(self.state(last_success_at=timestamp))
                self.assertNotIn("uptime", node)
                self.assertEqual("Example (1001)", node["hostname"])

    def test_epoch_sample_timestamp_is_supported(self) -> None:
        # No discovery timestamp forces NodeState to use its own timestamp parser;
        # numeric epochs are valid for both paths.
        node = self.export(self.state(last_success_at="1735690170"))
        self.assertEqual("2025-01-01T00:07:30+00:00", node["uptime"])

    def test_parser_rejects_nonfinite_fields_without_losing_node(self) -> None:
        for value in ("nan", "inf", "-inf", "NaN", "1e999", float("nan"), float("inf")):
            with self.subTest(value=value):
                parsed = parse_payload({"data": {
                    "common": {"node": "1001", "community": "Dresden"},
                    "contact": {"name": "Example"},
                    "gps": {"latitude": value, "longitude": "13.7"},
                    "system": {"uptime": value},
                    "statistic": {"cpu_load": f"0 {value} 0", "meminfo_MemTotal": f"{value} kB",
                                  "meminfo_MemFree": "25 kB", "client2g": {"1min": "3"}},
                }})
                self.assertEqual("1001", parsed.node_id)
                self.assertEqual("Example", parsed.info["name"])
                self.assertNotIn("location_latitude", parsed.info)
                for field in ("uptime_seconds", "load_avg_5", "mem_total"):
                    self.assertNotIn(field, parsed.stats)
                node = self.export(self.state(info=parsed.info, stats=parsed.stats))
                self.assertEqual(3, node["clients"])
                self.assertNotIn("uptime", node)

    def test_export_rejects_persisted_nonfinite_and_overflow_values(self) -> None:
        for value in ("nan", "inf", "-inf", float("nan"), float("inf"), 1e300, -1):
            with self.subTest(value=value):
                node = self.export(self.state(stats={"uptime_seconds": value, "clients_2g": 2}))
                self.assertNotIn("uptime", node)
                self.assertEqual(2, node["clients"])
        node = self.export(self.state(
            info={"name": "Example", "location_latitude": "nan", "location_longitude": "inf"},
            stats={"load_avg_5": "inf", "mem_total": "nan", "mem_free": 25},
        ))
        for field in ("location", "loadavg", "memory_usage"):
            self.assertNotIn(field, node)

    def test_finite_memory_multiplication_overflow_and_blank_uptime(self) -> None:
        parsed = parse_payload({"data": {"system": {"uptime": "   "},
                                         "statistic": {"meminfo_MemTotal": "1e308 GiB"}}})
        self.assertNotIn("mem_total", parsed.stats)
        self.assertNotIn("uptime_seconds", parsed.stats)

    def test_contact_name_is_url_decoded_exactly_once(self) -> None:
        for encoded, decoded in (("A%2BB", "A+B"), ("A+B", "A B"),
                                 ("A%252BB", "A%2BB"), ("100%25+ready%2B", "100% ready+")):
            with self.subTest(encoded=encoded):
                parsed = parse_payload({"data": {"contact": {"name": encoded}}})
                self.assertEqual(decoded, parsed.info["name"])
                node = self.export(self.state(info=parsed.info))
                self.assertEqual(f"{decoded} (1001)", node["hostname"])


if __name__ == "__main__":
    unittest.main()
