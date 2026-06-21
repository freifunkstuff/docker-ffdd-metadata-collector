from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote_plus

from .models import NodeState


def build_meshviewer_document(
    generated_at: str,
    states: list[NodeState],
    online_window_seconds: float,
    hide_temp_after_seconds: float,
    hide_stale_after_days: float,
) -> dict[str, Any]:
    now = _parse_timestamp(generated_at)
    visible_nodes: list[dict[str, Any]] = []
    visible_node_ids: set[str] = set()

    for state in sorted(states, key=lambda item: item.node_id):
        node = _build_meshviewer_node(
            state=state,
            now=now,
            online_window_seconds=online_window_seconds,
            hide_temp_after_seconds=hide_temp_after_seconds,
            hide_stale_after_days=hide_stale_after_days,
        )
        if node is None:
            continue
        visible_nodes.append(node)
        visible_node_ids.add(state.node_id)

    links: list[dict[str, Any]] = []
    for link in _merge_links(states):
        item = _build_meshviewer_link(link, now, online_window_seconds)
        if item is None:
            continue
        if item["source"] not in visible_node_ids or item["target"] not in visible_node_ids:
            continue
        links.append(item)

    return {
        "timestamp": generated_at,
        "nodes": visible_nodes,
        "links": links,
    }


def build_community_meshviewer_documents(
    generated_at: str,
    states: list[NodeState],
    online_window_seconds: float,
    hide_temp_after_seconds: float,
    hide_stale_after_days: float,
) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for community_slug in _collect_community_slugs(states):
        community_states = [state for state in states if _is_in_community_document(state, community_slug)]
        documents[community_slug] = build_meshviewer_document(
            generated_at=generated_at,
            states=community_states,
            online_window_seconds=online_window_seconds,
            hide_temp_after_seconds=hide_temp_after_seconds,
            hide_stale_after_days=hide_stale_after_days,
        )
    return documents


def _build_meshviewer_node(
    state: NodeState,
    now: datetime,
    online_window_seconds: float,
    hide_temp_after_seconds: float,
    hide_stale_after_days: float,
) -> dict[str, Any] | None:
    if not state.node_id or not state.info:
        return None
    if _is_hidden(state, now, hide_temp_after_seconds, hide_stale_after_days):
        return None

    info = state.info
    stats = state.stats
    is_gateway = info.get("node_type") == "server"
    online = state.is_online(now, online_window_seconds) is True
    node: dict[str, Any] = {
        "node_id": state.node_id,
        "hostname": _build_hostname(info.get("name"), state.node_id),
        "addresses": [state.primary_ip],
        "mac": _generate_mac(state.node_id),
        "firstseen": state.first_seen_at,
        "lastseen": state.last_seen_for_snapshot(),
        "is_online": online,
        "is_gateway": is_gateway,
    }

    _set_if(node, "contact", info.get("contact_email"))
    _set_if(node, "group", info.get("group"))
    _set_if(node, "model", info.get("model"))
    _set_if(node, "domain", info.get("community"))
    _set_if(node, "nproc", info.get("cpu_count"))

    location = _build_location(info, is_gateway)
    if location is not None:
        node["location"] = location

    firmware = _build_firmware(info)
    if firmware is not None:
        node["firmware"] = firmware

    autoupdater = _build_autoupdater(info.get("auto_update"))
    if autoupdater is not None:
        node["autoupdater"] = autoupdater

    if online and stats:
        gateway = _build_gateway(stats)
        if gateway is not None:
            node["gateway"] = gateway

        clients_2g = _coerce_int(stats.get("clients_2g")) or 0
        clients_5g = _coerce_int(stats.get("clients_5g")) or 0
        node["clients_wifi24"] = clients_2g
        node["clients_wifi5"] = clients_5g
        node["clients"] = clients_2g + clients_5g

        uptime = _build_uptime(now, stats.get("uptime_seconds"))
        if uptime is not None:
            node["uptime"] = uptime

        _set_if(node, "loadavg", _coerce_float(stats.get("load_avg_5")))

        memory_usage = _build_memory_usage(stats)
        if memory_usage is not None:
            node["memory_usage"] = memory_usage

    return node


def _build_meshviewer_link(link: dict[str, object], now: datetime, online_window_seconds: float) -> dict[str, Any] | None:
    left_ts = _parse_optional_timestamp(link.get("left_ts"))
    right_ts = _parse_optional_timestamp(link.get("right_ts"))
    left_valid = _timestamp_is_recent(left_ts, now, online_window_seconds)
    right_valid = _timestamp_is_recent(right_ts, now, online_window_seconds)
    if not left_valid and not right_valid:
        return None

    source_tq = None
    if left_valid:
        source_tq = _normalize_tq(link.get("left_tq"))
    elif right_valid:
        source_tq = _normalize_tq(link.get("right_rq"))

    target_tq = None
    if right_valid:
        target_tq = _normalize_tq(link.get("right_tq"))
    elif left_valid:
        target_tq = _normalize_tq(link.get("left_rq"))

    item = {
        "source": str(link.get("left_node_id") or ""),
        "source_address": _generate_mac(str(link.get("left_node_id") or "")),
        "target": str(link.get("right_node_id") or ""),
        "target_address": _generate_mac(str(link.get("right_node_id") or "")),
        "type": _map_link_type(str(link.get("type") or "")),
    }
    _set_if(item, "source_tq", source_tq)
    _set_if(item, "target_tq", target_tq)
    return item if item["source"] and item["target"] else None


