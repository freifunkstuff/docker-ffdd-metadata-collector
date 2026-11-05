from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metadata_collector.app import MetadataCollectorApp
from metadata_collector.config import MetadataCollectorConfig
from metadata_collector.models import DiscoveredNode, FetchOutcome
from metadata_collector.node_list_sources import parse_node_list_payload
from metadata_collector.storage import YamlBackedMemoryStore
from metadata_collector.snapshot import write_published_json_atomic


class AppRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with patch.dict(os.environ, {
            "METADATA_COLLECTOR_DATA_DIR": str(root / "data"),
            "METADATA_COLLECTOR_RUN_DIR": str(root / "run"),
            "METADATA_COLLECTOR_WEBROOT_DIR": str(root / "www"),
            "METADATA_COLLECTOR_SOURCE_PATH": str(root / "nodes.json"),
        }, clear=True):
            self.config = replace(MetadataCollectorConfig.from_env(), snapshot_interval_seconds=.001)
        self.app = MetadataCollectorApp(self.config)
        self.config.ensure_directories()
        self.app.store.initialize()

    def test_legacy_nonfinite_values_publish_as_strict_json_without_mutation(self):
        document = {"stats": {"uptime": float("nan"), "nested": [float("inf"), float("-inf"), 3]}}
        write_published_json_atomic(self.config.node_metadata_path, self.config.published_node_metadata_path, document)
        text = self.config.published_node_metadata_path.read_text()
        def reject(value):
            raise AssertionError(f"non-standard JSON constant {value}")
        self.assertEqual(json.loads(text, parse_constant=reject), {"stats": {"uptime": None, "nested": [None, None, 3]}})
        self.assertIsInstance(document["stats"]["uptime"], float)

    def test_failed_write_cleans_temporary_file_and_preserves_previous_output(self):
        runtime, published = self.config.node_metadata_path, self.config.published_node_metadata_path
        write_published_json_atomic(runtime, published, {"old": True})
        before = set(runtime.parent.iterdir())
        with patch("metadata_collector.snapshot.json.dump", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                write_published_json_atomic(runtime, published, {"new": True})
        self.assertEqual(json.loads(published.read_text()), {"old": True})
        self.assertEqual(set(runtime.parent.iterdir()), before)

    async def test_snapshot_loop_recovers_from_transient_publication_error(self):
        recovered = asyncio.Event()
        calls = 0

        async def write(reason=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary disk failure")
            recovered.set()

        with patch.object(self.app, "_write_outputs", side_effect=write), self.assertLogs("metadata_collector.app", level="ERROR"):
            task = asyncio.create_task(self.app._snapshot_loop())
            try:
                await asyncio.wait_for(recovered.wait(), 1)
                self.assertFalse(task.done())
                self.assertGreaterEqual(calls, 2)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_snapshot_loop_recovers_from_builder_exception(self):
        with patch("metadata_collector.app.build_meshviewer_document", side_effect=ValueError("bad node")), self.assertLogs("metadata_collector.app", level="ERROR"):
            self.assertFalse(await self.app._try_write_outputs())
        self.assertTrue(await self.app._try_write_outputs())
        self.assertIsInstance(json.loads(self.config.published_meshviewer_path.read_text())["nodes"], list)

    async def test_startup_publication_error_does_not_prevent_worker_start(self):
        self.config.source_path.write_text('{"nodes":[{"id":"1001","ip":"10.200.0.1"}]}')
        reached_workers = asyncio.Event()

        async def worker():
            reached_workers.set()
            self.app.stop()
            await asyncio.Event().wait()

        with patch.object(self.app, "_write_outputs", side_effect=OSError("disk temporarily unavailable")), patch.object(self.app, "_poll_loop", side_effect=worker), self.assertLogs("metadata_collector.app", level="ERROR"):
            await asyncio.wait_for(self.app.run(), 1)
        self.assertTrue(reached_workers.is_set())

    async def test_supervisor_restarts_failed_loop(self):
        calls = 0
        recovered = asyncio.Event()

        async def loop():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TypeError("malformed discovery payload")
            recovered.set()
            await asyncio.Event().wait()

        with self.assertLogs("metadata_collector.app", level="ERROR"):
            task = asyncio.create_task(self.app._supervise("test", loop))
            try:
                await asyncio.wait_for(recovered.wait(), 2)
                self.assertEqual(calls, 2)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_unexpected_worker_cancellation_does_not_leave_process_waiting(self):
        self.config.source_path.write_text('{"nodes":[{"id":"1001","ip":"10.200.0.1"}]}')

        async def cancelled():
            raise asyncio.CancelledError()

        with patch.object(self.app, "_poll_loop", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.app.run(), 1)
        self.assertFalse(any(t.get_name() in {"discovery-loop", "snapshot-loop", "summary-loop", "stop-waiter"} for t in asyncio.all_tasks() if not t.done()))

    async def test_actual_poll_deadline_persists_and_bootstrap_respects_it(self):
        now = datetime.now(timezone.utc)
        self.app.store.merge_discovered_nodes([DiscoveredNode("1001", "10.200.0.1")], now.isoformat())
        outcome = FetchOutcome(node_id="1001", primary_ip="10.200.0.1", fetched_at=now.isoformat(), success=False, result_kind="timeout")
        with patch.object(self.app.fetcher.__class__, "fetch", new=AsyncMock(return_value=outcome)):
            await self.app._poll_node("1001", asyncio.Semaphore(1))
        deadline = self.app.store.get_node_state("1001").next_poll_at
        self.assertIsNotNone(deadline)
        reloaded = YamlBackedMemoryStore(self.config.discovery_state_path, self.config.node_info_dir, self.config.node_status_dir)
        reloaded.initialize()
        self.assertEqual(reloaded.get_node_state("1001").next_poll_at, deadline)
        self.app.store = reloaded
        self.app.scheduler = self.app.scheduler.__class__()
        self.app._bootstrap_scheduler()
        self.assertEqual(self.app.scheduler.pop_due(now, 64), [])

    async def test_exception_retry_deadline_is_persisted(self):
        now = datetime.now(timezone.utc)
        self.app.store.merge_discovered_nodes([DiscoveredNode("1001", "10.200.0.1")], now.isoformat())
        with patch.object(self.app.fetcher.__class__, "fetch", new=AsyncMock(side_effect=RuntimeError("unexpected"))), self.assertLogs("metadata_collector.app", level="ERROR"):
            await self.app._poll_node("1001", asyncio.Semaphore(1))
        self.assertIsNotNone(self.app.store.get_node_state("1001").next_poll_at)

    async def test_initial_unexpected_discovery_failure_still_starts_workers(self):
        reached = asyncio.Event()

        async def worker():
            reached.set()
            self.app.stop()
            await asyncio.Event().wait()

        with patch.object(self.app, "_run_discovery_once", side_effect=TypeError("bad source")), patch.object(self.app, "_poll_loop", side_effect=worker), self.assertLogs("metadata_collector.app", level="ERROR"):
            await asyncio.wait_for(self.app.run(), 1)
        self.assertTrue(reached.is_set())

    async def test_corrupt_persisted_deadline_is_scheduled_now_not_startup_fatal(self):
        now = datetime.now(timezone.utc)
        self.app.store.merge_discovered_nodes([DiscoveredNode("1001", "10.200.0.1")], now.isoformat())
        self.app.store.set_next_poll_at("1001", "invalid timestamp")
        self.assertEqual(self.app._bootstrap_scheduler(), 1)
        self.assertEqual(self.app.scheduler.pop_due(datetime.now(timezone.utc), 64), ["1001"])

    async def test_mixed_discovery_ids_have_stable_sort_order(self):
        nodes = parse_node_list_payload({"nodes": [
            {"id": "server", "ip": "10.200.0.1"},
            {"id": "10", "ip": "10.200.0.10"},
            {"id": "2", "ip": "10.200.0.2"},
        ]})
        self.assertEqual([n.node_id for n in nodes], ["2", "10", "server"])


if __name__ == "__main__":
    unittest.main()
