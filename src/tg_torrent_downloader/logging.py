"""Apply redaction after formatting, including exception tracebacks."""

import logging
import re
import sys
from urllib.parse import quote, quote_plus

from tg_torrent_downloader.config import Settings


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        variants = {variant for s in secrets for variant in (s, quote(s, safe=""), quote_plus(s))}
        self.secrets = sorted(variants, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"[0-9]+:[A-Za-z0-9_-]{20,}", "[REDACTED]", text)
        text = re.sub(
            r"(?im)((?:set-cookie|cookie|authorization)\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text
        )
        return text


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(settings.secret_values()))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # Request URLs and headers should never be emitted by HTTP client debug logging.
    for name in ("aiohttp", "aiogram", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
