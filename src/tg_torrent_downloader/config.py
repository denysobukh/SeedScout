"""Load configuration without exposing supplied values in validation errors."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


class ConfigurationError(ValueError):
    """Configuration is missing or invalid; messages contain field names only."""


def validate_rpc_url(url: str) -> None:
    """Shared validation for configuration and direct Transmission client construction."""
    try:
        parsed = urlsplit(url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.port != 0
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 for c in url)
            and parsed.path.startswith("/")
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigurationError(
            "TRANSMISSION_RPC_URL must be an HTTP(S) URL with a path, "
            "without embedded credentials, query, or fragment"
        )


@dataclass(frozen=True, repr=False)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    transmission_app_path: Path | None
    transmission_rpc_url: str
    transmission_username: str | None
    transmission_password: str | None
    transmission_download_dir: str | None
    database_path: Path
    log_level: str
    log_file: Path
    log_max_bytes: int
    log_backup_count: int

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        env_file: Path | None = Path(".env"),
    ) -> Settings:
        # Explicit current-directory file only; never search parents or expand secrets.
        try:
            values = dict(dotenv_values(env_file, interpolate=False)) if env_file else {}
        except OSError, UnicodeError:
            raise ConfigurationError("Environment file could not be read as UTF-8") from None
        values.update(os.environ if env is None else env)

        def required(key: str) -> str:
            value = values.get(key)
            if not value or not value.strip():
                raise ConfigurationError(f"{key} is required")
            return value

        token = required("TELEGRAM_BOT_TOKEN")
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token):
            raise ConfigurationError("TELEGRAM_BOT_TOKEN has an invalid format")
        raw_ids = required("TELEGRAM_ALLOWED_USER_IDS").split(",")
        if any(not re.fullmatch(r"[1-9][0-9]{0,15}", item.strip()) for item in raw_ids):
            raise ConfigurationError("TELEGRAM_ALLOWED_USER_IDS must contain positive integer IDs")
        allowed_ids = frozenset(int(item.strip()) for item in raw_ids)
        if any(user_id >= 2**52 for user_id in allowed_ids):
            raise ConfigurationError("TELEGRAM_ALLOWED_USER_IDS contains an out-of-range ID")
        url = values.get("TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc")
        validate_rpc_url(url)
        raw_app_path = values.get("TRANSMISSION_APP_PATH") or ""
        app_path = None
        if raw_app_path:
            if not raw_app_path.strip() or any(ord(c) < 32 for c in raw_app_path):
                raise ConfigurationError(
                    "TRANSMISSION_APP_PATH must be an absolute .app bundle path"
                )
            try:
                app_path = Path(raw_app_path).expanduser()
            except RuntimeError:
                raise ConfigurationError(
                    "TRANSMISSION_APP_PATH home directory could not be resolved"
                ) from None
            if not app_path.is_absolute() or app_path.suffix.lower() != ".app":
                raise ConfigurationError(
                    "TRANSMISSION_APP_PATH must be an absolute .app bundle path"
                )
        username = values.get("TRANSMISSION_USERNAME") or None
        password = values.get("TRANSMISSION_PASSWORD") or None
        if (username is None) != (password is None):
            raise ConfigurationError(
                "TRANSMISSION_USERNAME and TRANSMISSION_PASSWORD must be paired"
            )
        log_level = (values.get("LOG_LEVEL") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        database = values.get("DATABASE_PATH", ".runtime/bot.db")
        if not database or not database.strip() or "\x00" in database or database == ":memory:":
            raise ConfigurationError("DATABASE_PATH must be a persistent file path")
        path = Path(database).expanduser()
        if path.is_dir():
            raise ConfigurationError("DATABASE_PATH must be a file path")
        log_file = values.get("LOG_FILE", ".runtime/bot.log")
        if not log_file or not log_file.strip() or "\x00" in log_file:
            raise ConfigurationError("LOG_FILE must be a file path")
        log_path = Path(log_file).expanduser()
        if log_path.is_dir() or log_path.is_symlink() or log_path.resolve() == path.resolve():
            raise ConfigurationError("LOG_FILE must be a separate file path")

        def positive_integer(key: str, default: str, maximum: int) -> int:
            raw = values.get(key, default)
            if not raw or not re.fullmatch(r"[0-9]{1,10}", raw) or not 1 <= int(raw) <= maximum:
                raise ConfigurationError(
                    f"{key} must be a positive integer within its allowed range"
                )
            return int(raw)

        return cls(
            telegram_bot_token=token,
            telegram_allowed_user_ids=allowed_ids,
            transmission_app_path=app_path,
            transmission_rpc_url=url,
            transmission_username=username,
            transmission_password=password,
            transmission_download_dir=values.get("TRANSMISSION_DOWNLOAD_DIR") or None,
            database_path=path,
            log_level=log_level,
            log_file=log_path,
            log_max_bytes=positive_integer("LOG_MAX_BYTES", "5242880", 1073741824),
            log_backup_count=positive_integer("LOG_BACKUP_COUNT", "3", 100),
        )

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.telegram_bot_token,
                self.transmission_username,
                self.transmission_password,
            )
            if value
        )
