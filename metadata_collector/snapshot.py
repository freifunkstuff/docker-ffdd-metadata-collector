from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import NodeState


def build_snapshot_document(generated_at: str, states: list[NodeState]) -> dict[str, Any]:
    return {
        "generatedAt": generated_at,
        "nodes": [state.to_snapshot_node() for state in states],
        "links": _merge_links(states),
    }


def build_status_document(
    generated_at: str,
    states: list[NodeState],
    online_window_seconds: float,
    fetch_window_seconds: float,
    source_type: str,
    source: str,
) -> dict[str, Any]:
    generated_at_dt = _parse_iso_timestamp(generated_at)
    online = 0
    stale = 0
    unknown = 0
    nodes_with_info = 0
    nodes_with_fetch_error = 0
    for state in states:
        is_online = state.is_online(generated_at_dt, online_window_seconds)
        if is_online is True:
            online += 1
        elif is_online is False:
            stale += 1
        else:
            unknown += 1
        if state.info:
            nodes_with_info += 1
        if state.fetch_error:
            nodes_with_fetch_error += 1

    fetch_summary = build_fetch_summary(generated_at_dt, states, fetch_window_seconds)

    return {
        "generatedAt": generated_at,
        "collector": {
            "sourceType": source_type,
            "source": source,
            "onlineWindowSeconds": online_window_seconds,
        },
        "nodes": {
            "total": len(states),
            "online": online,
            "stale": stale,
            "unknown": unknown,
            "withInfo": nodes_with_info,
            "withFetchError": nodes_with_fetch_error,
        },
        "fetch": fetch_summary,
    }


def build_fetch_summary(now: datetime, states: list[NodeState], window_seconds: float) -> dict[str, Any]:
    window_start = now - timedelta(seconds=window_seconds)
    fetches = 0
    for state in states:
        for attempt in state.request_history:
            attempted_at = _parse_optional_timestamp(attempt.get("at"))
            if attempted_at is None:
                continue
            if attempted_at >= window_start:
                fetches += 1
    rate_per_minute = 0.0
    if window_seconds > 0:
        rate_per_minute = fetches * 60.0 / window_seconds
    return {
        "windowSeconds": window_seconds,
        "fetches": fetches,
        "ratePerMinute": round(rate_per_minute, 3),
    }


def write_published_json_atomic(runtime_path: Path, published_path: Path, document: dict[str, Any]) -> None:
    runtime_path = Path(runtime_path)
    published_path = Path(published_path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=runtime_path.parent, delete=False) as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o644)
    if runtime_path.is_symlink():
        runtime_path.unlink()
    temp_path.replace(runtime_path)
    _replace_symlink_atomic(published_path, runtime_path)


def _merge_links(states: list[NodeState]) -> list[dict[str, object]]:
    merged: dict[tuple[str, str, str], dict[str, object]] = {}
    for state in states:
        for link in state.links:
            key = (
                str(link.get("type") or ""),
                str(link.get("left_node_id") or ""),
                str(link.get("right_node_id") or ""),
            )
            if not all(key):
                continue
            current = merged.get(key, {}).copy()
            current.update({name: value for name, value in link.items() if value is not None})
            merged[key] = current
    return [merged[key] for key in sorted(merged)]


def _replace_symlink_atomic(link_path: Path, target_path: Path) -> None:
    temporary_link_path = link_path.parent / f".{link_path.name}.{uuid4().hex}.tmp"
    os.symlink(target_path, temporary_link_path)
    Path(temporary_link_path).replace(link_path)


def _parse_iso_timestamp(value: str) -> Any:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _parse_iso_timestamp(str(value))
    except ValueError:
        return None
