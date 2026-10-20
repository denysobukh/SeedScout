"""Application entry point for configuration checks and Telegram long polling."""

import argparse
import asyncio
import logging
import sqlite3
import sys

from aiogram.exceptions import TelegramAPIError

from tg_torrent_downloader.bot import run_bot
from tg_torrent_downloader.config import ConfigurationError, Settings
from tg_torrent_downloader.logging import configure_logging
from tg_torrent_downloader.storage import StateError


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram Torrent Downloader")
    parser.add_argument(
        "--check-config", action="store_true", help="validate settings without network IO"
    )
    args = parser.parse_args()
    try:
        settings = Settings.load()
        if not args.check_config:
            configure_logging(settings)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print("Configuration is valid; no services contacted", file=sys.stderr)
        return 0
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0
    except (TelegramAPIError, OSError, sqlite3.Error, StateError) as error:
        logging.getLogger(__name__).error(
            "Bot startup or polling failed (%s)", type(error).__name__
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
