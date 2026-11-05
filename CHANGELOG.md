# Changelog

## v1.0.1

- Base the collector on the released `ghcr.io/freifunkstuff/ffdd-node:v1.0.1`, including verified registration TLS and node runtime recovery fixes.
- Retry failed snapshot publication and supervise background loops instead of silently leaving stale output.
- Reject nonfinite numeric input and overflowing uptime values; publish legacy nonfinite persisted values as JSON null.
- Preserve the current discovery address across reloads and late responses from an older address.
- Persist polling deadlines and restore backoff across restarts, tolerating missing or malformed legacy deadlines.
- Anchor Meshviewer boot timestamps to the successful measurement time and decode contact names exactly once.
- Sort mixed numeric/text discovery IDs safely and clean temporary files after failed JSON writes.
- Add 30 tests, including producer/consumer contracts against the actual Node base; run all 97 tests during the Docker build.

## v1.0.0

Initial tagged metadata collector release.
