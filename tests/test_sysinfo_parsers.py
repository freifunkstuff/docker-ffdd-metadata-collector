from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.sysinfo_parsers import detect_variant, parse_payload  # noqa: E402


class SysinfoParserTests(unittest.TestCase):
    def make_payload(
        self,
        *,
        version: str = "18",
        node_type: str = "node",
        common: dict | None = None,
        system: dict | None = None,
        statistic: dict | None = None,
        network_switch: dict | None = None,
        airtime: dict | None = None,
        bmxd: dict | None = None,
        backbone: dict | None = None,
        traffic_shaping: dict | None = None,
        connections: list | None = None,
    ) -> dict:
        payload = {
            "version": version,
            "timestamp": "1774642113",
            "data": {
                "firmware": {
                    "version": "8.2.7",
                    "DISTRIB_DESCRIPTION": "OpenWrt 22.03.7",
                },
                "system": {
                    "uptime": "5309950.40 19930990.00",
                    "cpu_load": "0.14 0.32 0.35 1/102 25686",
                    "uname": "Linux test 5.10.221",
                    "board": "board",
                    "model": "fallback-model",
                    "model2": "Preferred Model",
                    "cpucount": "4",
                    "autoupdate": 1,
                    "node_type": node_type,
                },
                "common": {
                    "community": "Dresden",
                    "group_id": "0",
                    "node": "1001",
                    "domain": "freifunk-dresden.de",
                    "ip": "10.200.3.237",
                    "network_id": "0",
                },
                "backbone": {
                    "fastd_pubkey": "fastd-key",
                    "wg_pubkey": "wg-key",
                },
                "gps": {
                    "latitude": 51.0,
                    "longitude": 13.7,
                    "altitude": 100,
                },
                "contact": {
                    "name": "Daniel",
                    "location": "Somewhere",
                    "email": "daniel%40example.org",
                    "note": "",
                },
                "statistic": {
                    "interfaces": {
                        "wifi2_rx": "123",
                        "wifi2_tx": "456",
                    },
                    "client2g": {"1min": 1},
                    "client5g": {"1min": 2},
                    "meminfo_MemTotal": "122124 kB",
                    "meminfo_MemFree": "69192 kB",
                    "cpu_load": "0.14 0.32 0.35 1/102 25686",
                },
                "bmxd": {
                    "links": [
                        {"node": "4", "rq": "99", "tq": "98", "type": "wifi_mesh"},
                    ],
                    "gateways": {
                        "selected": "10.200.0.202",
                        "preferred": "10.200.0.202",
                    },
                },
                "airtime": {
                    "radio2g": "1,2,3,4",
                    "radio5g": "5,6,7,8",
                },
                "network_switch": {
                    "switch": [
                        {"port": "1 (lan1)", "carrier": "1", "speed": "1000"},
                        {"port": "0 (wan)", "carrier": "0", "speed": "1000"},
                    ]
                },
                "opkg": {"packages": []},
            },
        }

        if common:
            payload["data"]["common"].update(common)
        if system:
            payload["data"]["system"].update(system)
        if statistic is not None:
            payload["data"]["statistic"] = statistic
        if network_switch is not None:
            payload["data"]["network_switch"] = network_switch
        if airtime is not None:
            payload["data"]["airtime"] = airtime
        if bmxd is not None:
            payload["data"]["bmxd"] = bmxd
        if backbone is not None:
            payload["data"]["backbone"] = backbone
        if traffic_shaping is not None:
            payload["data"]["traffic_shaping"] = traffic_shaping
        if connections is not None:
            payload["data"]["connections"] = connections
        return payload

    def test_detect_variant_v18_node(self) -> None:
        payload = self.make_payload(version="18", node_type="node")
        variant = detect_variant(payload)
        self.assertEqual("v18-node", variant.key)

    def test_detect_variant_v18_server(self) -> None:
        payload = self.make_payload(version="18", node_type="server")
        variant = detect_variant(payload)
        self.assertEqual("v18-server", variant.key)

    def test_detect_variant_unknown_goes_generic(self) -> None:
        payload = self.make_payload(version="99", node_type="node")
        variant = detect_variant(payload)
        self.assertEqual("generic:99:node", variant.key)

    def test_parse_server_city_as_community_without_warning(self) -> None:
        payload = self.make_payload(
            version="18",
            node_type="server",
            common={"community": None, "city": "Dresden"},
            statistic={
                "meminfo_MemTotal": "239616 kB",
                "meminfo_MemFree": "189152 kB",
                "cpu_load": "0.00 0.01 0.00 1/68 30601",
            },
        )
        result = parse_payload(payload)
        self.assertEqual("server", result.node_type)
        self.assertEqual("Dresden", result.info["community"])
        self.assertEqual("data.common.city", result.field_sources["community"])
        self.assertNotIn("community_fallback_from_city", result.parse_warnings)
        self.assertIn("server_missing_interfaces_block", result.parse_warnings)

    def test_parse_model_fallback_without_warning(self) -> None:
        payload = self.make_payload(system={"model2": "", "model": "Fallback Only"})
        result = parse_payload(payload)
        self.assertEqual("Fallback Only", result.info["model"])
        self.assertEqual("data.system.model", result.field_sources["model"])
        self.assertNotIn("model_fallback_from_model", result.parse_warnings)

    def test_parse_switch0_ports(self) -> None:
        payload = self.make_payload(
            version="17",
            node_type="server",
            network_switch={
                "dsa": False,
                "switch0": [
                    {"port": "0", "carrier": "up", "speed": "1000baseT"},
                    {"port": "1", "carrier": "down", "speed": ""},
                ],
            },
        )
        result = parse_payload(payload)
        self.assertEqual("17", result.version)
        self.assertEqual(2, len(result.stats["switch_ports"]))
        self.assertEqual(1000, result.stats["switch_ports"][0]["status_mbps"])
        self.assertEqual(0, result.stats["switch_ports"][1]["status_mbps"])
        self.assertNotIn("network_switch_present_but_unparsed", result.parse_warnings)

    def test_parse_v16_server_with_traffic_shaping_and_wan_lan_switch(self) -> None:
        payload = self.make_payload(
            version="16",
            node_type="server",
            common={"community": None, "city": "Dresden", "fastd_pubkey": "legacy-fastd"},
            backbone={"wg_pubkey": "wg-key"},
            network_switch={
                "wan": {"carrier": "up", "speed": "1000baseT"},
                "lan": {"carrier": "down", "speed": "100baseT"},
            },
            traffic_shaping={
                "enabled": 1,
                "network": "wan",
                "incomming": "100mbit",
                "outgoing": "40mbit",
            },
        )
        result = parse_payload(payload)
        self.assertEqual("16", result.version)
        self.assertEqual("Dresden", result.info["community"])
        self.assertEqual("legacy-fastd", result.info["backbone_fastd_pubkey"])
        self.assertEqual(2, len(result.stats["switch_ports"]))
        self.assertTrue(result.stats["traffic_shaping_enabled"])
        self.assertEqual("wan", result.stats["traffic_shaping_network"])
        self.assertNotIn("network_switch_present_but_unparsed", result.parse_warnings)

    def test_parse_links_normalizes_sides(self) -> None:
        payload = self.make_payload(
            common={"node": "1001"},
            bmxd={
                "links": [
                    {"node": "999", "rq": "80", "tq": "81", "type": "backbone"},
                    {"node": "1005", "rq": "90", "tq": "91", "type": "wifi_mesh"},
                ],
                "gateways": {"selected": "10.0.0.1", "preferred": "10.0.0.2"},
            },
        )
        result = parse_payload(payload)
        self.assertEqual(2, len(result.links))
        first = result.links[0]
        second = result.links[1]
        self.assertEqual("1001", first["left_node_id"])
        self.assertEqual("999", first["right_node_id"])
        self.assertEqual(80, first["left_rq"])
        self.assertEqual("1001", second["left_node_id"])
        self.assertEqual("1005", second["right_node_id"])
        self.assertEqual(90, second["left_rq"])

    def test_parse_mobile_variant(self) -> None:
        payload = self.make_payload(version="18", node_type="mobile")
        result = parse_payload(payload)
        self.assertEqual("18", result.version)
        self.assertEqual("mobile", result.info["node_type"])

    def test_parse_decodes_urlencoded_contact_fields(self) -> None:
        payload = self.make_payload()
        payload["data"]["contact"].update(
            {
                "name": "Glas+%26+Bohne",
                "location": "Leipzig+Sued",
                "email": "foo%2Bbar%40example.org",
                "note": "Cafe%2BRouter",
            }
        )

        result = parse_payload(payload)

        self.assertEqual("Glas & Bohne", result.info["name"])
        self.assertEqual("Leipzig Sued", result.info["location"])
        self.assertEqual("foo+bar@example.org", result.info["contact_email"])
        self.assertEqual("Cafe+Router", result.info["note"])


if __name__ == "__main__":
    unittest.main()
