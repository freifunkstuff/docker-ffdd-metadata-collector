from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import DiscoveredNode


class NodeListSourceError(RuntimeError):
    pass


class NodeListSource(Protocol):
    async def fetch_nodes(self) -> list[DiscoveredNode]:
        ...


@dataclass(slots=True, frozen=True)
class HttpJsonNodeListSource:
    url: str
    timeout_seconds: float = 5.0
    user_agent: str = "metadata-collector/0.1"

    async def fetch_nodes(self) -> list[DiscoveredNode]:
        return await asyncio.to_thread(self._fetch_nodes_sync)

    def _fetch_nodes_sync(self) -> list[DiscoveredNode]:
        request = Request(self.url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NodeListSourceError(f"failed to load node list from {self.url}: {exc}") from exc

        nodes = parse_node_list_payload(payload)
        if not nodes:
            raise NodeListSourceError(f"node list from {self.url} was empty or unsupported")
        return nodes


@dataclass(slots=True, frozen=True)
class FileJsonNodeListSource:
    path: Path

    async def fetch_nodes(self) -> list[DiscoveredNode]:
        return await asyncio.to_thread(self._fetch_nodes_sync)

    def _fetch_nodes_sync(self) -> list[DiscoveredNode]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError as exc:
            raise NodeListSourceError(f"failed to load node list from {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NodeListSourceError(f"failed to parse node list from {self.path}: {exc}") from exc

        nodes = parse_node_list_payload(payload)
        if not nodes:
            raise NodeListSourceError(f"node list from {self.path} was empty or unsupported")
        return nodes


@dataclass(slots=True, frozen=True)
class BmxdNodeListSource:
    command: str = "bmxd"

    async def fetch_nodes(self) -> list[DiscoveredNode]:
        raise NotImplementedError("BmxdNodeListSource is not implemented yet")


def parse_node_list_payload(payload: Any) -> list[DiscoveredNode]:
    nodes_by_id: dict[str, DiscoveredNode] = {}

    def add_node(node_id: Any, primary_ip: Any, last_seen: Any = None, source: str | None = None) -> None:
        node_id_text = _coerce_text(node_id)
        primary_ip_text = _coerce_text(primary_ip)
        if not node_id_text or not primary_ip_text:
            return
        discovered = DiscoveredNode(
            node_id=node_id_text,
            primary_ip=primary_ip_text,
            last_seen=_coerce_text(last_seen),
            source=source,
        )
        existing = nodes_by_id.get(node_id_text)
        if existing is None or (existing.last_seen is None and discovered.last_seen is not None):
            nodes_by_id[node_id_text] = discovered

    if isinstance(payload, list):
        _extract_from_iterable(payload, add_node, "list")
    elif isinstance(payload, dict):
        if isinstance(payload.get("nodes"), list):
            _extract_from_iterable(payload["nodes"], add_node, "nodes[]")
        elif isinstance(payload.get("nodes"), dict):
            _extract_from_mapping(payload["nodes"], add_node, "nodes{}")

        bmxd = payload.get("bmxd")
        if isinstance(bmxd, dict):
            _extract_from_iterable(bmxd.get("originators", []), add_node, "bmxd.originators")

    return sorted(nodes_by_id.values(), key=lambda item: int(item.node_id) if item.node_id.isdigit() else item.node_id)


def _extract_from_iterable(items: Iterable[Any], add_node: Any, source: str) -> None:
    for entry in items:
        if not isinstance(entry, dict):
            continue
        add_node(
            entry.get("id") or entry.get("node") or entry.get("node_id"),
            entry.get("primary_ip") or entry.get("ip") or entry.get("address"),
            entry.get("last_seen") or entry.get("lastseen") or entry.get("timestamp"),
            source,
        )


def _extract_from_mapping(items: dict[str, Any], add_node: Any, source: str) -> None:
    for key, value in items.items():
        if isinstance(value, dict):
            add_node(
                value.get("id") or value.get("node") or key,
                value.get("primary_ip") or value.get("ip") or value.get("address"),
                value.get("last_seen") or value.get("lastseen") or value.get("timestamp"),
                source,
            )


def _coerce_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
