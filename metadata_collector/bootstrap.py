from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import MetadataCollectorConfig
from .models import NodeState
from .storage import YamlBackedMemoryStore


logger = logging.getLogger(__name__)

DEFAULT_LEIPZIG_MESHVIEWER_URL = "https://karte.freifunk-leipzig.de/meshviewer/meshviewer.json"


class BootstrapError(RuntimeError):
    pass


def run_bootstrap(
    config: MetadataCollectorConfig,
    source: str,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> int:
    """Seed the persistent state from an existing meshviewer.json once.

    Reads a meshviewer document (HTTP(S) URL or local file), reverse-maps every
    node into a :class:`NodeState` and merges it into the store. Existing data
    from real polls is kept; ``first_seen_at`` is only ever lowered, never lost.
    """
    if config.storage_backend != "yaml-memory":
        raise BootstrapError(f"bootstrap only supports yaml-memory storage, got {config.storage_backend}")

    now = now or _utcnow()
    config.ensure_directories()
    store = YamlBackedMemoryStore(
        discovery_state_path=config.discovery_state_path,
        node_info_dir=config.node_info_dir,
        node_status_dir=config.node_status_dir,
    )
    store.initialize()

    document = load_meshviewer_document(source, user_agent=user_agent or config.request_user_agent)
    seeds = build_seed_states(document, now)
    if not seeds:
        raise BootstrapError(f"meshviewer document from {source} contained no usable nodes")
    seeded = store.seed_states(seeds, now.isoformat())
    logger.info("bootstrap seeded nodes=%s source=%s", seeded, source)
    return seeded


def load_meshviewer_document(source: str, user_agent: str = "metadata-collector/0.1") -> dict[str, Any]:
    if _looks_like_url(source):
        request = Request(source, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=30.0) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"failed to load meshviewer document from {source}: {exc}") from exc
    else:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except OSError as exc:
            raise BootstrapError(f"failed to read meshviewer document from {source}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BootstrapError(f"failed to parse meshviewer document from {source}: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise BootstrapError(f"meshviewer document from {source} has no nodes list")
    return payload


def build_seed_states(document: dict[str, Any], now: datetime) -> list[NodeState]:
    seeds: list[NodeState] = []
    for node in document.get("nodes", []):
        if not isinstance(node, dict):
            continue
        state = build_seed_state(node, now)
        if state is not None:
            seeds.append(state)
    return seeds


def build_seed_state(node: dict[str, Any], now: datetime) -> NodeState | None:
    node_id = _coerce_str(node.get("node_id"))
    if not node_id:
        return None
    primary_ip = _first_address(node.get("addresses"))

    first_seen_at = _normalize_iso(node.get("firstseen")) or now.isoformat()
    last_seen_at = _normalize_iso(node.get("lastseen"))

    is_gateway = bool(node.get("is_gateway"))
    info = _build_info(node, primary_ip, is_gateway)
    stats = _build_stats(node, now)

    return NodeState(
        node_id=node_id,
        primary_ip=primary_ip or "",
        first_seen_at=first_seen_at,
        # Both fields drive online/offline and snapshot lastSeen. We never
        # actually fetched, so this is intentionally lossy: lastseen stands in
        # for "successfully observed" until the first real poll refines it.
        last_source_seen_at=last_seen_at,
        last_success_at=last_seen_at,
        last_fetch_at=last_seen_at,
        info=info,
        stats=stats,
        node_type="server" if is_gateway else "node",
        parser_name="meshviewer-bootstrap",
    )


def _build_info(node: dict[str, Any], primary_ip: str | None, is_gateway: bool) -> dict[str, Any]:
    info: dict[str, Any] = {}
    _set_if(info, "community", _coerce_str(node.get("domain")))
    _set_if(info, "model", _coerce_str(node.get("model")))
    _set_if(info, "name", _hostname_to_name(node.get("hostname"), _coerce_str(node.get("node_id"))))
    _set_if(info, "contact_email", _coerce_str(node.get("contact")))
    _set_if(info, "group", _coerce_str(node.get("group")))
    _set_if(info, "node_type", "server" if is_gateway else "node")
    _set_if(info, "primary_ip", primary_ip)
    _set_if(info, "cpu_count", _coerce_int(node.get("nproc")))

    autoupdater = node.get("autoupdater")
    if isinstance(autoupdater, dict) and autoupdater.get("enabled") is not None:
        info["auto_update"] = bool(autoupdater.get("enabled"))

    firmware = node.get("firmware")
    if isinstance(firmware, dict):
        _set_if(info, "firmware_base", _coerce_str(firmware.get("base")))
        _set_if(info, "firmware_release", _coerce_str(firmware.get("release")))

    location = node.get("location")
    if isinstance(location, dict):
        _set_if(info, "location_latitude", _coerce_float(location.get("latitude")))
        _set_if(info, "location_longitude", _coerce_float(location.get("longitude")))
        _set_if(info, "location_altitude", _coerce_int(location.get("altitude")))

    return info


def _build_stats(node: dict[str, Any], now: datetime) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    _set_if(stats, "clients_2g", _coerce_int(node.get("clients_wifi24")))
    _set_if(stats, "clients_5g", _coerce_int(node.get("clients_wifi5")))
    _set_if(stats, "load_avg_5", _coerce_float(node.get("loadavg")))
    _set_if(stats, "selected_gateway", _coerce_str(node.get("gateway")))

    boot_at = _parse_iso(node.get("uptime"))
    if boot_at is not None:
        uptime_seconds = int((now - boot_at).total_seconds())
        if uptime_seconds > 0:
            stats["uptime_seconds"] = uptime_seconds

    return stats


def _hostname_to_name(hostname: Any, node_id: str | None) -> str | None:
    text = _coerce_str(hostname)
    if not text:
        return None
    if node_id:
        suffix = f" ({node_id})"
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = text.strip()
    if not text or text == node_id:
        return None
    return text


def _first_address(addresses: Any) -> str | None:
    if not isinstance(addresses, list):
        return None
    for address in addresses:
        text = _coerce_str(address)
        if text:
            return text
    return None


def _looks_like_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _normalize_iso(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.isoformat() if parsed is not None else None


def _parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _set_if(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
