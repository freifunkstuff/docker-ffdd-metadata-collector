from __future__ import annotations

import os
import sys
import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.config import MetadataCollectorConfig  # noqa: E402
from metadata_collector.app import MetadataCollectorApp  # noqa: E402
from metadata_collector.models import DiscoveredNode, FetchOutcome, NodeState, ParseResult  # noqa: E402
from metadata_collector.node_list_sources import FileJsonNodeListSource, parse_node_list_payload  # noqa: E402
from metadata_collector.scheduler import classify_poll_mode, compute_next_poll_at, fetch_timeout_for_mode  # noqa: E402
from metadata_collector.snapshot import build_fetch_summary, build_snapshot_document, build_status_document  # noqa: E402
from metadata_collector.storage import YamlBackedMemoryStore  # noqa: E402


class FakeSource:
    def __init__(self, nodes: list[DiscoveredNode]) -> None:
        self._nodes = nodes

    async def fetch_nodes(self) -> list[DiscoveredNode]:
        return list(self._nodes)


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def schedule(self, node_id: str, due_at: object) -> None:
        self.calls.append((node_id, due_at))


class FakeStore:
    def __init__(self, states: list[NodeState]) -> None:
        self._states = {state.node_id: state for state in states}
        self.merged_nodes: list[DiscoveredNode] = []

    def initialize(self) -> None:
        return None

    def merge_discovered_nodes(self, nodes: list[DiscoveredNode], discovered_at: str) -> None:
        self.merged_nodes = list(nodes)
        for node in nodes:
            state = self._states.get(node.node_id)
            if state is None:
                self._states[node.node_id] = NodeState(
                    node_id=node.node_id,
                    primary_ip=node.primary_ip,
                    first_seen_at=discovered_at,
                    last_source_seen_at=node.last_seen or discovered_at,
                )
            else:
                state.primary_ip = node.primary_ip
                state.last_source_seen_at = node.last_seen or discovered_at

    def get_node_state(self, node_id: str) -> NodeState | None:
        return self._states.get(node_id)

    def list_node_states(self) -> list[NodeState]:
        return list(self._states.values())

    def apply_fetch_outcome(self, outcome: FetchOutcome) -> None:
        return None


