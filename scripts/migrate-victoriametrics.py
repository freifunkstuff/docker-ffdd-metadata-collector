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


def transform_line(line: bytes) -> bytes | None:
    line = line.strip()
    if not line:
        return None
    obj = json.loads(line)
    metric = obj.get("metric")
    if not isinstance(metric, dict):
        return None
    obj["metric"] = rename(metric)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    parser.add_argument("--start", help="Export start (RFC3339 or unix seconds). Default: all history")
    parser.add_argument("--end", help="Export end (RFC3339 or unix seconds). Default: now")
    parser.add_argument("--batch-bytes", type=int, default=8 * 1024 * 1024, help="Import batch size (default 8 MiB)")
    parser.add_argument("--dry-run", action="store_true", help="Transform and count, but do not import")
    args = parser.parse_args(argv)

    matches = args.match or DEFAULT_MATCHES
    src_headers = _auth_header(args.src_user, args.src_pass)
    dst_headers = _auth_header(args.dst_user, args.dst_pass)

    total_series = 0
    total_renamed = 0
    sample_shown = False

    for match in matches:
        series = 0
        buffer: list[bytes] = []
        buffer_size = 0

        def flush() -> None:
            nonlocal buffer, buffer_size
            if not buffer or args.dry_run:
                buffer, buffer_size = [], 0
                return
            import_batch(args.dst_url, b"\n".join(buffer) + b"\n", dst_headers)
            buffer, buffer_size = [], 0

        try:
            stream = export_stream(args.src_url, match, args.start, args.end, src_headers)
        except (HTTPError, URLError) as exc:
            print(f"  ! export failed for {match}: {exc}", file=sys.stderr)
            return 2

        with stream:
            for raw in stream:
                transformed = transform_line(raw)
                if transformed is None:
                    continue
                series += 1
                if not sample_shown:
                    before = json.loads(raw)["metric"].get("__name__")
                    after = json.loads(transformed)["metric"].get("__name__")
                    print(f"  sample rename: {before!r} -> {after!r}")
                    sample_shown = True
                if "." in raw.split(b"}", 1)[0].decode("utf-8", "replace"):
                    total_renamed += 1
                buffer.append(transformed)
                buffer_size += len(transformed) + 1
                if buffer_size >= args.batch_bytes:
                    flush()
            flush()

        total_series += series
        print(f"  {match}: {series} series")

    action = "would migrate" if args.dry_run else "migrated"
    print(f"{action} {total_series} series ({total_renamed} contained dots) from {args.src_url} to {args.dst_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
