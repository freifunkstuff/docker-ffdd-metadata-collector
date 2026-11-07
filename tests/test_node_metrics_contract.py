"""Real Node renderer -> collector -> public metrics and meshviewer contract."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metadata_collector.sysinfo_parsers import parse_payload
from metadata_collector.models import NodeState
from metadata_collector.metrics_exporter import VictoriametricsExporter
from metadata_collector.meshviewer import build_meshviewer_document

BASE_DIR = Path('/usr/local/bin')
if not (BASE_DIR / 'sysinfo.py').exists():
    BASE_DIR = Path(__file__).resolve().parents[2] / 'dockernode' / 'scripts'


@unittest.skipUnless((BASE_DIR / 'sysinfo.py').exists(), 'Node base producer unavailable')
class NodeMetricsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(BASE_DIR))
        spec = importlib.util.spec_from_file_location('node_metrics_producer_contract', BASE_DIR / 'sysinfo.py')
        cls.producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.producer)

    def render(self, node_type):
        # Explicit config works with the Docker 1.0.1 base too: this is a renderer
        # contract, not a dependency on a newer NODE_TYPE environment schema.
        config = {'system': {'node_type': node_type, 'autoupdate': False},
                  'node': {'community': 'Dresden',
                           'common': {'group_id': 0, 'domain': 'freifunk-dresden.de', 'network_id': 0},
                           'gps': {'latitude': 51.05, 'longitude': 13.74, 'altitude': 110},
                           'contact': {'name': 'Offloader', 'email': 'owner@example.org', 'location': 'Dresden', 'note': ''}}}
        state = {'registration': {'node_id': 52001}, 'wireguard': {'public_key': 'test-public-only'}}
        # Interface collection's aggregation is producer-unit-tested. Feed its
        # aggregate result here, never invoke host networking/processes or keys.
        mocks = {'_safe_command': '', '_read_os_release': {}, '_read_uptime_raw': '120 0',
                 '_container_uptime_seconds': 120, '_read_nameservers': [], '_read_cpuinfo': '',
                 '_effective_meminfo': {'MemTotal': '1024 kB', 'MemFree': '512 kB'},
                 '_read_text': '', '_read_cpu_stat': '', '_read_gateway_usage': [],
                 '_derive_fastd_pubkey': 'test-fastd-public', '_wireguard_pubkey': 'test-wg-public',
                 '_read_interface_stats': {'tbb_wg_rx': '123456789', 'tbb_wg_tx': '987654321', 'tbb_fastd_rx': '0'}}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'SYSINFO_MODEL': 'fallback', 'SYSINFO_MODEL2': 'Offloader Model', 'NODE_TYPE': node_type}))
            for name, value in mocks.items():
                # Stable base 1.0.1 predates publishing the WG public key.
                # Counter/role contracts do not require that optional API.
                if name == '_wireguard_pubkey' and not hasattr(self.producer, name):
                    continue
                stack.enter_context(patch.object(self.producer, name, return_value=value))
            return self.producer.render_stub_payload(config, state, {'bmxd': {'links': [], 'gateways': {'selected': '', 'preferred': '', 'gateways': []}, 'info': []}})

    def consume(self, payload):
        result = parse_payload(payload)
        now = datetime.now(timezone.utc)
        state = NodeState(node_id=result.node_id, primary_ip=result.info['primary_ip'],
                          first_seen_at=now.isoformat(), last_success_at=now.isoformat(),
                          info=result.info, stats=result.stats, node_type=result.node_type)
        metrics = VictoriametricsExporter('http://unused.invalid').build_payload([state], now)
        document = build_meshviewer_document(now.isoformat(), [state], 600, 3600, 30)
        return result, metrics, document['nodes'][0]

    def test_offloader_renderer_preserves_node_role_position_model_and_exact_wg_bytes(self):
        result, metrics, node = self.consume(self.render('node'))
        self.assertEqual(result.node_type, 'node')
        self.assertFalse(node['is_gateway'])
        self.assertEqual(node['location']['latitude'], 51.05)
        self.assertEqual(node['location']['longitude'], 13.74)
        self.assertEqual(node['model'], 'Offloader Model')
        self.assertEqual(result.stats['mem_total'], 1024 * 1024)
        for direction, expected in [('rx', 123456789), ('tx', 987654321)]:
            lines = [line for line in metrics.splitlines() if line.startswith(f'node_traffic_backbone_wg_{direction}_bytes{{')]
            self.assertEqual(len(lines), 1)
            self.assertEqual(float(lines[0].rsplit(' ', 1)[1]), expected)

    def test_server_renderer_and_nested_v18_use_existing_metrics_without_totals(self):
        payload = self.render('server')
        payload['version'] = '18'
        statistic = payload['data']['statistic']
        statistic.pop('interfaces')
        statistic['network'] = {'backbone_mesh_rx': '999999999', 'wan_rx': '888888888',
                                'details': {'backbone_mesh_wg_rx': '123456789', 'backbone_mesh_wg_tx': '0',
                                            'backbone_mesh_fastd_rx': '42', 'backbone_mesh_fastd_tx': '43'}}
        result, metrics, node = self.consume(payload)
        self.assertTrue(node['is_gateway'])
        self.assertNotIn('server_missing_interfaces_block', result.parse_warnings)
        self.assertEqual(result.stats['traffic_backbone_wg_rx'], 123456789)
        self.assertEqual(result.stats['traffic_backbone_wg_tx'], 0)
        self.assertIn('node_traffic_backbone_fastd_rx_bytes{', metrics)
        self.assertNotIn('999999999', metrics)


if __name__ == '__main__':
    unittest.main()
