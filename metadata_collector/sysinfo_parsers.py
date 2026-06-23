from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote_plus

from .models import ParseResult


@dataclass(slots=True, frozen=True)
class SysinfoVariant:
    key: str
    version: str | None
    node_type: str | None


GENERIC_VARIANT = SysinfoVariant(key="generic", version=None, node_type=None)


def _get_path(data: dict[str, Any] | None, *path: str) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _decode_urlencoded_text(value: Any) -> str | None:
    text = _coerce_str(value)
    if text in (None, ""):
        return text
    return unquote_plus(text)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _parse_nbytes(value: Any) -> int | None:
    text = _coerce_str(value)
    if not text:
        return None
    parts = text.strip().split()
    if not parts:
        return None
    try:
        amount = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower() if len(parts) > 1 else "b"
    factors = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
    }
    factor = factors.get(unit)
    if factor is None:
        return None
    return int(amount * factor)


def _parse_uptime_seconds(value: Any) -> float | None:
    text = _coerce_str(value)
    if not text:
        return None
    first = text.split()[0]
    return _coerce_float(first)


def _parse_load_avg5(value: Any) -> float | None:
    text = _coerce_str(value)
    if not text:
        return None
    parts = text.split()
    if len(parts) < 2:
        return None
    return _coerce_float(parts[1])


def _parse_airtime(value: Any) -> dict[str, int | None]:
    text = _coerce_str(value)
    if not text:
        return {"active": None, "busy": None, "rx": None, "tx": None}
    parts = [part.strip() for part in text.split(",")]
    numbers = [_coerce_int(part) for part in parts[:4]]
    while len(numbers) < 4:
        numbers.append(None)
    return {
        "active": numbers[0],
        "busy": numbers[1],
        "rx": numbers[2],
        "tx": numbers[3],
    }


def _normalize_carrier(value: Any) -> bool | None:
    text = _coerce_str(value)
    if text is None:
        return None
    text = text.strip().lower()
    if text in {"1", "up"}:
        return True
    if text in {"0", "down"}:
        return False
    return None


def _normalize_speed(value: Any) -> int | None:
    text = _coerce_str(value)
    if not text:
        return None
    text = text.strip().lower()
    mapping = {
        "10": 10,
        "10baset": 10,
        "100": 100,
        "100baset": 100,
        "1000": 1000,
        "1000baset": 1000,
    }
    return mapping.get(text)


