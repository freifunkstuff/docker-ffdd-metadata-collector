"""Optional resource normalization and real producer/exporter contracts."""
from __future__ import annotations

import unittest

if __package__:
    from . import test_node_metrics_contract as node_contract
else:
    import test_node_metrics_contract as node_contract
from metadata_collector.sysinfo_parsers import parse_payload


class ResourceParserTests(unittest.TestCase):
    def parse(self, resources):
        return parse_payload({'data': {'resources': resources, 'system': {'uptime': '12 0'},
                              'statistic': {'cpu_load': '1 2 3', 'meminfo_MemTotal': '7 kB',
                                            'meminfo_MemFree': '3 kB'}}})

    def test_exact_bytes_and_fractional_capacity(self):
        result = self.parse({'scope': 'container', 'cpu_capacity': 1.5,
                             'memory_limit_bytes': 1025, 'memory_remaining_bytes': 513,
                             'memory_current_bytes': 512, 'cpu_total_seconds': 123})
        self.assertEqual(result.info['resource_scope'], 'container')
        self.assertEqual(result.stats['mem_total'], 1025)
        self.assertEqual(result.stats['mem_free'], 513)
        self.assertEqual(result.stats['cpu_capacity'], 1.5)
        self.assertEqual(result.stats['cpu_usage_seconds_total'], 123)
        self.assertNotIn('load_avg_5', result.stats)

    def test_missing_and_untrusted_scope_fall_back(self):
        for resource in (None, {}, {'scope': 'unknown', 'memory_limit_bytes': 1}):
            result = self.parse(resource)
            self.assertEqual(result.stats['mem_total'], 7 * 1024)
            self.assertEqual(result.stats['load_avg_5'], 2)
            self.assertNotIn('resource_scope', result.info)
        self.assertNotIn('load_avg_5', self.parse({'scope': 'container'}).stats)

    def test_container_unknowns_never_fall_back_to_host(self):
        for resources in ({'scope': 'container'},
                          {'scope': 'container', 'cpu_total_seconds': 123}):
            result = parse_payload({'data': {
                'resources': resources,
                'system': {'cpucount': '8', 'uptime': '10000 0'},
                'statistic': {'cpu_load': '1 2 3', 'meminfo_MemTotal': '16777216 kB',
                              'meminfo_MemFree': '8388608 kB'}}})
            self.assertNotIn('cpu_count', result.info)
            for key in ('mem_total', 'mem_free', 'uptime_seconds', 'load_avg_5'):
                self.assertNotIn(key, result.stats)
            if 'cpu_total_seconds' in resources:
                self.assertEqual(result.stats['cpu_usage_seconds_total'], 123)

    def test_zero_capacity_invalid_but_zero_counters_valid(self):
        result = self.parse({'scope': 'container', 'cpu_capacity': 0,
                             'cpu_total_seconds': 0, 'cpu_throttled_seconds': 0})
        self.assertNotIn('cpu_capacity', result.stats)
        self.assertNotIn('cpu_count', result.info)
        self.assertEqual(result.stats['cpu_usage_seconds_total'], 0)
        self.assertEqual(result.stats['cpu_throttled_seconds_total'], 0)

    def test_invalid_numeric_fields_omitted(self):
        for value in (True, False, -1, float('nan'), float('inf'), '12', None, {}, []):
            with self.subTest(value=value):
                result = self.parse({'scope': 'container', 'cpu_total_seconds': value,
                                     'cpu_capacity': value, 'memory_current_bytes': value})
                for key in ('cpu_usage_seconds_total', 'cpu_capacity', 'memory_used_bytes'):
                    self.assertNotIn(key, result.stats)

    def test_host_load_and_zero(self):
        result = self.parse({'scope': 'host', 'load_average': [1, 0, 3],
                             'memory_current_bytes': 0, 'cpu_total_seconds': 0})
        self.assertEqual(result.stats['load_avg_5'], 0)
        self.assertEqual(result.stats['memory_used_bytes'], 0)
        self.assertEqual(result.stats['cpu_usage_seconds_total'], 0)


class ResourceProducerContractTests(node_contract.NodeMetricsContractTests):
    def test_container_resources_reach_metrics_without_load_or_label_churn(self):
        if not hasattr(self.producer, '_resource_snapshot'):
            self.skipTest('Stable Node base predates optional resources')
        payload = self.render('node')
        result, metrics, _ = self.consume(payload)
        self.assertEqual(result.info['resource_scope'], 'container')
        self.assertEqual(payload['data']['resources']['cpu_count'], 8)
        self.assertEqual(result.info['cpu_count'], 2)
        self.assertEqual(result.info['cpu_allowed_count'], 8)
        for name, value in [('node_cpu_usage_seconds_total', 123), ('node_cpu_capacity', 2),
                            ('node_memory_used_bytes', 524288)]:
            line = next(line for line in metrics.splitlines() if line.startswith(name + '{'))
            self.assertEqual(float(line.rsplit(' ', 1)[1]), value)
        self.assertNotIn('node_load{', metrics)
        self.assertNotIn('resource_scope=', metrics)
        self.assertNotIn('sampled_at', metrics)

    def test_unlimited_container_can_export_used_without_total(self):
        if not hasattr(self.producer, '_resource_snapshot'):
            self.skipTest('Stable Node base predates optional resources')
        snapshot = self.producer.ResourceSnapshot(scope='container', sampled_at=1,
                                                 memory_current_bytes=12345)
        payload = self.render('node', resource_snapshot=snapshot)
        result, metrics, _ = self.consume(payload)
        self.assertEqual(result.stats['memory_used_bytes'], 12345)
        self.assertNotIn('node_memory_total{', metrics)
        self.assertNotIn('node_load{', metrics)


if __name__ == '__main__':
    unittest.main()