class ScaffoldTests(unittest.TestCase):
    def test_parse_node_list_payload_from_bmxd_snapshot(self) -> None:
        payload = {
            "bmxd": {
                "originators": [
                    {"node": "1001", "ip": "10.0.0.1"},
                    {"node": "1002", "ip": "10.0.0.2"},
                ],
                "gateways": {
                    "gateways": [
                        {"node": "1001", "ip": "10.0.0.1"},
                        {"node": "1003", "ip": "10.0.0.3"},
                    ]
                },
            }
        }

        nodes = parse_node_list_payload(payload)
        self.assertEqual(["1001", "1002"], [node.node_id for node in nodes])
        self.assertEqual("10.0.0.2", nodes[-1].primary_ip)

    def test_parse_node_list_payload_ignores_local_node_and_gateways(self) -> None:
        payload = {
            "timestamp": "1774740830",
            "node": {"id": "51082", "ip": "10.200.200.83"},
            "bmxd": {
                "originators": [
                    {"node": "51010", "ip": "10.200.200.11"},
                    {"node": "51015", "ip": "10.200.200.16"},
                ],
                "gateways": {
                    "gateways": [
                        {"node": "99999", "ip": "10.9.9.9"},
                    ]
                },
            },
        }

        nodes = parse_node_list_payload(payload)

        self.assertEqual(["51010", "51015"], [node.node_id for node in nodes])

    def test_state_store_roundtrip_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            store = YamlBackedMemoryStore(
                discovery_state_path=base / "discovery.yaml",
                node_info_dir=base / "info",
                node_status_dir=base / "status",
            )
            store.initialize()
            store.merge_discovered_nodes(
                [
                    type("Node", (), {"node_id": "1001", "primary_ip": "10.0.0.1", "last_seen": "2025-01-01T00:00:00+00:00"})(),
                ],
                "2025-01-01T00:00:00+00:00",
            )

            parse_result = ParseResult(
                node_id="1001",
                version="18",
                timestamp="1774642113",
                node_type="node",
                parser_name="GenericSysinfoParser",
                info={"community": "Dresden"},
                stats={"uptime_seconds": 12.0},
                links=[{"type": "wifi_mesh", "left_node_id": "1001", "right_node_id": "1002", "left_rq": 90}],
                parse_warnings=[],
                field_sources={"community": "data.common.community"},
            )
            outcome = FetchOutcome(
                node_id="1001",
                primary_ip="10.0.0.1",
                fetched_at="2025-01-01T00:01:00+00:00",
                success=True,
                parse_result=parse_result,
            )
            store.apply_fetch_outcome(outcome)

            state = store.get_node_state("1001")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("Dresden", state.info["community"])
            self.assertEqual("2025-01-01T00:01:00+00:00", state.last_success_at)

            discovery_document = yaml.safe_load((base / "discovery.yaml").read_text())
            info_document = yaml.safe_load((base / "info" / "1001.yaml").read_text())
            status_document = yaml.safe_load((base / "status" / "1001.yaml").read_text())

            self.assertEqual("10.0.0.1", discovery_document["nodes"]["1001"]["primary_ip"])
            self.assertEqual("Dresden", info_document["info"]["community"])
            self.assertEqual("18", info_document["version"])
            self.assertEqual(12.0, status_document["stats"]["uptime_seconds"])
            self.assertEqual("1774642113", str(status_document["timestamp"]))
            self.assertEqual(1, len(status_document["request_history"]))
            self.assertEqual("success", status_document["request_history"][0]["result"])

            reloaded_store = YamlBackedMemoryStore(
                discovery_state_path=base / "discovery.yaml",
                node_info_dir=base / "info",
                node_status_dir=base / "status",
            )
            reloaded_store.initialize()
            reloaded_state = reloaded_store.get_node_state("1001")
            self.assertIsNotNone(reloaded_state)
            assert reloaded_state is not None
            self.assertEqual("Dresden", reloaded_state.info["community"])
            self.assertEqual(1, len(reloaded_state.links))

            snapshot = build_snapshot_document("2025-01-01T00:02:00+00:00", reloaded_store.list_node_states())
            self.assertEqual("2025-01-01T00:02:00+00:00", snapshot["generatedAt"])
            self.assertEqual(1, len(snapshot["nodes"]))
            self.assertEqual(1, len(snapshot["links"]))
            self.assertNotIn("reachable", snapshot["nodes"][0])

            status = build_status_document(
                generated_at="2025-01-01T00:02:00+00:00",
                states=reloaded_store.list_node_states(),
                online_window_seconds=600.0,
                fetch_window_seconds=900.0,
                source_type="file-json",
                source="/run/freifunk/sysinfo/nodes.json",
            )
            self.assertEqual(1, status["nodes"]["total"])
            self.assertEqual(1, status["nodes"]["online"])
            self.assertEqual(1, status["nodes"]["withInfo"])
            self.assertEqual("file-json", status["collector"]["sourceType"])
            self.assertEqual(1, status["fetch"]["fetches"])
            self.assertEqual(0.067, status["fetch"]["ratePerMinute"])

    def test_fetch_failure_updates_stats_without_dropping_previous_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            store = YamlBackedMemoryStore(
                discovery_state_path=base / "discovery.yaml",
                node_info_dir=base / "info",
                node_status_dir=base / "status",
            )
            store.initialize()
            store.merge_discovered_nodes(
                [
                    type("Node", (), {"node_id": "1001", "primary_ip": "10.0.0.1", "last_seen": "2025-01-01T00:00:00+00:00"})(),
                ],
                "2025-01-01T00:00:00+00:00",
            )
            success = FetchOutcome(
                node_id="1001",
                primary_ip="10.0.0.1",
                fetched_at="2025-01-01T00:01:00+00:00",
                success=True,
                parse_result=ParseResult(
                    node_id="1001",
                    version="18",
                    timestamp="1774642113",
                    node_type="node",
                    parser_name="GenericSysinfoParser",
                    info={"community": "Dresden"},
                    stats={"uptime_seconds": 12.0},
                    links=[],
                    parse_warnings=[],
                    field_sources={},
                ),
            )
            store.apply_fetch_outcome(success)

            failure = FetchOutcome(
                node_id="1001",
                primary_ip="10.0.0.1",
                fetched_at="2025-01-01T00:02:00+00:00",
                success=False,
                error="TimeoutError: timed out",
            )
            store.apply_fetch_outcome(failure)

            state = store.get_node_state("1001")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("Dresden", state.info["community"])
            self.assertEqual(12.0, state.stats["uptime_seconds"])
            self.assertEqual(1, state.consecutive_failures)
            self.assertEqual("TimeoutError: timed out", state.fetch_error)
            self.assertEqual(2, len(state.request_history))
            self.assertEqual("timeout", state.request_history[-1]["result"])

    def test_reloading_prefers_discovery_last_source_seen_over_stale_info_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "info").mkdir(parents=True, exist_ok=True)
            (base / "status").mkdir(parents=True, exist_ok=True)
            (base / "discovery.yaml").write_text(
                yaml.safe_dump(
                    {
                        "generated_at": "2025-01-01T00:10:00+00:00",
                        "nodes": {
                            "1001": {
                                "primary_ip": "10.0.0.1",
                                "first_seen_at": "2025-01-01T00:00:00+00:00",
                                "last_source_seen_at": "2025-01-01T00:09:30+00:00",
                            }
                        },
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (base / "info" / "1001.yaml").write_text(
                yaml.safe_dump(
                    {
                        "node_id": "1001",
                        "primary_ip": "10.0.0.1",
                        "first_seen_at": "2025-01-01T00:00:00+00:00",
                        "last_source_seen_at": "2024-12-31T23:00:00+00:00",
                        "last_info_success_at": "2025-01-01T00:08:00+00:00",
                        "info": {"community": "Dresden"},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            store = YamlBackedMemoryStore(
                discovery_state_path=base / "discovery.yaml",
                node_info_dir=base / "info",
                node_status_dir=base / "status",
            )
            store.initialize()

            state = store.get_node_state("1001")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("2025-01-01T00:09:30+00:00", state.last_source_seen_at)
            self.assertEqual("2025-01-01T00:08:00+00:00", state.last_success_at)

    def test_purge_nodes_older_than_removes_persisted_node_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            store = YamlBackedMemoryStore(
                discovery_state_path=base / "discovery.yaml",
                node_info_dir=base / "info",
                node_status_dir=base / "status",
            )
            store.initialize()
            store.merge_discovered_nodes(
                [
                    type("Node", (), {"node_id": "1001", "primary_ip": "10.0.0.1", "last_seen": "2025-01-01T00:00:00+00:00"})(),
                    type("Node", (), {"node_id": "1002", "primary_ip": "10.0.0.2", "last_seen": "2024-08-01T00:00:00+00:00"})(),
                ],
                "2025-01-01T00:00:00+00:00",
            )
            store.apply_fetch_outcome(
                FetchOutcome(
                    node_id="1002",
                    primary_ip="10.0.0.2",
                    fetched_at="2024-08-01T00:00:00+00:00",
                    success=False,
                    error="TimeoutError: timed out",
                )
            )

            removed = store.purge_nodes_older_than(datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc), 90.0 * 24.0 * 3600.0)

            self.assertEqual(1, removed)
            self.assertIsNotNone(store.get_node_state("1001"))
            self.assertIsNone(store.get_node_state("1002"))
            self.assertFalse((base / "status" / "1002.yaml").exists())
            discovery_document = yaml.safe_load((base / "discovery.yaml").read_text())
            self.assertEqual(["1001"], sorted((discovery_document.get("nodes") or {}).keys()))

    def test_node_state_online_follows_last_seen_window(self) -> None:
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        online_state = NodeState(
            node_id="1001",
            primary_ip="10.0.0.1",
            first_seen_at="2025-01-01T00:00:00+00:00",
            last_source_seen_at="2025-01-01T00:05:01+00:00",
        )
        stale_state = NodeState(
            node_id="1002",
            primary_ip="10.0.0.2",
            first_seen_at="2025-01-01T00:00:00+00:00",
            last_source_seen_at="2024-12-31T23:59:00+00:00",
        )
        unknown_state = NodeState(
            node_id="1003",
            primary_ip="10.0.0.3",
            first_seen_at="2025-01-01T00:00:00+00:00",
        )

        self.assertTrue(online_state.is_online(now, 600.0))
        self.assertFalse(stale_state.is_online(now, 600.0))
        self.assertIsNone(unknown_state.is_online(now, 600.0))

    def test_scheduler_keeps_timeout_series_slow(self) -> None:
        config = MetadataCollectorConfig.from_env()
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        state = type("State", (), {
            "last_source_seen_at": now.isoformat(),
            "consecutive_failures": 2,
            "request_history": [
                {"success": False, "result": "timeout", "duration_ms": 2900},
                {"success": False, "result": "timeout", "duration_ms": 3000},
                {"success": True, "result": "success", "duration_ms": 2500},
            ],
        })()
        outcome = FetchOutcome(
            node_id="1001",
            primary_ip="10.0.0.1",
            fetched_at=now.isoformat(),
            success=False,
            error="TimeoutError: timed out",
            duration_ms=3000,
            result_kind="timeout",
        )
        due = compute_next_poll_at(config, state, outcome, now)
        self.assertEqual(config.poll_interval_very_slow_seconds, (due - now).total_seconds())
        self.assertEqual("very_slow", classify_poll_mode(config, state, now))
        self.assertEqual(60.0, fetch_timeout_for_mode(config, "very_slow"))

    def test_scheduler_needs_three_clean_successes_for_normal(self) -> None:
        config = MetadataCollectorConfig.from_env()
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        state = type("State", (), {
            "last_source_seen_at": now.isoformat(),
            "consecutive_failures": 0,
            "request_history": [
                {"success": False, "result": "timeout", "duration_ms": 3000},
                {"success": True, "result": "success", "duration_ms": 1200},
                {"success": True, "result": "success", "duration_ms": 1100},
            ],
        })()
        outcome = FetchOutcome(
            node_id="1001",
            primary_ip="10.0.0.1",
            fetched_at=now.isoformat(),
            success=True,
            duration_ms=1000,
            result_kind="success",
        )
        due = compute_next_poll_at(config, state, outcome, now)
        self.assertEqual(config.poll_interval_slow_seconds, (due - now).total_seconds())
        self.assertEqual("slow", classify_poll_mode(config, state, now))
        self.assertEqual(30.0, fetch_timeout_for_mode(config, "slow"))

    def test_scheduler_returns_to_normal_after_three_stable_successes(self) -> None:
        config = MetadataCollectorConfig.from_env()
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        state = type("State", (), {
            "last_source_seen_at": now.isoformat(),
            "consecutive_failures": 0,
            "request_history": [
                {"success": True, "result": "success", "duration_ms": 800},
                {"success": True, "result": "success", "duration_ms": 900},
                {"success": True, "result": "success", "duration_ms": 1000},
            ],
        })()
        outcome = FetchOutcome(
            node_id="1001",
            primary_ip="10.0.0.1",
            fetched_at=now.isoformat(),
            success=True,
            duration_ms=900,
            result_kind="success",
        )
        due = compute_next_poll_at(config, state, outcome, now)
        self.assertEqual(config.poll_interval_normal_seconds, (due - now).total_seconds())
        self.assertEqual("normal", classify_poll_mode(config, state, now))
        self.assertEqual(10.0, fetch_timeout_for_mode(config, "normal"))

    def test_discovery_schedules_only_new_nodes_immediately(self) -> None:
        config = MetadataCollectorConfig.from_env()
        app = MetadataCollectorApp(config)
        existing = NodeState(
            node_id="1001",
            primary_ip="10.0.0.1",
            first_seen_at="2025-01-01T00:00:00+00:00",
            last_source_seen_at="2025-01-01T00:00:00+00:00",
        )
        app.store = FakeStore([existing])
        app.source = FakeSource(
            [
                DiscoveredNode(node_id="1001", primary_ip="10.0.0.1", last_seen="2025-01-01T00:01:00+00:00"),
                DiscoveredNode(node_id="2002", primary_ip="10.0.0.2", last_seen="2025-01-01T00:01:00+00:00"),
            ]
        )
        app.scheduler = FakeScheduler()

        import asyncio

        asyncio.run(app._run_discovery_once())

        self.assertEqual(["2002"], [node_id for node_id, _ in app.scheduler.calls])

    def test_discovery_does_not_reset_existing_slow_nodes(self) -> None:
        config = MetadataCollectorConfig.from_env()
        app = MetadataCollectorApp(config)
        existing = NodeState(
            node_id="1777",
            primary_ip="10.0.0.7",
            first_seen_at="2025-01-01T00:00:00+00:00",
            last_source_seen_at="2025-01-01T00:05:00+00:00",
            consecutive_failures=4,
            request_history=[
                {"success": False, "result": "timeout", "duration_ms": 3000},
                {"success": False, "result": "timeout", "duration_ms": 3000},
                {"success": False, "result": "timeout", "duration_ms": 3000},
            ],
        )
        app.store = FakeStore([existing])
        app.source = FakeSource([DiscoveredNode(node_id="1777", primary_ip="10.0.0.7", last_seen="2025-01-01T00:06:00+00:00")])
        app.scheduler = FakeScheduler()

        import asyncio

        asyncio.run(app._run_discovery_once())

        self.assertEqual([], app.scheduler.calls)

    def test_summarize_states_reports_online_and_stale_breakdown(self) -> None:
        config = MetadataCollectorConfig.from_env()
        app = MetadataCollectorApp(config)
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)

        summary = app._summarize_states(
            [
                NodeState(
                    node_id="1001",
                    primary_ip="10.0.0.1",
                    first_seen_at="2025-01-01T00:00:00+00:00",
                    last_source_seen_at="2025-01-01T00:09:30+00:00",
                    last_success_at="2025-01-01T00:09:00+00:00",
                ),
                NodeState(
                    node_id="1002",
                    primary_ip="10.0.0.2",
                    first_seen_at="2025-01-01T00:00:00+00:00",
                    last_source_seen_at="2024-12-31T23:40:00+00:00",
                ),
                NodeState(
                    node_id="1003",
                    primary_ip="10.0.0.3",
                    first_seen_at="2025-01-01T00:00:00+00:00",
                ),
            ],
            now,
        )

        self.assertEqual(3, summary["total"])
        self.assertEqual(1, summary["online"])
        self.assertEqual(1, summary["stale"])
        self.assertEqual(1, summary["unknown"])
        self.assertEqual(2, summary["with_source_seen"])
        self.assertEqual(1, summary["with_success"])
        self.assertEqual(30.0, summary["freshest_age_seconds"])
        self.assertEqual(30.0, summary["stalest_online_age_seconds"])

    def test_build_fetch_summary_reports_window_rate(self) -> None:
        now = datetime(2025, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        summary = build_fetch_summary(
            now,
            [
                NodeState(
                    node_id="1001",
                    primary_ip="10.0.0.1",
                    first_seen_at="2025-01-01T00:00:00+00:00",
                    request_history=[
                        {"at": "2025-01-01T00:09:30+00:00"},
                        {"at": "2025-01-01T00:08:00+00:00"},
                    ],
                ),
                NodeState(
                    node_id="1002",
                    primary_ip="10.0.0.2",
                    first_seen_at="2025-01-01T00:00:00+00:00",
                    request_history=[
                        {"at": "2025-01-01T00:09:00+00:00"},
                        {"at": "2024-12-31T23:40:00+00:00"},
                    ],
                ),
            ],
            120.0,
        )

        self.assertEqual(3, summary["fetches"])
        self.assertEqual(1.5, summary["ratePerMinute"])
        self.assertEqual(120.0, summary["windowSeconds"])

    def test_run_writes_persistence_outputs_before_initial_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            old = dict(os.environ)
            try:
                os.environ["METADATA_COLLECTOR_DATA_DIR"] = str(base / "data")
                os.environ["METADATA_COLLECTOR_RUN_DIR"] = str(base / "run" / "freifunk" / "state")
                os.environ["METADATA_COLLECTOR_WEBROOT_DIR"] = str(base / "run" / "freifunk" / "www")
                config = MetadataCollectorConfig.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old)

            app = MetadataCollectorApp(config)
            app.store = FakeStore(
                [
                    NodeState(
                        node_id="1001",
                        primary_ip="10.0.0.1",
                        first_seen_at="2025-01-01T00:00:00+00:00",
                        last_source_seen_at="2025-01-01T00:09:30+00:00",
                    )
                ]
            )

            calls: list[str] = []

            app._create_store = lambda: app.store  # type: ignore[method-assign]
            app._bootstrap_scheduler = lambda: 1  # type: ignore[method-assign]
            app._install_signal_handlers = lambda: None  # type: ignore[method-assign]
            app._remove_signal_handlers = lambda: None  # type: ignore[method-assign]
            app._log_loaded_persistence_summary = lambda: None  # type: ignore[method-assign]

            async def fake_write_outputs(reason: str | None = None) -> None:
                calls.append(f"write:{reason}")

            async def fake_run_discovery_once(initial: bool = False) -> None:
                calls.append(f"discovery:{initial}")
                app.stop()

            app._write_outputs = fake_write_outputs  # type: ignore[method-assign]
            app._run_discovery_once = fake_run_discovery_once  # type: ignore[method-assign]

            import asyncio

            asyncio.run(app.run())

            self.assertEqual(
                ["write:startup-persistence", "discovery:True", "write:startup-discovery"],
                calls,
            )

    def test_config_paths_are_derived(self) -> None:
        old = dict(os.environ)
        try:
            os.environ["METADATA_COLLECTOR_DATA_DIR"] = "/tmp/edge-data"
            config = MetadataCollectorConfig.from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertTrue(config.node_metadata_path.is_absolute())
        self.assertTrue(config.status_path.is_absolute())
        self.assertTrue(config.discovery_state_path.is_absolute())
        self.assertEqual(Path("/run/freifunk/sysinfo/nodes.json").resolve(), config.source_path)
        self.assertEqual(Path("/run/freifunk/state").resolve(), config.run_dir)
        self.assertEqual(Path("/run/freifunk/www").resolve(), config.webroot_dir)
        self.assertEqual(config.state_dir / "info", config.node_info_dir)
        self.assertEqual(config.state_dir / "status", config.node_status_dir)
        self.assertEqual(config.run_dir / "node-metadata.json", config.node_metadata_path)
        self.assertEqual(config.run_dir / "node-metadata-status.json", config.status_path)
        self.assertEqual(config.webroot_dir / "node-metadata.json", config.published_node_metadata_path)
        self.assertEqual(config.webroot_dir / "node-metadata-status.json", config.published_status_path)
        self.assertEqual(10.0, config.fetch_timeout_normal_seconds)
        self.assertEqual(30.0, config.fetch_timeout_slow_seconds)
        self.assertEqual(60.0, config.fetch_timeout_very_slow_seconds)
        self.assertEqual(90.0 * 24.0 * 3600.0, config.node_retention_seconds)
        self.assertEqual(600.0, config.online_window_seconds)

    def test_defaults_file_is_used_and_env_overrides_it(self) -> None:
        old = dict(os.environ)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                defaults_path = Path(temp_dir) / "defaults.yaml"
                defaults_path.write_text(
                    yaml.safe_dump(
                        {
                            "METADATA_COLLECTOR_SOURCE": "file-json",
                            "METADATA_COLLECTOR_SOURCE_PATH": "/tmp/from-defaults.json",
                            "METADATA_COLLECTOR_FETCH_CONCURRENCY": "17",
                            "METADATA_COLLECTOR_LOG_LEVEL": "DEBUG",
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                os.environ["METADATA_COLLECTOR_DEFAULTS_PATH"] = str(defaults_path)
                os.environ["METADATA_COLLECTOR_FETCH_CONCURRENCY"] = "23"

                config = MetadataCollectorConfig.from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)

        self.assertEqual("file-json", config.source_type)
        self.assertEqual(Path("/tmp/from-defaults.json").resolve(), config.source_path)
        self.assertEqual(23, config.fetch_concurrency)
        self.assertEqual("DEBUG", config.log_level)

    def test_file_json_source_reads_nodes_from_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.json"
            path.write_text(
                '{"nodes": [{"id": "1001", "primary_ip": "10.0.0.1", "last_seen": "2025-01-01T00:00:00+00:00"}, {"id": "1002", "primary_ip": "10.0.0.2", "last_seen": "2025-01-01T00:01:00+00:00"}]}'
                ,
                encoding="utf-8",
            )

            import asyncio

            nodes = asyncio.run(FileJsonNodeListSource(path=path).fetch_nodes())

        self.assertEqual(["1001", "1002"], [node.node_id for node in nodes])
        self.assertEqual("10.0.0.2", nodes[1].primary_ip)

    def test_write_outputs_publishes_runtime_files_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            old = dict(os.environ)
            try:
                os.environ["METADATA_COLLECTOR_RUN_DIR"] = str(base / "run" / "freifunk" / "state")
                os.environ["METADATA_COLLECTOR_WEBROOT_DIR"] = str(base / "run" / "freifunk" / "www")
                config = MetadataCollectorConfig.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old)

            app = MetadataCollectorApp(config)
            app.store = FakeStore(
                [
                    NodeState(
                        node_id="1001",
                        primary_ip="10.0.0.1",
                        first_seen_at="2025-01-01T00:00:00+00:00",
                        last_source_seen_at="2025-01-01T00:05:00+00:00",
                        last_success_at="2025-01-01T00:05:30+00:00",
                        info={"community": "Dresden"},
                    )
                ]
            )

            import asyncio

            asyncio.run(app._write_outputs(reason="test"))

            self.assertTrue(config.node_metadata_path.exists())
            self.assertTrue(config.status_path.exists())
            self.assertTrue(config.published_node_metadata_path.is_symlink())
            self.assertTrue(config.published_status_path.is_symlink())
            self.assertEqual(config.node_metadata_path, config.published_node_metadata_path.resolve())
            self.assertEqual(config.status_path, config.published_status_path.resolve())

            node_metadata = json.loads(config.node_metadata_path.read_text(encoding="utf-8"))
            status = json.loads(config.status_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(node_metadata["nodes"]))
            self.assertEqual(1, status["nodes"]["total"])
            self.assertEqual("file-json", status["collector"]["sourceType"])
            self.assertEqual(str(config.source_path), status["collector"]["source"])

    def test_config_does_not_resolve_existing_published_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime_dir = base / "run" / "freifunk" / "state"
            webroot_dir = base / "run" / "freifunk" / "www"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            webroot_dir.mkdir(parents=True, exist_ok=True)
            runtime_target = runtime_dir / "node-metadata-status.json"
            runtime_target.write_text("{}\n", encoding="utf-8")
            (webroot_dir / "node-metadata-status.json").symlink_to(runtime_target)

            old = dict(os.environ)
            try:
                os.environ["METADATA_COLLECTOR_RUN_DIR"] = str(runtime_dir)
                os.environ["METADATA_COLLECTOR_WEBROOT_DIR"] = str(webroot_dir)
                os.environ["METADATA_COLLECTOR_STATUS_PATH"] = str(runtime_target)
                os.environ["METADATA_COLLECTOR_PUBLISHED_STATUS_PATH"] = str(webroot_dir / "node-metadata-status.json")
                config = MetadataCollectorConfig.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old)

            self.assertEqual(runtime_target, config.status_path)
            self.assertEqual(webroot_dir / "node-metadata-status.json", config.published_status_path)

    def test_write_outputs_recovers_from_broken_runtime_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            old = dict(os.environ)
            try:
                os.environ["METADATA_COLLECTOR_RUN_DIR"] = str(base / "run" / "freifunk" / "state")
                os.environ["METADATA_COLLECTOR_WEBROOT_DIR"] = str(base / "run" / "freifunk" / "www")
                config = MetadataCollectorConfig.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old)

            config.node_metadata_path.parent.mkdir(parents=True, exist_ok=True)
            config.node_metadata_path.symlink_to(config.node_metadata_path)

            app = MetadataCollectorApp(config)
            app.store = FakeStore(
                [
                    NodeState(
                        node_id="1001",
                        primary_ip="10.0.0.1",
                        first_seen_at="2025-01-01T00:00:00+00:00",
                        last_source_seen_at="2025-01-01T00:05:00+00:00",
                    )
                ]
            )

            import asyncio

            asyncio.run(app._write_outputs(reason="test"))

            self.assertTrue(config.node_metadata_path.is_file())
            self.assertFalse(config.node_metadata_path.is_symlink())
            self.assertTrue(config.published_node_metadata_path.is_symlink())
            self.assertEqual(config.node_metadata_path, config.published_node_metadata_path.resolve())


if __name__ == "__main__":
    unittest.main()
