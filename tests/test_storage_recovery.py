from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_collector.models import DiscoveredNode, FetchOutcome, ParseResult
from metadata_collector.storage import YamlBackedMemoryStore


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.make_store()

    def make_store(self) -> YamlBackedMemoryStore:
        store = YamlBackedMemoryStore(self.root / "discovery.yaml", self.root / "info", self.root / "status")
        store.initialize()
        return store

    def write_yaml(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(value), encoding="utf-8")

    def outcome(self, success: bool = True) -> FetchOutcome:
        return FetchOutcome(
            node_id="1", primary_ip="10.0.0.1", fetched_at="2026-01-01T00:00:00+00:00",
            success=success,
            parse_result=ParseResult(None, None, None, None, "test", info={"hostname": "node"}) if success else None,
            error=None if success else "timeout",
        )

    def test_new_discovery_ip_survives_old_success_and_failure_files(self) -> None:
        self.store.apply_fetch_outcome(self.outcome())
        self.store.apply_fetch_outcome(self.outcome(False))
        self.store.merge_discovered_nodes(
            [DiscoveredNode("1", "10.0.0.2")], "2026-01-02T00:00:00+00:00"
        )
        recovered = self.make_store().get_node_state("1")
        self.assertEqual(recovered.primary_ip, "10.0.0.2")
        self.assertEqual(recovered.info, {"hostname": "node"})
        self.assertEqual(recovered.consecutive_failures, 1)
        self.assertEqual(recovered.last_source_seen_at, "2026-01-02T00:00:00+00:00")

    def test_in_flight_old_address_outcome_does_not_revert_discovery(self) -> None:
        for success in (True, False):
            with self.subTest(success=success):
                self.store.merge_discovered_nodes(
                    [DiscoveredNode("1", "10.0.0.1")], "2026-01-01T00:00:00+00:00"
                )
                in_flight_outcome = self.outcome(success)
                self.store.merge_discovered_nodes(
                    [DiscoveredNode("1", "10.0.0.2")], "2026-01-02T00:00:00+00:00"
                )
                self.store.apply_fetch_outcome(in_flight_outcome)
                state = self.store.get_node_state("1")
                self.assertEqual(state.primary_ip, "10.0.0.2")
                self.assertEqual(state.last_fetch_at, in_flight_outcome.fetched_at)
                self.assertEqual(state.request_history[-1]["success"], success)
                if success:
                    self.assertEqual(state.info, {"hostname": "node"})
                    info = yaml.safe_load((self.root / "info" / "1.yaml").read_text())
                    self.assertEqual(info["primary_ip"], "10.0.0.2")
                status = yaml.safe_load((self.root / "status" / "1.yaml").read_text())
                self.assertEqual(status["primary_ip"], "10.0.0.2")
                # An unrelated discovery write must not perpetuate the stale IP.
                self.store.merge_discovered_nodes([], "2026-01-03T00:00:00+00:00")
                self.assertEqual(self.make_store().get_node_state("1").primary_ip, "10.0.0.2")

    def test_poll_outcome_fills_missing_address(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.store = self.make_store()
                if existing:
                    self.store.merge_discovered_nodes(
                        [DiscoveredNode("1", "")], "2026-01-01T00:00:00+00:00"
                    )
                self.store.apply_fetch_outcome(self.outcome())
                self.assertEqual(self.store.get_node_state("1").primary_ip, "10.0.0.1")
                self.assertEqual(self.make_store().get_node_state("1").primary_ip, "10.0.0.1")

    def test_address_precedence_and_missing_discovery_fallback(self) -> None:
        for discovery_ip in ("10.0.0.3", "", None):
            for status_ip in ("10.0.0.2", "", None):
                with self.subTest(discovery_ip=discovery_ip, status_ip=status_ip):
                    discovery = {} if discovery_ip is None else {"primary_ip": discovery_ip}
                    status = {"node_id": "1"}
                    if status_ip is not None:
                        status["primary_ip"] = status_ip
                    self.write_yaml(self.root / "discovery.yaml", {"nodes": {"1": discovery}})
                    self.write_yaml(self.root / "info" / "1.yaml", {"node_id": "1", "primary_ip": "10.0.0.1"})
                    self.write_yaml(self.root / "status" / "1.yaml", status)
                    state = self.make_store().get_node_state("1")
                    self.assertEqual(state.primary_ip, discovery_ip or status_ip or "10.0.0.1")

    def test_status_address_used_without_discovery_file(self) -> None:
        self.write_yaml(self.root / "info" / "1.yaml", {"primary_ip": "10.0.0.1"})
        self.write_yaml(self.root / "status" / "1.yaml", {"primary_ip": "10.0.0.2"})
        self.assertEqual(self.make_store().get_node_state("1").primary_ip, "10.0.0.2")

    def test_deadline_persists_after_success_and_failure(self) -> None:
        for success in (True, False):
            with self.subTest(success=success):
                self.store.apply_fetch_outcome(self.outcome(success))
                deadline = "2026-01-02T03:04:05+00:00"
                self.store.set_next_poll_at("1", deadline)
                self.assertEqual(self.store.get_node_state("1").next_poll_at, deadline)
                persisted = yaml.safe_load((self.root / "status" / "1.yaml").read_text())
                self.assertEqual(persisted["next_poll_at"], deadline)
                recovered = self.make_store().get_node_state("1")
                self.assertEqual(recovered.next_poll_at, deadline)
                self.assertEqual(recovered.last_fetch_at, self.outcome().fetched_at)
                self.assertTrue(recovered.request_history)

    def test_deadline_is_explicit_and_can_be_cleared(self) -> None:
        self.store.merge_discovered_nodes([DiscoveredNode("1", "10.0.0.1")], "2026-01-01T00:00:00+00:00")
        self.store.get_node_state("1").next_poll_at = "not saved"
        self.assertIsNone(self.store.get_node_state("1").next_poll_at)
        self.store.set_next_poll_at("1", "2026-01-02T00:00:00+00:00")
        self.store.set_next_poll_at("1", None)
        self.assertIsNone(self.make_store().get_node_state("1").next_poll_at)
        with self.assertRaises(KeyError):
            self.store.set_next_poll_at("missing", None)
        self.assertIsNone(self.store.get_node_state("missing"))

    def test_legacy_status_without_deadline_loads_as_none(self) -> None:
        self.write_yaml(self.root / "status" / "1.yaml", {"primary_ip": "10.0.0.1"})
        self.assertIsNone(self.make_store().get_node_state("1").next_poll_at)


if __name__ == "__main__":
    unittest.main()
