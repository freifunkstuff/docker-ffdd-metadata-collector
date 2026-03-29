from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import FetchOutcome
from .sysinfo_parsers import parse_json_bytes


@dataclass(slots=True, frozen=True)
class SysinfoFetcher:
    user_agent: str = "metadata-collector/0.1"

    async def fetch(self, node_id: str, primary_ip: str, timeout_seconds: float) -> FetchOutcome:
        return await asyncio.to_thread(self._fetch_sync, node_id, primary_ip, timeout_seconds)

    def _fetch_sync(self, node_id: str, primary_ip: str, timeout_seconds: float) -> FetchOutcome:
        url = f"http://{primary_ip}/sysinfo-json.cgi"
        fetched_at = _utcnow()
        started_at = monotonic()
        request = Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
                status = getattr(response, "status", None)
            parse_result = parse_json_bytes(payload, node_id_hint=node_id)
            return FetchOutcome(
                node_id=node_id,
                primary_ip=primary_ip,
                fetched_at=fetched_at,
                success=True,
                parse_result=parse_result,
                http_status=status,
                duration_ms=_elapsed_ms(started_at),
                result_kind="success",
                timeout_ms=int(timeout_seconds * 1000),
            )
        except HTTPError as exc:
            return FetchOutcome(
                node_id=node_id,
                primary_ip=primary_ip,
                fetched_at=fetched_at,
                success=False,
                http_status=exc.code,
                error=f"HTTPError: {exc}",
                duration_ms=_elapsed_ms(started_at),
                result_kind="http_error",
                timeout_ms=int(timeout_seconds * 1000),
            )
        except (URLError, TimeoutError, JSONDecodeError, ValueError) as exc:
            return FetchOutcome(
                node_id=node_id,
                primary_ip=primary_ip,
                fetched_at=fetched_at,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=_elapsed_ms(started_at),
                result_kind=_result_kind_for_exception(exc),
                timeout_ms=int(timeout_seconds * 1000),
            )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started_at: float) -> int:
    return int((monotonic() - started_at) * 1000)


def _result_kind_for_exception(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, ValueError):
        return "parse_error"
    if isinstance(exc, URLError):
        reason = str(exc.reason).lower() if getattr(exc, "reason", None) is not None else str(exc).lower()
        if "timed out" in reason:
            return "timeout"
        if "refused" in reason:
            return "connection_refused"
        if "no route to host" in reason:
            return "no_route"
        return "network_error"
    return "error"
