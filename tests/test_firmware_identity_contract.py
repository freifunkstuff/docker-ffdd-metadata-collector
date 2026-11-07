from datetime import datetime, timezone
import unittest

from metadata_collector.sysinfo_parsers import parse_payload
from metadata_collector.models import NodeState
from metadata_collector.meshviewer import build_meshviewer_document


class FirmwareIdentityContractTests(unittest.TestCase):
    def payload(self, version, implementation='dockernode'):
        firmware = {'version': version, 'DISTRIB_DESCRIPTION': 'Alpine Linux v3.23',
                    'DISTRIB_RELEASE': '3.23.0'}
        if implementation:
            firmware['name'] = implementation
        return {'version': '17', 'data': {'firmware': firmware,
                'system': {'node_type': 'node'},
                'common': {'node': '1468', 'ip': '10.200.5.194', 'community': 'Leipzig'},
                'statistic': {}}}

    def test_node_build_identity_is_displayed_without_os_suffix(self):
        for version in ('dockernode / master / abcdef1', 'dockernode / 1.2.3'):
            with self.subTest(version=version):
                parsed = parse_payload(self.payload(version))
                self.assertEqual(parsed.info['firmware_release'], version)
                self.assertNotIn('firmware_base', parsed.info)
                self.assertEqual(parsed.info['os_description'], 'Alpine Linux v3.23')
                self.assertEqual(parsed.info['os_release'], '3.23.0')
                now = datetime.now(timezone.utc)
                state = NodeState(node_id='1468', primary_ip='10.200.5.194',
                                  first_seen_at=now.isoformat(), last_success_at=now.isoformat(),
                                  info=parsed.info, stats=parsed.stats, node_type='node')
                node = build_meshviewer_document(now.isoformat(), [state], 600, 3600, 30)['nodes'][0]
                self.assertEqual(node['firmware'], {'release': version})

    def test_other_firmware_preserves_legacy_base_mapping(self):
        parsed = parse_payload(self.payload('9.0.0', implementation=None))
        self.assertEqual(parsed.info['firmware_release'], '9.0.0')
        self.assertEqual(parsed.info['firmware_base'], 'Alpine Linux v3.23')
