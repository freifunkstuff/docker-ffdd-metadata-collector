from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(slots=True)
class ParseResult:
    node_id: str | None
    version: str | None
    timestamp: str | None
    node_type: str | None
    parser_name: str
    info: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)
    field_sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class DiscoveredNode:
    node_id: str
    primary_ip: str
    last_seen: str | None = None
    source: str | None = None


@dataclass(slots=True)
class FetchOutcome:
    node_id: str
    primary_ip: str
    fetched_at: str
    success: bool
    parse_result: ParseResult | None = None
    http_status: int | None = None
    error: str | None = None
    duration_ms: int | None = None
    result_kind: str = "unknown"
    timeout_ms: int | None = None


@dataclass(slots=True)
class NodeState:
    node_id: str
    primary_ip: str
    first_seen_at: str
    last_source_seen_at: str | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    consecutive_failures: int = 0
    fetch_error: str | None = None
    next_poll_at: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    version: str | None = None
    timestamp: str | None = None
    node_type: str | None = None
    parser_name: str | None = None
    parse_warnings: list[str] = field(default_factory=list)
    request_history: list[dict[str, Any]] = field(default_factory=list)

    def last_seen_for_snapshot(self) -> str | None:
        return _prefer_newer_timestamp(self.last_source_seen_at, self.last_success_at)

    def last_seen_at(self) -> datetime | None:
        last_seen = self.last_seen_for_snapshot()
        if last_seen is None:
            return None
        return _parse_timestamp(last_seen)

    def retention_reference_at(self) -> datetime | None:
        return self.last_seen_at() or _parse_timestamp(self.first_seen_at)

    def is_online(self, now: datetime, offline_after_seconds: float) -> bool | None:
        last_seen_at = self.last_seen_at()
        if last_seen_at is None:
            return None
        return (now - last_seen_at) < timedelta(seconds=offline_after_seconds)

    @property
    def community(self) -> str | None:
        value = self.info.get("community")
        return str(value) if value is not None else None

    def to_snapshot_node(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "primaryIpAddress": self.primary_ip,
            "firstSeen": self.first_seen_at,
            "lastSeen": self.last_seen_for_snapshot(),
            "lastFetched": self.last_fetch_at,
            "fetchError": self.fetch_error,
            "info": self.info,
            "stats": self.stats,
            "parser": {
                "version": self.version,
                "timestamp": self.timestamp,
                "nodeType": self.node_type,
                "parserName": self.parser_name,
                "warnings": list(self.parse_warnings),
            },
        }


def _prefer_newer_timestamp(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    left_dt = _parse_timestamp(left)
    right_dt = _parse_timestamp(right)
    if left_dt is not None and right_dt is not None:
        return left if left_dt >= right_dt else right
    if left_dt is not None:
        return left
    if right_dt is not None:
        return right
    return left


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
