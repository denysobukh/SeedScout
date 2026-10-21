"""File logging and structured diagnostics; sanitize HTTP data before creating records."""

import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from tg_torrent_downloader.config import ConfigurationError, Settings

_REDACTED = "[REDACTED]"
_SAFE_HEADERS = {
    "host",
    "user-agent",
    "accept",
    "accept-encoding",
    "accept-language",
    "content-type",
    "content-length",
    "connection",
    "date",
    "server",
    "cache-control",
    "expires",
    "pragma",
    "vary",
    "retry-after",
    "cf-ray",
    "cf-cache-status",
    "content-encoding",
}
_SAFE_FIELDS = {"nm", "t", "start", "login", "redirect", "o", "s"}
_SAFE_PATHS = {
    "/",
    "/forum/",
    "/forum/index.php",
    "/forum/login.php",
    "/forum/tracker.php",
    "/forum/viewtopic.php",
    "/forum/dl.php",
    "/forum/captcha.php",
}
_LOG = logging.getLogger("tg_torrent_downloader.diagnostics")


def safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return _REDACTED
        # Never preserve userinfo or fragments. Decode legacy query values without losing text.
        try:
            pairs = parse_qsl(
                parts.query, keep_blank_values=True, encoding="utf-8", errors="strict"
            )
        except UnicodeError:
            pairs = parse_qsl(
                parts.query, keep_blank_values=True, encoding="cp1251", errors="replace"
            )
        authority = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode(safe_fields(pairs))
        path = parts.path if parts.path in _SAFE_PATHS else "/[REDACTED]"
        return urlunsplit((parts.scheme, authority, path, query, ""))
    except ValueError:
        return _REDACTED


def safe_fields(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    for key, value in pairs:
        normalized = key.casefold()
        # Some CAPTCHA field names themselves contain a challenge token.
        label = "cap_code" if normalized.startswith("cap_code") else key
        if normalized == "redirect":
            # Redirect can embed credentials or session IDs in a nested URL.
            value = value if value in {"index.php", "/forum/index.php", "/"} else _REDACTED
        elif normalized not in _SAFE_FIELDS:
            value = _REDACTED
        result.append((label, value))
    return result


def safe_headers(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    for key, value in pairs:
        normalized = key.casefold()
        if normalized in {"location", "referer"}:
            value = safe_url(value)
        elif normalized not in _SAFE_HEADERS:
            value = _REDACTED
        result.append((key, value))
    return result


def safe_post_data(data: bytes | None) -> list[tuple[str, str]] | None:
    if data is None:
        return None
    try:
        # Form bytes are ASCII percent-encoded. Unknown form fields are redacted by default.
        text = data.decode("ascii")
        try:
            pairs = parse_qsl(text, keep_blank_values=True, encoding="utf-8", errors="strict")
        except UnicodeError:
            pairs = parse_qsl(text, keep_blank_values=True, encoding="cp1251", errors="strict")
        return safe_fields(pairs)
    except UnicodeError:
        return [("unparsed_body", _REDACTED)]


def diagnostic_event(event: str, **fields: object) -> None:
    """Accept only caller-selected, sanitized fields, never raw objects or exceptions."""
    _LOG.info(json.dumps({"event": event, **fields}, ensure_ascii=False))


def log_user_input(user_id: int, chat_id: int, value: str, *, sensitive: bool = False) -> None:
    """Record queries/actions or redact sensitive input such as CAPTCHA answers."""
    diagnostic_event(
        "user.input", user_id=user_id, chat_id=chat_id, input=_REDACTED if sensitive else value
    )


def log_state(
    state: str,
    *,
    user_id: int,
    chat_id: int,
    topic_id: int | None = None,
    selection_id: str | None = None,
    submission_status: str | None = None,
) -> None:
    diagnostic_event(
        "bot.state",
        user_id=user_id,
        chat_id=chat_id,
        state=state,
        topic_id=topic_id,
        selection_id=selection_id,
        submission_status=submission_status,
    )


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = []
        self.add_secrets(secrets)

    def add_secrets(self, secrets: tuple[str, ...]) -> None:
        variants = set(self.secrets)
        for secret in secrets:
            if not secret:
                continue
            variants.update((secret, json.dumps(secret, ensure_ascii=False)[1:-1]))
            for encoding in ("utf-8", "cp1251"):
                try:
                    variants.update(
                        (
                            quote(secret, safe="", encoding=encoding),
                            quote_plus(secret, encoding=encoding),
                        )
                    )
                except UnicodeError:
                    pass
        self.secrets = sorted(variants, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, _REDACTED)
        text = re.sub(r"[0-9]+:[A-Za-z0-9_-]{20,}", _REDACTED, text)
        text = re.sub(
            r"(?im)((?:set-cookie|cookie|authorization)\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text
        )
        return text


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The stdlib fallback prints the original record/args, which may contain secrets.
        print(
            "Diagnostic log write failed; check log path and available disk space", file=sys.stderr
        )

    def _open(self):
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace")
        except BaseException:
            os.close(fd)
            raise


def configure_logging(settings: Settings) -> None:
    formatter = RedactingFormatter(settings.secret_values())
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _PrivateRotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
    except OSError:
        raise ConfigurationError("LOG_FILE could not be opened for diagnostic logging") from None
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=settings.log_level, handlers=[stream, file_handler], force=True)
    # HTTP diagnostics use our sanitized records, never dependency debug output.
    for name in ("aiohttp", "aiogram", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def register_secrets(*secrets: str) -> None:
    """Extend output redaction for credentials acquired after application startup."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler.formatter, RedactingFormatter):
            handler.acquire()
            try:
                handler.formatter.add_secrets(secrets)
            finally:
                handler.release()
