from __future__ import annotations

import argparse
import asyncio
import logging

from .app import run_from_env
from .bootstrap import DEFAULT_LEIPZIG_MESHVIEWER_URL, BootstrapError, run_bootstrap
from .config import MetadataCollectorConfig
from .logging_utils import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkconfig", action="store_true")
    parser.add_argument(
        "--bootstrap-meshviewer",
        nargs="?",
        const=DEFAULT_LEIPZIG_MESHVIEWER_URL,
        default=None,
        metavar="URL_OR_PATH",
        help="Seed the state once from an existing meshviewer.json, then exit "
        f"(default source: {DEFAULT_LEIPZIG_MESHVIEWER_URL})",
    )
    args = parser.parse_args(argv)

    config = MetadataCollectorConfig.from_env()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)
    if args.checkconfig:
        logger.info("config check passed")
        return 0

    if args.bootstrap_meshviewer:
        try:
            run_bootstrap(config, args.bootstrap_meshviewer)
        except BootstrapError as exc:
            logger.error("bootstrap failed: %s", exc)
            return 1
        return 0

    try:
        asyncio.run(run_from_env(config))
    except KeyboardInterrupt:
        logger.info("shutdown requested signal=SIGINT")
        logger.info("collector stopped")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