def _normalize_ports(network_switch: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(network_switch, dict):
        return [], False

    if isinstance(network_switch.get("switch"), list):
        return _normalize_port_list(network_switch.get("switch")), False
    if isinstance(network_switch.get("switch0"), list):
        return _normalize_port_list(network_switch.get("switch0")), False

    if isinstance(network_switch.get("wan"), dict) or isinstance(network_switch.get("lan"), dict):
        ports: list[dict[str, Any]] = []
        for name in ("wan", "lan"):
            port = network_switch.get(name)
            if isinstance(port, dict):
                up = _normalize_carrier(port.get("carrier"))
                speed = _normalize_speed(port.get("speed"))
                status_mbps = speed if up else 0 if up is False else None
                ports.append(
                    {
                        "index": len(ports) + 1,
                        "name": name,
                        "up": up,
                        "status_mbps": status_mbps,
                    }
                )
        return ports, False

    return [], bool(network_switch)


def _normalize_port_list(ports: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, port in enumerate(ports, start=1):
        if not isinstance(port, dict):
            continue
        up = _normalize_carrier(port.get("carrier"))
        speed = _normalize_speed(port.get("speed"))
        status_mbps = speed if up else 0 if up is False else None
        normalized.append(
            {
                "index": index,
                "name": _coerce_str(port.get("port")),
                "up": up,
                "status_mbps": status_mbps,
            }
        )
    return normalized


def _first_present(*values: Any) -> Any:
    """First value that is not None. Unlike ``a or b`` this keeps a legit 0."""
    for value in values:
        if value is not None:
            return value
    return None


def _set_if(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def detect_variant(payload: dict[str, Any]) -> SysinfoVariant:
    version = _coerce_str(payload.get("version"))
    node_type = _coerce_str(_get_path(payload, "data", "system", "node_type"))
    if version == "18" and node_type == "node":
        return SysinfoVariant(key="v18-node", version=version, node_type=node_type)
    if version == "18" and node_type == "server":
        return SysinfoVariant(key="v18-server", version=version, node_type=node_type)
    if version == "18" and node_type == "mobile":
        return SysinfoVariant(key="v18-mobile", version=version, node_type=node_type)
    if version == "17" and node_type == "node":
        return SysinfoVariant(key="v17-node", version=version, node_type=node_type)
    if version == "17" and node_type == "server":
        return SysinfoVariant(key="v17-server", version=version, node_type=node_type)
    if version == "16" and node_type == "server":
        return SysinfoVariant(key="v16-server", version=version, node_type=node_type)
    return SysinfoVariant(key=f"generic:{version or 'unknown'}:{node_type or 'unknown'}", version=version, node_type=node_type)


class GenericSysinfoParser:
    parser_name = "GenericSysinfoParser"

    def parse(self, payload: dict[str, Any], node_id_hint: str | None = None) -> ParseResult:
        variant = detect_variant(payload)

        data = payload.get("data") or {}
        system = data.get("system") or {}
        common = data.get("common") or {}
        firmware = data.get("firmware") or {}
        contact = data.get("contact") or {}
        gps = data.get("gps") or {}
        statistic = data.get("statistic") or {}
        bmxd = data.get("bmxd") or {}
        backbone = data.get("backbone") or {}
        network_switch = data.get("network_switch") or {}
        traffic_shaping = data.get("traffic_shaping") or {}
        connections = data.get("connections") or []

        warnings: list[str] = []
        field_sources: dict[str, str] = {}

        node_id = _coerce_str(common.get("node")) or node_id_hint
        node_type = _coerce_str(system.get("node_type"))

        info: dict[str, Any] = {}
        stats: dict[str, Any] = {}

        community = None
        if common.get("community") not in (None, ""):
            community = _coerce_str(common.get("community"))
            field_sources["community"] = "data.common.community"
        elif common.get("city") not in (None, ""):
            community = _coerce_str(common.get("city"))
            field_sources["community"] = "data.common.city"
            if node_type != "server":
                warnings.append("community_fallback_from_city")
        _set_if(info, "community", community)

        model = None
        if system.get("model2") not in (None, ""):
            model = _coerce_str(system.get("model2"))
            field_sources["model"] = "data.system.model2"
        elif system.get("model") not in (None, ""):
            model = _coerce_str(system.get("model"))
            field_sources["model"] = "data.system.model"
        _set_if(info, "model", model)

        for key, value, source in [
            ("name", _decode_urlencoded_text(contact.get("name")), "data.contact.name"),
            ("location", _decode_urlencoded_text(contact.get("location")), "data.contact.location"),
            ("contact_email", _decode_urlencoded_text(contact.get("email")), "data.contact.email"),
            ("note", _decode_urlencoded_text(contact.get("note")), "data.contact.note"),
            ("node_type", node_type, "data.system.node_type"),
            ("group", common.get("group_id"), "data.common.group_id"),
            ("city", common.get("city"), "data.common.city"),
            ("domain", common.get("domain"), "data.common.domain"),
            ("network_id", common.get("network_id"), "data.common.network_id"),
            ("primary_ip", common.get("ip"), "data.common.ip"),
            ("auto_update", _parse_bool(system.get("autoupdate")), "data.system.autoupdate"),
            ("cpu_count", _coerce_int(system.get("cpucount")), "data.system.cpucount"),
            ("location_latitude", _coerce_float(gps.get("latitude")), "data.gps.latitude"),
            ("location_longitude", _coerce_float(gps.get("longitude")), "data.gps.longitude"),
            ("location_altitude", _coerce_int(gps.get("altitude")), "data.gps.altitude"),
            ("firmware_base", firmware.get("DISTRIB_DESCRIPTION"), "data.firmware.DISTRIB_DESCRIPTION"),
            ("firmware_release", firmware.get("version"), "data.firmware.version"),
            ("system_board", system.get("board"), "data.system.board"),
            ("system_uname", system.get("uname"), "data.system.uname"),
            (
                "backbone_fastd_pubkey",
                backbone.get("fastd_pubkey") or common.get("fastd_pubkey"),
                "data.backbone.fastd_pubkey|data.common.fastd_pubkey",
            ),
            ("backbone_wg_pubkey", backbone.get("wg_pubkey"), "data.backbone.wg_pubkey"),
        ]:
            if value is not None:
                info[key] = value
                field_sources.setdefault(key, source)

        interfaces = statistic.get("interfaces") if isinstance(statistic.get("interfaces"), dict) else {}
        airtime_2g = _parse_airtime((data.get("airtime") or {}).get("radio2g"))
        airtime_5g = _parse_airtime((data.get("airtime") or {}).get("radio5g"))

        for key, value in [
            ("uptime_seconds", _parse_uptime_seconds(system.get("uptime"))),
            ("load_avg_5", _parse_load_avg5(statistic.get("cpu_load"))),
            ("mem_total", _parse_nbytes(statistic.get("meminfo_MemTotal"))),
            ("mem_free", _parse_nbytes(statistic.get("meminfo_MemFree"))),
            ("clients_2g", _coerce_int(_get_path(statistic, "client2g", "1min"))),
            ("clients_5g", _coerce_int(_get_path(statistic, "client5g", "1min"))),
            ("traffic_wifi_rx", _first_present(_coerce_int(interfaces.get("wifi2_rx")), _coerce_int(statistic.get("traffic_any_ap")))),
            ("traffic_wifi_tx", _first_present(_coerce_int(interfaces.get("wifi2_tx")), _coerce_int(statistic.get("traffic_ap_any")))),
            ("selected_gateway", _coerce_str(_get_path(bmxd, "gateways", "selected"))),
            ("preferred_gateway", _coerce_str(_get_path(bmxd, "gateways", "preferred"))),
            ("connections_count", len(connections) if isinstance(connections, list) else None),
            ("traffic_shaping_enabled", _parse_bool(traffic_shaping.get("enabled")) if isinstance(traffic_shaping, dict) else None),
            ("traffic_shaping_network", _coerce_str(traffic_shaping.get("network")) if isinstance(traffic_shaping, dict) else None),
            ("traffic_shaping_incoming", _coerce_str(traffic_shaping.get("incomming")) if isinstance(traffic_shaping, dict) else None),
            ("traffic_shaping_outgoing", _coerce_str(traffic_shaping.get("outgoing")) if isinstance(traffic_shaping, dict) else None),
        ]:
            _set_if(stats, key, value)

        for prefix, airtime_values in (("airtime_2g", airtime_2g), ("airtime_5g", airtime_5g)):
            for suffix, value in airtime_values.items():
                _set_if(stats, f"{prefix}_{suffix}", value)

        ports, unparsed_switch = _normalize_ports(network_switch)
        if ports:
            stats["switch_ports"] = ports
        elif unparsed_switch:
            warnings.append("network_switch_present_but_unparsed")

        links = _parse_links(node_id=node_id, timestamp=_coerce_str(payload.get("timestamp")), links_payload=bmxd.get("links"))

        if node_type == "server" and not isinstance(statistic.get("interfaces"), dict):
            warnings.append("server_missing_interfaces_block")

        return ParseResult(
            node_id=node_id,
            version=_coerce_str(payload.get("version")),
            timestamp=_coerce_str(payload.get("timestamp")),
            node_type=node_type,
            parser_name=self.parser_name,
            info=info,
            stats=stats,
            links=links,
            parse_warnings=warnings,
            field_sources=field_sources,
        )


def _parse_links(node_id: str | None, timestamp: str | None, links_payload: Any) -> list[dict[str, Any]]:
    if node_id is None or not isinstance(links_payload, list):
        return []
    result: list[dict[str, Any]] = []
    current = str(node_id)
    for raw_link in links_payload:
        if not isinstance(raw_link, dict):
            continue
        other = _coerce_str(raw_link.get("node"))
        if not other or other == current:
            continue
        link_type = _coerce_str(raw_link.get("type"))
        if not link_type:
            interface = _coerce_str(raw_link.get("interface")) or ""
            link_type = "backbone" if interface.startswith("tbb_") else None
        if not link_type:
            continue
        left, right = sorted([current, other], key=lambda value: value.lower())
        item = {
            "type": link_type,
            "left_node_id": left,
            "right_node_id": right,
            "left_rq": None,
            "left_tq": None,
            "left_ts": None,
            "right_rq": None,
            "right_tq": None,
            "right_ts": None,
        }
        if current.lower() == left.lower():
            item["left_rq"] = _coerce_int(raw_link.get("rq"))
            item["left_tq"] = _coerce_int(raw_link.get("tq"))
            item["left_ts"] = timestamp
        else:
            item["right_rq"] = _coerce_int(raw_link.get("rq"))
            item["right_tq"] = _coerce_int(raw_link.get("tq"))
            item["right_ts"] = timestamp
        result.append(item)
    return result


def detect_parser(payload: dict[str, Any]) -> GenericSysinfoParser:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("sysinfo payload must be an object with a data section")
    return GenericSysinfoParser()


def parse_payload(payload: dict[str, Any], node_id_hint: str | None = None) -> ParseResult:
    parser = detect_parser(payload)
    return parser.parse(payload=payload, node_id_hint=node_id_hint)


def parse_json_bytes(raw: bytes, node_id_hint: str | None = None) -> ParseResult:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("sysinfo payload root must be an object")
    return parse_payload(payload, node_id_hint=node_id_hint)
