from __future__ import annotations

from datetime import datetime
import logging


def configure_logging(level_name: str) -> None:
    level = _parse_log_level(level_name)
    formatter = _MetadataCollectorFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=[handler],
        force=True,
    )


class _MetadataCollectorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        return f"{self.formatTime(record)} [metadata-collector] {message}"

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        local_time = datetime.fromtimestamp(record.created).astimezone()
        return local_time.strftime("%Y-%m-%d %H:%M:%S %z")


def _parse_log_level(value: str) -> int:
    candidate = getattr(logging, str(value).upper(), None)
    return candidate if isinstance(candidate, int) else logging.INFO