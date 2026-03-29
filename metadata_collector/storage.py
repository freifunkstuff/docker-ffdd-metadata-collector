from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from .models import DiscoveredNode, FetchOutcome, NodeState


class StateStore(Protocol):
    def initialize(self) -> None:
        ...

    def merge_discovered_nodes(self, nodes: list[DiscoveredNode], discovered_at: str) -> None:
        ...

    def get_node_state(self, node_id: str) -> NodeState | None:
        ...

    def list_node_states(self) -> list[NodeState]:
        ...

    def apply_fetch_outcome(self, outcome: FetchOutcome) -> None:
        ...


class YamlBackedMemoryStore:
    def __init__(self, discovery_state_path: Path, node_info_dir: Path, node_status_dir: Path) -> None:
        self.discovery_state_path = Path(discovery_state_path)
        self.node_info_dir = Path(node_info_dir)
        self.node_status_dir = Path(node_status_dir)
        self._states: dict[str, NodeState] = {}

    def initialize(self) -> None:
        self.discovery_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.node_info_dir.mkdir(parents=True, exist_ok=True)
        self.node_status_dir.mkdir(parents=True, exist_ok=True)
        self._states = {}
        self._load_discovery_state()
        self._load_info_states()
        self._load_status_states()

    def merge_discovered_nodes(self, nodes: list[DiscoveredNode], discovered_at: str) -> None:
        for node in nodes:
            state = self._states.get(node.node_id)
            if state is None:
                state = NodeState(
                    node_id=node.node_id,
                    primary_ip=node.primary_ip,
                    first_seen_at=discovered_at,
                )
                self._states[node.node_id] = state
            state.primary_ip = node.primary_ip
            state.last_source_seen_at = node.last_seen or discovered_at
        self._write_discovery_state(generated_at=discovered_at)

    def get_node_state(self, node_id: str) -> NodeState | None:
        state = self._states.get(node_id)
        return deepcopy(state) if state is not None else None

    def list_node_states(self) -> list[NodeState]:
        return [deepcopy(self._states[node_id]) for node_id in sorted(self._states, key=_sort_key)]

    def apply_fetch_outcome(self, outcome: FetchOutcome) -> None:
        state = self._states.get(outcome.node_id)
        if state is None:
            state = NodeState(
                node_id=outcome.node_id,
                primary_ip=outcome.primary_ip,
                first_seen_at=outcome.fetched_at,
            )
            self._states[outcome.node_id] = state

        state.primary_ip = outcome.primary_ip
        state.last_fetch_at = outcome.fetched_at
        state.request_history = _append_request_attempt(state.request_history, outcome)

        if outcome.success and outcome.parse_result is not None:
            state.last_success_at = outcome.fetched_at
            state.last_failure_at = None
            state.consecutive_failures = 0
            state.fetch_error = None
            state.info = deepcopy(outcome.parse_result.info)
            state.stats = deepcopy(outcome.parse_result.stats)
            state.links = deepcopy(outcome.parse_result.links)
            state.version = outcome.parse_result.version
            state.timestamp = outcome.parse_result.timestamp
            state.node_type = outcome.parse_result.node_type
            state.parser_name = outcome.parse_result.parser_name
            state.parse_warnings = list(outcome.parse_result.parse_warnings)
            self._write_info_state(state)
            self._write_status_state(state)
            return

        state.last_failure_at = outcome.fetched_at
        state.consecutive_failures += 1
        state.fetch_error = outcome.error
        self._write_status_state(state)

    def purge_nodes_older_than(self, now: datetime, retention_seconds: float) -> int:
        cutoff = now - timedelta(seconds=retention_seconds)
        removable_node_ids = [
            node_id
            for node_id, state in self._states.items()
            if (state.retention_reference_at() is None or state.retention_reference_at() < cutoff)
        ]
        if not removable_node_ids:
            return 0

        for node_id in removable_node_ids:
            self._states.pop(node_id, None)
            _unlink_if_exists(self.node_info_dir / f"{node_id}.yaml")
            _unlink_if_exists(self.node_status_dir / f"{node_id}.yaml")

        self._write_discovery_state(generated_at=now.isoformat())
        return len(removable_node_ids)

    def _load_discovery_state(self) -> None:
        document = _read_yaml_file(self.discovery_state_path)
        if not isinstance(document, dict):
            return
        nodes = document.get("nodes")
        if not isinstance(nodes, dict):
            return
        for node_id, item in nodes.items():
            if not isinstance(item, dict):
                continue
            state = self._states.get(str(node_id))
            if state is None:
                state = NodeState(
                    node_id=str(node_id),
                    primary_ip=str(item.get("primary_ip") or ""),
                    first_seen_at=str(item.get("first_seen_at") or document.get("generated_at") or ""),
                )
                self._states[state.node_id] = state
            state.primary_ip = str(item.get("primary_ip") or state.primary_ip)
            state.first_seen_at = str(item.get("first_seen_at") or state.first_seen_at)
            state.last_source_seen_at = _as_optional_str(item.get("last_source_seen_at")) or state.last_source_seen_at

    def _load_info_states(self) -> None:
        for path in sorted(self.node_info_dir.glob("*.yaml")):
            document = _read_yaml_file(path)
            if not isinstance(document, dict):
                continue
            node_id = _as_optional_str(document.get("node_id")) or path.stem
            state = self._states.get(node_id)
            if state is None:
                state = NodeState(
                    node_id=node_id,
                    primary_ip=_as_optional_str(document.get("primary_ip")) or "",
                    first_seen_at=_as_optional_str(document.get("first_seen_at")) or "",
                )
                self._states[node_id] = state
            state.primary_ip = _as_optional_str(document.get("primary_ip")) or state.primary_ip
            state.first_seen_at = _as_optional_str(document.get("first_seen_at")) or state.first_seen_at
            info_last_source_seen_at = _as_optional_str(document.get("last_source_seen_at"))
            if state.last_source_seen_at is None:
                state.last_source_seen_at = info_last_source_seen_at
            state.last_success_at = _as_optional_str(document.get("last_info_success_at")) or state.last_success_at
            state.version = _as_optional_str(document.get("version")) or state.version
            state.node_type = _as_optional_str(document.get("node_type")) or state.node_type
            state.parser_name = _as_optional_str(document.get("parser_name")) or state.parser_name
            state.parse_warnings = list(document.get("parse_warnings") or state.parse_warnings)
            state.info = dict(document.get("info") or state.info)

    def _load_status_states(self) -> None:
        for path in sorted(self.node_status_dir.glob("*.yaml")):
            document = _read_yaml_file(path)
            if not isinstance(document, dict):
                continue
            node_id = _as_optional_str(document.get("node_id")) or path.stem
            state = self._states.get(node_id)
            if state is None:
                state = NodeState(
                    node_id=node_id,
                    primary_ip=_as_optional_str(document.get("primary_ip")) or "",
                    first_seen_at=_as_optional_str(document.get("first_seen_at")) or "",
                )
                self._states[node_id] = state
            state.primary_ip = _as_optional_str(document.get("primary_ip")) or state.primary_ip
            state.first_seen_at = _as_optional_str(document.get("first_seen_at")) or state.first_seen_at
            state.last_fetch_at = _as_optional_str(document.get("last_fetch_at")) or state.last_fetch_at
            state.last_success_at = _as_optional_str(document.get("last_success_at")) or state.last_success_at
            state.last_failure_at = _as_optional_str(document.get("last_failure_at")) or state.last_failure_at
            state.consecutive_failures = int(document.get("consecutive_failures") or state.consecutive_failures)
            state.fetch_error = _as_optional_str(document.get("fetch_error")) or state.fetch_error
            state.timestamp = _as_optional_str(document.get("timestamp")) or state.timestamp
            state.stats = dict(document.get("stats") or state.stats)
            state.links = list(document.get("links") or state.links)
            state.request_history = _normalize_request_history(document.get("request_history")) or state.request_history

    def _write_discovery_state(self, generated_at: str) -> None:
        payload = {
            "generated_at": generated_at,
            "nodes": {
                node_id: {
                    "primary_ip": state.primary_ip,
                    "first_seen_at": state.first_seen_at,
                    "last_source_seen_at": state.last_source_seen_at,
                }
                for node_id, state in sorted(self._states.items(), key=lambda item: _sort_key(item[0]))
            },
        }
        _write_yaml_atomic(self.discovery_state_path, payload)

    def _write_info_state(self, state: NodeState) -> None:
        payload = {
            "node_id": state.node_id,
            "primary_ip": state.primary_ip,
            "first_seen_at": state.first_seen_at,
            "last_source_seen_at": state.last_source_seen_at,
            "last_info_success_at": state.last_success_at,
            "version": state.version,
            "node_type": state.node_type,
            "parser_name": state.parser_name,
            "parse_warnings": list(state.parse_warnings),
            "info": state.info,
        }
        _write_yaml_atomic(self.node_info_dir / f"{state.node_id}.yaml", payload)

    def _write_status_state(self, state: NodeState) -> None:
        payload = {
            "node_id": state.node_id,
            "primary_ip": state.primary_ip,
            "first_seen_at": state.first_seen_at,
            "last_fetch_at": state.last_fetch_at,
            "last_success_at": state.last_success_at,
            "last_failure_at": state.last_failure_at,
            "consecutive_failures": state.consecutive_failures,
            "fetch_error": state.fetch_error,
            "timestamp": state.timestamp,
            "request_history": state.request_history,
            "stats": state.stats,
            "links": state.links,
        }
        _write_yaml_atomic(self.node_status_dir / f"{state.node_id}.yaml", payload)


