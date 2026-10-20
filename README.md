# Telegram Torrent Downloader

Python Telegram bot for searching RuTracker, confirming an exact release, and sending its torrent file to Transmission. See [SPECIFICATION.md](SPECIFICATION.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current status

Step 1 provides project setup, configuration validation, and logging. Search, Telegram polling, and Transmission operations will be added in later steps. The default command exits with an explanatory message until polling is implemented.

## Setup

Use Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --locked
cp .env.example .env
```

Edit `.env` with your Telegram bot token, your own Telegram user ID, RuTracker login, and Transmission RPC address. The example values are placeholders. The comma-separated user allowlist is mandatory. Transmission credentials must both be set or both left empty. Keep authentication outside the RPC URL. RuTracker's host is fixed by the specification; it is not a configurable download URL.

Environment variables override `.env`, including empty values. Only `.env` in the current working directory is read; secrets are not interpolated. Run commands from the project root. Relative paths are relative to that directory. `DATABASE_PATH` defaults to `.runtime/bot.db` for the future SQLite store; validation does not create it. Leave `TRANSMISSION_DOWNLOAD_DIR` empty to use Transmission's default; a supplied path refers to the Transmission host.

Validate configuration without contacting any service:

```bash
uv run --locked tg-torrent-downloader --check-config
# Equivalent module entry point:
uv run --locked python -m tg_torrent_downloader --check-config
```

Exit codes: `0` for a successful configuration check, `2` for configuration/argument errors, `1` when attempting to run the not-yet-implemented bot.

## Development

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`uv.lock` pins application, development, and transitive dependencies. Commit intentional dependency updates together with `pyproject.toml`. SQLite uses Python's standard library; persistent workflow storage is step 2.

Logs go to stderr, redact configured credentials and common authentication headers, and suppress dependency debug logging. Never log request bodies, raw headers, cookies, or settings objects. `.env`, local databases, and temporary torrent files are ignored by Git.

Read [PROGRESS.md](PROGRESS.md) when resuming work. Each implementation step requires user review before its commit.
