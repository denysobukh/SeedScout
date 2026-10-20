"""Application entry point; network services are introduced in later steps."""

import argparse
import logging
import sys

from tg_torrent_downloader.config import ConfigurationError, Settings
from tg_torrent_downloader.logging import configure_logging


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
    logging.getLogger(__name__).error(
        "Bot polling is not implemented yet. Use --check-config to validate this foundation."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