def _read_yaml_file(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        yaml.safe_dump(document, handle, sort_keys=True, allow_unicode=False)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _as_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
def _sort_key(node_id: str) -> tuple[int, str]:
    if node_id.isdigit():
        return (0, f"{int(node_id):020d}")
    return (1, node_id)


def _append_request_attempt(history: list[dict[str, Any]], outcome: FetchOutcome) -> list[dict[str, Any]]:
    updated = list(history)
    result = outcome.result_kind
    if result == "unknown":
        result = _derive_result_kind(outcome)
    updated.append(
        {
            "at": outcome.fetched_at,
            "duration_ms": outcome.duration_ms,
            "timeout_ms": outcome.timeout_ms,
            "success": outcome.success,
            "result": result,
            "http_status": outcome.http_status,
            "error": outcome.error,
        }
    )
    return updated[-10:]


def _normalize_request_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "at": _as_optional_str(item.get("at")),
                "duration_ms": item.get("duration_ms"),
                "timeout_ms": item.get("timeout_ms"),
                "success": _as_optional_bool(item.get("success")),
                "result": _as_optional_str(item.get("result")) or "unknown",
                "http_status": item.get("http_status"),
                "error": _as_optional_str(item.get("error")),
            }
        )
    return normalized[-10:]


def _derive_result_kind(outcome: FetchOutcome) -> str:
    if outcome.success:
        return "success"
    error = (outcome.error or "").lower()
    if "timed out" in error:
        return "timeout"
    if "refused" in error:
        return "connection_refused"
    if "no route to host" in error:
        return "no_route"
    if outcome.http_status is not None:
        return "http_error"
    return "error"
