from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import MetadataCollectorConfig
from .models import FetchOutcome, NodeState


@dataclass(order=True, slots=True)
class QueueEntry:
    due_at: float
    generation: int
    node_id: str = field(compare=False)


class PollScheduler:
    def __init__(self) -> None:
        self._heap: list[QueueEntry] = []
        self._generations: dict[str, int] = {}

    def schedule(self, node_id: str, due_at: datetime) -> None:
        generation = self._generations.get(node_id, 0) + 1
        self._generations[node_id] = generation
        heapq.heappush(self._heap, QueueEntry(due_at=due_at.timestamp(), generation=generation, node_id=node_id))

    def pop_due(self, now: datetime, limit: int) -> list[str]:
        due_node_ids: list[str] = []
        threshold = now.timestamp()
        while self._heap and len(due_node_ids) < limit:
            entry = self._heap[0]
            if entry.due_at > threshold:
                break
            heapq.heappop(self._heap)
            if self._generations.get(entry.node_id) != entry.generation:
                continue
            due_node_ids.append(entry.node_id)
        return due_node_ids

    def seconds_until_next_due(self, now: datetime) -> float:
        while self._heap:
            entry = self._heap[0]
            if self._generations.get(entry.node_id) != entry.generation:
                heapq.heappop(self._heap)
                continue
            return max(0.0, entry.due_at - now.timestamp())
        return 1.0


def classify_poll_mode(config: MetadataCollectorConfig, state: NodeState, now: datetime) -> str:
    if state.last_source_seen_at:
        try:
            source_seen_at = _parse_datetime(state.last_source_seen_at)
        except ValueError:
            source_seen_at = now
    else:
        source_seen_at = now

    return _classify_poll_mode(
        recent_attempts=state.request_history[-10:],
        source_is_stale=(now - source_seen_at) > timedelta(seconds=config.source_stale_after_seconds),
        default_timeout_ms=int(config.fetch_timeout_normal_seconds * 1000),
        consecutive_failures=state.consecutive_failures,
        failures_before_very_slow=config.max_consecutive_failures_before_very_slow,
    )


def fetch_timeout_for_mode(config: MetadataCollectorConfig, mode: str) -> float:
    if mode == "very_slow":
        return config.fetch_timeout_very_slow_seconds
    if mode == "slow":
        return config.fetch_timeout_slow_seconds
    return config.fetch_timeout_normal_seconds


def compute_next_poll_at(config: MetadataCollectorConfig, state: NodeState, outcome: FetchOutcome | None, now: datetime) -> datetime:
    if outcome is None:
        return now

    mode = classify_poll_mode(config, state, now)

    if mode == "normal":
        return now + timedelta(seconds=config.poll_interval_normal_seconds)

    if mode == "very_slow":
        return now + timedelta(seconds=config.poll_interval_very_slow_seconds)

    return now + timedelta(seconds=config.poll_interval_slow_seconds)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _classify_poll_mode(
    recent_attempts: list[dict[str, object]],
    source_is_stale: bool,
    default_timeout_ms: int,
    consecutive_failures: int,
    failures_before_very_slow: int,
) -> str:
    if not recent_attempts:
        return "normal"

    recent5 = recent_attempts[-5:]
    recent3 = recent_attempts[-3:]
    successes5 = sum(1 for item in recent5 if item.get("success") is True)
    failures5 = sum(1 for item in recent5 if item.get("success") is False)
    consecutive_failure_tail = _count_trailing_failures(recent_attempts)
    timeoutish5 = sum(1 for item in recent5 if _is_timeoutish(item, default_timeout_ms))
    slow_successes5 = sum(1 for item in recent5 if _is_slow_success(item, default_timeout_ms))
    stable_successes3 = len(recent3) == 3 and all(_is_stable_success(item, default_timeout_ms) for item in recent3)

    if source_is_stale:
        if successes5 == 0 or consecutive_failure_tail >= 2:
            return "very_slow"
        return "slow"

    if consecutive_failures >= failures_before_very_slow or consecutive_failure_tail >= 3:
        return "very_slow"

    if timeoutish5 >= 2:
        return "very_slow"

    if stable_successes3 and failures5 == 0 and timeoutish5 == 0 and slow_successes5 == 0:
        return "normal"

    if failures5 > 0 or timeoutish5 > 0 or slow_successes5 > 0:
        return "slow"

    if successes5 >= 1:
        return "slow"

    return "normal"


def _count_trailing_failures(recent_attempts: list[dict[str, object]]) -> int:
    count = 0
    for item in reversed(recent_attempts):
        if item.get("success") is False:
            count += 1
            continue
        break
    return count


def _is_timeoutish(item: dict[str, object], default_timeout_ms: int) -> bool:
    result = str(item.get("result") or "")
    duration_ms = item.get("duration_ms")
    timeout_ms = item.get("timeout_ms") if isinstance(item.get("timeout_ms"), int) else default_timeout_ms
    if result == "timeout":
        return True
    if isinstance(duration_ms, int) and duration_ms >= int(timeout_ms * 0.8):
        return True
    return False


def _is_slow_success(item: dict[str, object], default_timeout_ms: int) -> bool:
    duration_ms = item.get("duration_ms")
    timeout_ms = item.get("timeout_ms") if isinstance(item.get("timeout_ms"), int) else default_timeout_ms
    return item.get("success") is True and isinstance(duration_ms, int) and duration_ms >= int(timeout_ms * 0.6)


def _is_stable_success(item: dict[str, object], default_timeout_ms: int) -> bool:
    duration_ms = item.get("duration_ms")
    timeout_ms = item.get("timeout_ms") if isinstance(item.get("timeout_ms"), int) else default_timeout_ms
    return item.get("success") is True and isinstance(duration_ms, int) and duration_ms < int(timeout_ms * 0.6)
