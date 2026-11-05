"""Producer/consumer tests against the actual installed (or sibling) Node code."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metadata_collector.node_list_sources import FileJsonNodeListSource, parse_node_list_payload

BASE_DIR = Path("/usr/local/bin")
if not (BASE_DIR / "sysinfo.py").exists():
    BASE_DIR = Path(__file__).resolve().parents[2] / "dockernode" / "scripts"


@unittest.skipUnless((BASE_DIR / "sysinfo.py").exists(), "Node base producer not installed; Docker build runs this contract")
class NodeBaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(BASE_DIR))
        spec = importlib.util.spec_from_file_location("node_base_sysinfo_contract", BASE_DIR / "sysinfo.py")
        cls.producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.producer)

    def payload(self, node_id=52001, online=True):
        def command(args):
            if online and args == ["bmxd", "-c", "--originators"]:
                return "10.200.4.23 tbb_fastd 10.201.200.21 92 95 0:00:10:00 123 0\n"
            return ""
        with patch.object(self.producer, "_safe_command", side_effect=command), patch.object(self.producer, "_read_gateway_usage_map", return_value={}):
            return self.producer.build_nodes_payload(
                config={"node": {"community": "Dresden", "common": {"domain": "freifunk-dresden.de", "network_id": "0"}}},
                state={"registration": {"node_id": node_id}} if node_id is not None else {},
            )

    def test_real_producer_output_is_consumed_via_default_file_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "nodes.json"
            source.write_text(json.dumps(self.payload()))
            nodes = asyncio.run(FileJsonNodeListSource(source).fetch_nodes())
        self.assertEqual({n.node_id: n.primary_ip for n in nodes}, {"1042": "10.200.4.23", "52001": "10.200.203.237"})

    def test_registered_node_remains_discoverable_without_mesh_neighbours(self):
        nodes = parse_node_list_payload(self.payload(online=False))
        self.assertEqual([(n.node_id, n.primary_ip) for n in nodes], [("52001", "10.200.203.237")])

    def test_unregistered_empty_base_snapshot_is_safe(self):
        self.assertEqual(parse_node_list_payload(self.payload(node_id=None, online=False)), [])


if __name__ == "__main__":
    unittest.main()
