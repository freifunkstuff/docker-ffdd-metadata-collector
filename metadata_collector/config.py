from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_SOURCE_URL = "http://172.18.0.2/nodes.json"
DEFAULT_SOURCE_PATH = "/run/freifunk/sysinfo/nodes.json"
DEFAULT_DEFAULTS_PATH = "/usr/local/share/freifunk/defaults-metadata-collector.yaml"


def _load_defaults(defaults_path: Path) -> dict[str, object]:
    if not defaults_path.exists():
        return {}
    loaded = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _get_config_value(defaults: dict[str, object], env_name: str, fallback: str) -> str:
    value = os.getenv(env_name)
    if value not in (None, ""):
        return value
    default_value = defaults.get(env_name)
    if default_value in (None, ""):
        return fallback
    return str(default_value)


def _get_env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return float(value)


def _get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return int(value)


def _get_config_float(defaults: dict[str, object], env_name: str, fallback: float) -> float:
    return float(_get_config_value(defaults, env_name, str(fallback)))


def _get_config_int(defaults: dict[str, object], env_name: str, fallback: int) -> int:
    return int(_get_config_value(defaults, env_name, str(fallback)))


def _parse_communities(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(slots=True, frozen=True)
class MetadataCollectorConfig:
    source_type: str
    source_url: str
    source_path: Path
    storage_backend: str
    discovery_interval_seconds: float
    snapshot_interval_seconds: float
    fetch_timeout_normal_seconds: float
    fetch_timeout_slow_seconds: float
    fetch_timeout_very_slow_seconds: float
    fetch_concurrency: int
    poll_interval_normal_seconds: float
    poll_interval_slow_seconds: float
    poll_interval_very_slow_seconds: float
    max_consecutive_failures_before_very_slow: int
    source_stale_after_seconds: float
    node_retention_seconds: float
    data_dir: Path
    run_dir: Path
    webroot_dir: Path
    state_dir: Path
    discovery_state_path: Path
    node_info_dir: Path
    node_status_dir: Path
    node_metadata_path: Path
    status_path: Path
    meshviewer_path: Path
    published_node_metadata_path: Path
    published_status_path: Path
    published_meshviewer_path: Path
    request_user_agent: str
    log_level: str
    log_summary_interval_seconds: float
    online_window_seconds: float
    meshviewer_online_window_seconds: float
    meshviewer_hide_temp_after_seconds: float
    meshviewer_hide_stale_after_days: float
    victoriametrics_url: str
    victoriametrics_username: str
    victoriametrics_password: str
    metrics_interval_seconds: float
    metrics_link_max_age_seconds: float
    metrics_communities: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "MetadataCollectorConfig":
        defaults_path = Path(os.getenv("METADATA_COLLECTOR_DEFAULTS_PATH", DEFAULT_DEFAULTS_PATH)).resolve()
        defaults = _load_defaults(defaults_path)

        data_dir = Path(_get_config_value(defaults, "METADATA_COLLECTOR_DATA_DIR", "/data")).resolve()
        run_dir = Path(_get_config_value(defaults, "METADATA_COLLECTOR_RUN_DIR", "/run/freifunk/state")).resolve()
        webroot_dir = Path(_get_config_value(defaults, "METADATA_COLLECTOR_WEBROOT_DIR", "/run/freifunk/www")).resolve()
        state_dir = Path(_get_config_value(defaults, "METADATA_COLLECTOR_STATE_DIR", str(data_dir / "state"))).resolve()
        node_metadata_path = Path(
            _get_config_value(
                defaults,
                "METADATA_COLLECTOR_NODE_METADATA_PATH",
                _get_config_value(defaults, "METADATA_COLLECTOR_SNAPSHOT_PATH", str(run_dir / "node-metadata.json")),
            )
        ).resolve()
        status_path = Path(
            _get_config_value(defaults, "METADATA_COLLECTOR_STATUS_PATH", str(run_dir / "node-metadata-status.json"))
        ).resolve()
        meshviewer_path = Path(
            _get_config_value(defaults, "METADATA_COLLECTOR_MESHVIEWER_PATH", str(run_dir / "meshviewer" / "meshviewer.json"))
        ).resolve()
        published_node_metadata_path = Path(
            _get_config_value(defaults, "METADATA_COLLECTOR_PUBLISHED_NODE_METADATA_PATH", str(webroot_dir / "node-metadata.json"))
        )
        published_status_path = Path(
            _get_config_value(defaults, "METADATA_COLLECTOR_PUBLISHED_STATUS_PATH", str(webroot_dir / "node-metadata-status.json"))
        )
        published_meshviewer_path = Path(
            _get_config_value(defaults, "METADATA_COLLECTOR_PUBLISHED_MESHVIEWER_PATH", str(webroot_dir / "meshviewer" / "meshviewer.json"))
        )
        return cls(
            source_type=_get_config_value(defaults, "METADATA_COLLECTOR_SOURCE", "file-json"),
            source_url=_get_config_value(defaults, "METADATA_COLLECTOR_SOURCE_URL", DEFAULT_SOURCE_URL),
            source_path=Path(_get_config_value(defaults, "METADATA_COLLECTOR_SOURCE_PATH", DEFAULT_SOURCE_PATH)).resolve(),
            storage_backend=_get_config_value(defaults, "METADATA_COLLECTOR_STORAGE", "yaml-memory"),
            discovery_interval_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_DISCOVERY_INTERVAL", 30.0),
            snapshot_interval_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_SNAPSHOT_INTERVAL", 15.0),
            fetch_timeout_normal_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_FETCH_TIMEOUT_NORMAL", 10.0),
            fetch_timeout_slow_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_FETCH_TIMEOUT_SLOW", 30.0),
            fetch_timeout_very_slow_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_FETCH_TIMEOUT_VERY_SLOW", 60.0),
            fetch_concurrency=_get_config_int(defaults, "METADATA_COLLECTOR_FETCH_CONCURRENCY", 64),
            poll_interval_normal_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_POLL_INTERVAL_NORMAL", 300.0),
            poll_interval_slow_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_POLL_INTERVAL_SLOW", 900.0),
            poll_interval_very_slow_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_POLL_INTERVAL_VERY_SLOW", 1800.0),
            max_consecutive_failures_before_very_slow=_get_config_int(defaults, "METADATA_COLLECTOR_FAILURES_BEFORE_VERY_SLOW", 3),
            source_stale_after_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_SOURCE_STALE_AFTER", 86400.0),
            node_retention_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_NODE_RETENTION_SECONDS", 90.0 * 24.0 * 3600.0),
            data_dir=data_dir,
            run_dir=run_dir,
            webroot_dir=webroot_dir,
            state_dir=state_dir,
            discovery_state_path=Path(
                _get_config_value(defaults, "METADATA_COLLECTOR_DISCOVERY_STATE_PATH", str(state_dir / "discovery.yaml"))
            ).resolve(),
            node_info_dir=Path(_get_config_value(defaults, "METADATA_COLLECTOR_INFO_DIR", str(state_dir / "info"))).resolve(),
            node_status_dir=Path(_get_config_value(defaults, "METADATA_COLLECTOR_STATUS_DIR", str(state_dir / "status"))).resolve(),
            node_metadata_path=node_metadata_path,
            status_path=status_path,
            meshviewer_path=meshviewer_path,
            published_node_metadata_path=published_node_metadata_path,
            published_status_path=published_status_path,
            published_meshviewer_path=published_meshviewer_path,
            request_user_agent=_get_config_value(defaults, "METADATA_COLLECTOR_USER_AGENT", "metadata-collector/0.1"),
            log_level=_get_config_value(defaults, "METADATA_COLLECTOR_LOG_LEVEL", "INFO"),
            log_summary_interval_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_LOG_SUMMARY_INTERVAL", 60.0),
            online_window_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_ONLINE_WINDOW_SECONDS", 600.0),
            meshviewer_online_window_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_MESHVIEWER_ONLINE_WINDOW_SECONDS", 600.0),
            meshviewer_hide_temp_after_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_MESHVIEWER_HIDE_TEMP_AFTER_SECONDS", 1800.0),
            meshviewer_hide_stale_after_days=_get_config_float(defaults, "METADATA_COLLECTOR_MESHVIEWER_HIDE_STALE_AFTER_DAYS", 30.0),
            victoriametrics_url=_get_config_value(defaults, "METADATA_COLLECTOR_VICTORIAMETRICS_URL", ""),
            victoriametrics_username=_get_config_value(defaults, "METADATA_COLLECTOR_VICTORIAMETRICS_USERNAME", ""),
            victoriametrics_password=_get_config_value(defaults, "METADATA_COLLECTOR_VICTORIAMETRICS_PASSWORD", ""),
            metrics_interval_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_METRICS_INTERVAL", 300.0),
            metrics_link_max_age_seconds=_get_config_float(defaults, "METADATA_COLLECTOR_METRICS_LINK_MAX_AGE_SECONDS", 900.0),
            metrics_communities=_parse_communities(_get_config_value(defaults, "METADATA_COLLECTOR_METRICS_COMMUNITIES", "")),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.webroot_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.discovery_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.node_info_dir.mkdir(parents=True, exist_ok=True)
        self.node_status_dir.mkdir(parents=True, exist_ok=True)
        self.node_metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.meshviewer_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_node_metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_status_path.parent.mkdir(parents=True, exist_ok=True)
        self.published_meshviewer_path.parent.mkdir(parents=True, exist_ok=True)