def _is_hidden(state: NodeState, now: datetime, hide_temp_after_seconds: float, hide_stale_after_days: float) -> bool:
    last_seen = state.last_seen_at()
    if last_seen is None:
        return True
    age_seconds = (now - last_seen).total_seconds()
    if len(state.node_id) == 3 and state.node_id.startswith("9"):
        return age_seconds > hide_temp_after_seconds
    return age_seconds > hide_stale_after_days * 24.0 * 3600.0


def _build_hostname(name: object, node_id: str) -> str:
    text = _decode_urlencoded_text(name)
    text = text.strip() if text is not None else ""
    return node_id if not text else f"{text} ({node_id})"


def _build_location(info: dict[str, Any], is_gateway: bool) -> dict[str, Any] | None:
    if is_gateway:
        return None
    latitude = _coerce_float(info.get("location_latitude"))
    longitude = _coerce_float(info.get("location_longitude"))
    altitude = _coerce_int(info.get("location_altitude"))
    if latitude is None or longitude is None:
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    location = {
        "latitude": latitude,
        "longitude": longitude,
    }
    _set_if(location, "altitude", altitude)
    return location


def _build_firmware(info: dict[str, Any]) -> dict[str, Any] | None:
    base = info.get("firmware_base")
    release = info.get("firmware_release")
    if base in (None, "") and release in (None, ""):
        return None
    firmware: dict[str, Any] = {}
    _set_if(firmware, "base", base)
    _set_if(firmware, "release", release)
    return firmware


def _build_autoupdater(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    enabled = bool(value)
    autoupdater = {"enabled": enabled}
    if enabled:
        autoupdater["branch"] = "stable"
    return autoupdater


def _build_gateway(stats: dict[str, Any]) -> str | None:
    selected = stats.get("selected_gateway")
    preferred = stats.get("preferred_gateway")
    if selected in (None, ""):
        return None
    gateway = str(selected)
    if preferred not in (None, "") and preferred != selected:
        gateway = f"{gateway} ({preferred})"
    return gateway


def _build_uptime(now: datetime, uptime_seconds: object) -> str | None:
    seconds = _coerce_float(uptime_seconds)
    if seconds is None:
        return None
    return (now - timedelta(seconds=seconds)).isoformat()


def _build_memory_usage(stats: dict[str, Any]) -> float | None:
    mem_total = _coerce_float(stats.get("mem_total"))
    mem_free = _coerce_float(stats.get("mem_free"))
    if mem_total in (None, 0.0) or mem_free is None:
        return None
    usage = 1.0 - (mem_free / mem_total)
    return round(usage, 6)


def _normalize_tq(value: object) -> float | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return round(number / 100.0, 6)


def _map_link_type(value: str) -> str:
    if value == "lan":
        return "lan"
    if value in {"wifi", "wifi_mesh", "wifi_adhoc"}:
        return "wifi"
    if value == "backbone":
        return "vpn"
    return "other"


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


def _generate_mac(node_id: str) -> str | None:
    try:
        value = int(node_id)
    except (TypeError, ValueError):
        return None
    text = f"ffdd00{value & 0xFFFFF:06x}"
    return ":".join(text[index:index + 2] for index in range(0, len(text), 2))


def _timestamp_is_recent(value: datetime | None, now: datetime, maximum_age_seconds: float) -> bool:
    if value is None:
        return False
    return (now - value).total_seconds() < maximum_age_seconds


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _parse_timestamp(str(value))
    except ValueError:
        return None


def _coerce_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _set_if(target: dict[str, Any], key: str, value: object) -> None:
    if value is not None:
        target[key] = value


def _decode_urlencoded_text(value: object) -> str | None:
    if value in (None, ""):
        return None if value is None else ""
    return unquote_plus(str(value))


def _collect_community_slugs(states: list[NodeState]) -> list[str]:
    slugs: set[str] = set()
    for state in states:
        community_slug = _slugify_community(state.community)
        if community_slug is None:
            continue
        slugs.add(community_slug)
    return sorted(slugs)


def _is_in_community_document(state: NodeState, community_slug: str) -> bool:
    state_community_slug = _slugify_community(state.community)
    if state_community_slug == community_slug:
        return True
    return str(state.info.get("node_type") or "") == "server"


def _slugify_community(value: object) -> str | None:
    if value in (None, ""):
        return None
    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.strip().lower()
    if not lowered:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or None