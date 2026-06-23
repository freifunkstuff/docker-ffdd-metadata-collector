#!/usr/bin/env python3
"""Copy active time series from one VictoriaMetrics to another, fixing the dot-bug.

Streams `/api/v1/export` (JSON lines) from the source, replaces every "." with
"_" in metric names *and* label names (label values are left untouched), and
imports the result into the destination via `/api/v1/import`.

This makes the historic FFDD metrics (e.g. ``node_clients.total``,
``link_tq{source.id=...}``) match exactly what the new collector writes
(``node_clients_total``, ``link_tq{source_id=...}``).

Stdlib only; runs anywhere with python3.

Example (local migration on ffle1):

    python3 migrate-victoriametrics.py \
        --src-url http://OLD_VM:8428 \
        --dst-url http://NEW_VM:8428

Default selectors cover the app metrics and exclude VM/Go/process internals.
Dead metric names without data simply export nothing.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_MATCHES = [
    '{__name__=~"node_.*"}',
    '{__name__=~"link_.*"}',
    '{__name__=~"global_.*"}',
    '{__name__=~"model_.*"}',
    '{__name__=~"firmware_.*"}',
    '{__name__=~"autoupdater_.*"}',
    '{__name__="flag"}',
]


def _auth_header(user: str | None, password: str | None) -> dict[str, str]:
    if not user:
        return {}
    token = base64.b64encode(f"{user}:{password or ''}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def rename(metric: dict[str, str]) -> dict[str, str]:
    """Replace dots with underscores in the metric name and all label keys.

    The metric name lives in the *value* of the ``__name__`` key, while label
    names are dict keys; both need fixing, label values stay untouched.
    """
    out: dict[str, str] = {}
    for key, value in metric.items():
        new_key = key.replace(".", "_")
        if new_key == "__name__":
            value = value.replace(".", "_")
        out[new_key] = value
    return out


def downsample(values: list, timestamps: list, interval_ms: int) -> tuple[list, list]:
    """Keep the last sample per ``interval_ms`` bucket (original timestamp kept).

    Correct for counters (last preserves the cumulative value, so rate/increase
    stay intact) and matches the live collector for gauges (it also writes the
    instantaneous value once per interval).
    """
    last_per_bucket: dict[int, tuple[int, object]] = {}
    for ts, val in zip(timestamps, values):
        bucket = ts - (ts % interval_ms)
        current = last_per_bucket.get(bucket)
        if current is None or ts >= current[0]:
            last_per_bucket[bucket] = (ts, val)
    items = sorted(last_per_bucket.values())
    new_timestamps = [ts for ts, _ in items]
    new_values = [val for _, val in items]
    return new_values, new_timestamps


def transform_line(line: bytes, interval_ms: int = 0) -> bytes | None:
    line = line.strip()
    if not line:
        return None
    obj = json.loads(line)
    metric = obj.get("metric")
    if not isinstance(metric, dict):
        return None
    obj["metric"] = rename(metric)
    if interval_ms > 0:
        values = obj.get("values")
        timestamps = obj.get("timestamps")
        if isinstance(values, list) and isinstance(timestamps, list) and len(values) == len(timestamps):
            obj["values"], obj["timestamps"] = downsample(values, timestamps, interval_ms)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_time(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())


def iter_time_chunks(start: int, end: int, chunk_seconds: int):
    t = start
    while t < end:
        yield t, min(t + chunk_seconds, end)
        t += chunk_seconds


def export_stream(base_url: str, match: str, start: str | None, end: str | None, headers: dict[str, str]):
    params = [("match[]", match)]
    if start:
        params.append(("start", start))
    if end:
        params.append(("end", end))
    url = f"{base_url.rstrip('/')}/api/v1/export?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "vm-migrate/1.0", **headers})
    return urlopen(request, timeout=3600)


def import_batch(base_url: str, body: bytes, headers: dict[str, str]) -> None:
    url = f"{base_url.rstrip('/')}/api/v1/import"
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"User-Agent": "vm-migrate/1.0", "Content-Type": "application/stream+json", **headers},
    )
    with urlopen(request, timeout=600) as response:
        response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src-url", required=True, help="Source VictoriaMetrics base URL (e.g. http://host:8428)")
    parser.add_argument("--dst-url", required=True, help="Destination VictoriaMetrics base URL")
    parser.add_argument("--src-user")
    parser.add_argument("--src-pass")
    parser.add_argument("--dst-user")
    parser.add_argument("--dst-pass")
    parser.add_argument("--match", action="append", help="Series selector(s); repeatable. Default: app metrics")
    parser.add_argument("--start", help="Export start (RFC3339 or unix seconds). Default: now - lookback-days")
    parser.add_argument("--end", help="Export end (RFC3339 or unix seconds). Default: now")
    parser.add_argument("--lookback-days", type=int, default=1825, help="Default history depth if --start unset (default 5y)")
    parser.add_argument("--chunk-days", type=int, default=7, help="Export window size per request (default 7). Avoids huge-range 400s.")
    parser.add_argument("--batch-bytes", type=int, default=8 * 1024 * 1024, help="Import batch size (default 8 MiB)")
    parser.add_argument(
        "--normalize-interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Downsample to one (last) sample per interval, e.g. 300 for 5 min. 0 = off (raw).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Transform and count, but do not import")
    args = parser.parse_args(argv)

    interval_ms = args.normalize_interval * 1000
    matches = args.match or DEFAULT_MATCHES
    src_headers = _auth_header(args.src_user, args.src_pass)
    dst_headers = _auth_header(args.dst_user, args.dst_pass)

    now_ts = int(time.time())
    end_ts = parse_time(args.end, now_ts)
    start_ts = parse_time(args.start, now_ts - args.lookback_days * 86400)
    chunk_seconds = args.chunk_days * 86400
    # Align chunk boundaries to the normalize interval so no 5-min bucket spans a
    # chunk edge (which would yield two points per bucket).
    if interval_ms > 0:
        start_ts -= start_ts % args.normalize_interval

    total_segments = 0
    sample_shown = False

    for match in matches:
        segments = 0
        buffer: list[bytes] = []
        buffer_size = 0

        def flush() -> None:
            nonlocal buffer, buffer_size
            if not buffer or args.dry_run:
                buffer, buffer_size = [], 0
                return
            import_batch(args.dst_url, b"\n".join(buffer) + b"\n", dst_headers)
            buffer, buffer_size = [], 0

        for chunk_start, chunk_end in iter_time_chunks(start_ts, end_ts, chunk_seconds):
            try:
                stream = export_stream(args.src_url, match, str(chunk_start), str(chunk_end), src_headers)
            except (HTTPError, URLError) as exc:
                print(f"  ! export failed for {match} [{chunk_start}-{chunk_end}]: {exc}", file=sys.stderr)
                return 2

            with stream:
                for raw in stream:
                    transformed = transform_line(raw, interval_ms)
                    if transformed is None:
                        continue
                    segments += 1
                    if not sample_shown:
                        before = json.loads(raw)["metric"].get("__name__")
                        after = json.loads(transformed)["metric"].get("__name__")
                        print(f"  sample rename: {before!r} -> {after!r}")
                        sample_shown = True
                    buffer.append(transformed)
                    buffer_size += len(transformed) + 1
                    if buffer_size >= args.batch_bytes:
                        flush()
        flush()

        total_segments += segments
        print(f"  {match}: {segments} series-segments")

    action = "would migrate" if args.dry_run else "migrated"
    print(f"{action} {total_segments} series-segments from {args.src_url} to {args.dst_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
