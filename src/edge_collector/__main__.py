from __future__ import annotations

import argparse
import asyncio
import logging

from .app import run_from_env
from .config import EdgeCollectorConfig
from .logging_utils import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkconfig", action="store_true")
    args = parser.parse_args(argv)

    config = EdgeCollectorConfig.from_env()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)
    if args.checkconfig:
        logger.info("config check passed")
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
