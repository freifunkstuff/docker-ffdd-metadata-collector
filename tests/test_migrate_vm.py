from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-victoriametrics.py"
_spec = importlib.util.spec_from_file_location("migrate_vm", _SCRIPT)
migrate_vm = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(migrate_vm)


class RenameTest(unittest.TestCase):
    def test_renames_metric_name_value_and_label_keys(self) -> None:
        metric = {"__name__": "node_clients.total", "nodeid": "1727", "domain": "Leipzig"}
        self.assertEqual(
            migrate_vm.rename(metric),
            {"__name__": "node_clients_total", "nodeid": "1727", "domain": "Leipzig"},
        )

    def test_renames_link_label_keys_not_values(self) -> None:
        metric = {"__name__": "link_tq", "source.id": "14", "source.hostname": "a.b.c"}
        out = migrate_vm.rename(metric)
        self.assertEqual(out["__name__"], "link_tq")
        self.assertEqual(out["source_id"], "14")
        # dotted value must stay intact
        self.assertEqual(out["source_hostname"], "a.b.c")

    def test_transform_line_roundtrip(self) -> None:
        line = json.dumps(
            {"metric": {"__name__": "node_memory.airtime_2g_busy", "source.id": "5"}, "values": [1], "timestamps": [1000]}
        ).encode()
        obj = json.loads(migrate_vm.transform_line(line))
        self.assertEqual(obj["metric"]["__name__"], "node_memory_airtime_2g_busy")
        self.assertIn("source_id", obj["metric"])
        self.assertEqual(obj["values"], [1])
        self.assertEqual(obj["timestamps"], [1000])

    def test_transform_skips_blank(self) -> None:
        self.assertIsNone(migrate_vm.transform_line(b"  "))


class DownsampleTest(unittest.TestCase):
    def test_keeps_last_sample_per_bucket(self) -> None:
        # interval 300s = 300000 ms; samples at 0s, 60s, 240s (bucket 0) and 360s (bucket 1)
        values = [10, 11, 12, 20]
        timestamps = [0, 60_000, 240_000, 360_000]
        vals, ts = migrate_vm.downsample(values, timestamps, 300_000)
        # bucket 0 -> last sample (240s, 12), bucket 1 -> (360s, 20)
        self.assertEqual(ts, [240_000, 360_000])
        self.assertEqual(vals, [12, 20])

    def test_transform_line_downsamples(self) -> None:
        line = json.dumps(
            {
                "metric": {"__name__": "node_clients.total", "nodeid": "1"},
                "values": [1, 2, 3],
                "timestamps": [0, 100_000, 360_000],
            }
        ).encode()
        obj = json.loads(migrate_vm.transform_line(line, 300_000))
        self.assertEqual(obj["metric"]["__name__"], "node_clients_total")
        self.assertEqual(obj["timestamps"], [100_000, 360_000])
        self.assertEqual(obj["values"], [2, 3])

    def test_interval_zero_keeps_all(self) -> None:
        line = json.dumps(
            {"metric": {"__name__": "x", "a": "b"}, "values": [1, 2], "timestamps": [0, 1000]}
        ).encode()
        obj = json.loads(migrate_vm.transform_line(line, 0))
        self.assertEqual(obj["values"], [1, 2])


if __name__ == "__main__":
    unittest.main()
