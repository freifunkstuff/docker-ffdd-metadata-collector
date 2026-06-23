from __future__ import annotations

import asyncio
import base64
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import NodeState
from .snapshot import _merge_links


logger = logging.getLogger(__name__)


# Common node label keys, in stable order. Prometheus-compatible (underscores),
# i.e. the old dotted names minus the dot-bug.
_NODE_LABEL_KEYS = (
    "nodeid",
    "hostname",
    "group",
    "model",
    "domain",
    "owner",
    "autoupdater",
    "firmware_base",
    "firmware_release",
)

_AIRTIME_SUFFIXES = ("busy", "active", "rx", "tx")


@dataclass(slots=True, frozen=True)
class VictoriametricsExporter:
    import_url: str
    username: str | None = None
    password: str | None = None
    user_agent: str = "metadata-collector/0.1"
    node_max_age_seconds: float = 600.0
    link_max_age_seconds: float = 900.0
    communities: frozenset[str] = frozenset()

    async def export(self, states: list[NodeState], now: datetime | None = None) -> bool:
        payload = self.build_payload(states, now or _utcnow())
        if not payload:
            return False
        return await asyncio.to_thread(self._post, payload)

    def build_payload(self, states: list[NodeState], now: datetime) -> str:
        node_by_id = {state.node_id: state for state in states}
        lines: list[str] = []
        for state in states:
            if not self._node_is_pushable(state, now):
                continue
            if not self._community_ok(state):
                continue
            lines.extend(self._node_lines(state))
        lines.extend(self._link_lines(states, node_by_id, now))
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def _post(self, payload: str) -> bool:
        request = Request(
            self.import_url,
            data=payload.encode("utf-8"),
            method="POST",
            headers={"User-Agent": self.user_agent, "Content-Type": "text/plain"},
        )
        if self.username is not None:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urlopen(request, timeout=30.0) as response:
                response.read()
            return True
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("victoriametrics push failed url=%s error=%s", self.import_url, exc)
            return False

    def _node_is_pushable(self, state: NodeState, now: datetime) -> bool:
        if not state.info or not state.stats:
            return False
        last_success = _parse_timestamp(state.last_success_at)
        if last_success is None:
            return False
        return (now - last_success).total_seconds() < self.node_max_age_seconds

    def _community_ok(self, state: NodeState) -> bool:
        if not self.communities:
            return True
        # Servers/gateways always pass so their gateway links stay visible,
        # matching the old CommunityFilter behaviour.
        if _is_server(state):
            return True
        community = state.community
        return community is not None and community.lower() in self.communities

    def _node_lines(self, state: NodeState) -> list[str]:
        info = state.info
        stats = state.stats
        labels = {
            "nodeid": state.node_id,
            "hostname": info.get("name"),
            "group": info.get("group"),
            "model": info.get("model"),
            "domain": info.get("community"),
            "owner": info.get("contact_email"),
            "autoupdater": _autoupdater_label(info.get("auto_update")),
            "firmware_base": info.get("firmware_base"),
            "firmware_release": info.get("firmware_release"),
        }
        label_str = _format_labels(labels, _NODE_LABEL_KEYS)

        metrics: list[tuple[str, Any]] = [
            ("node_info", 1),
            ("node_time_up", stats.get("uptime_seconds")),
            ("node_traffic_rx_bytes", stats.get("traffic_wifi_rx")),
            ("node_traffic_tx_bytes", stats.get("traffic_wifi_tx")),
            ("node_traffic_backbone_wg_rx_bytes", stats.get("traffic_backbone_wg_rx")),
            ("node_traffic_backbone_wg_tx_bytes", stats.get("traffic_backbone_wg_tx")),
            ("node_traffic_backbone_fastd_rx_bytes", stats.get("traffic_backbone_fastd_rx")),
            ("node_traffic_backbone_fastd_tx_bytes", stats.get("traffic_backbone_fastd_tx")),
            ("node_clients_wifi24", stats.get("clients_2g")),
            ("node_clients_wifi5", stats.get("clients_5g")),
            ("node_clients_total", _sum_optional(stats.get("clients_2g"), stats.get("clients_5g"))),
            ("node_load", stats.get("load_avg_5")),
            ("node_memory_total", stats.get("mem_total")),
            ("node_memory_available", stats.get("mem_free")),
        ]
        for band in ("2g", "5g"):
            for suffix in _AIRTIME_SUFFIXES:
                metrics.append((f"node_memory_airtime_{band}_{suffix}", stats.get(f"airtime_{band}_{suffix}")))

        lines: list[str] = []
        for name, value in metrics:
            line = _metric_line(name, label_str, value)
            if line is not None:
                lines.append(line)
        return lines

    def _link_lines(self, states: list[NodeState], node_by_id: dict[str, NodeState], now: datetime) -> list[str]:
        lines: list[str] = []
        for link in _merge_links(states):
            left = str(link.get("left_node_id") or "")
            right = str(link.get("right_node_id") or "")
            left_state = node_by_id.get(left)
            right_state = node_by_id.get(right)
            if left_state is None or right_state is None:
                continue
            if not self._community_ok(left_state) or not self._community_ok(right_state):
                continue
            self._append_link_direction(lines, left_state, right_state, link.get("left_tq"), link.get("left_ts"), now)
            self._append_link_direction(lines, right_state, left_state, link.get("right_tq"), link.get("right_ts"), now)
        return lines

    def _append_link_direction(
        self,
        lines: list[str],
        source: NodeState,
        target: NodeState,
        tq: Any,
        ts: Any,
        now: datetime,
    ) -> None:
        if tq is None:
            return
        timestamp = _parse_timestamp(ts)
        if timestamp is None or (now - timestamp).total_seconds() >= self.link_max_age_seconds:
            return
        labels = {
            "source_id": source.node_id,
            "source_hostname": source.info.get("name"),
            "target_id": target.node_id,
            "target_hostname": target.info.get("name"),
        }
        label_str = _format_labels(labels, ("source_id", "source_hostname", "target_id", "target_hostname"))
        line = _metric_line("link_tq", label_str, tq, timestamp_ms=int(timestamp.timestamp() * 1000))
        if line is not None:
            lines.append(line)


def _is_server(state: NodeState) -> bool:
    return state.node_type == "server" or str(state.info.get("node_type") or "") == "server"


def _autoupdater_label(value: Any) -> str | None:
    if value is None:
        return None
    return "enabled" if bool(value) else "disabled"


def _sum_optional(*values: Any) -> int | None:
    present = [int(value) for value in values if value is not None]
    return sum(present) if present else None


def _metric_line(name: str, label_str: str, value: Any, timestamp_ms: int | None = None) -> str | None:
    formatted = _format_value(value)
    if formatted is None:
        return None
    line = f"{name}{label_str} {formatted}"
    if timestamp_ms is not None:
        line += f" {timestamp_ms}"
    return line


def _format_labels(labels: dict[str, Any], keys: tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in keys:
        value = labels.get(key)
        if value is None or value == "":
            continue
        parts.append(f'{key}="{_escape(str(value))}"')
    if not parts:
        return ""
    return "{" + ",".join(parts) + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return repr(value)
    if isinstance(value, int):
        return str(value)
    try:
        return repr(float(str(value)))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
