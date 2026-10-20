# Telegram Torrent Downloader

Python Telegram bot for searching RuTracker, confirming an exact release, and sending its torrent file to Transmission. See [SPECIFICATION.md](SPECIFICATION.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current status

Steps 1–2 provide project setup, configuration validation, logging, and durable workflow storage. Search, Telegram polling, and Transmission operations will be added in later steps. The default command exits with an explanatory message until polling is implemented.

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

`uv.lock` pins application, development, and transitive dependencies. Commit intentional dependency updates together with `pyproject.toml`. SQLite storage uses Python's standard library and requires no additional service.

Logs go to stderr, redact configured credentials and common authentication headers, and suppress dependency debug logging. Never log request bodies, raw headers, cookies, or settings objects. `.env`, local databases, and temporary torrent files are ignored by Git.

Read [PROGRESS.md](PROGRESS.md) when resuming work. Each implementation step requires user review before its commit.

## Workflow storage

`WorkflowStore(Settings.database_path)` provides durable searches and selections. Call `initialize()` once at startup to create its directory and versioned schema. In async handlers, run store operations with `await asyncio.to_thread(...)`; each operation opens and closes its own connection with a bounded lock timeout. No network operation belongs inside a database transaction.

Searches preserve ordered result snapshots and expire after 15 minutes by default. Select and confirm callbacks must supply the requesting Telegram user and chat. Selecting the same result twice returns the same selection. Only a successful `claim_confirmation()` permits torrent retrieval; repeated or competing claims fail. Cancellation only applies to pending selections. Expiry blocks new claims but permits an already confirmed operation to finish.

The workflow is `pending → claimed → submitting → accepted/duplicate`, with `cancelled`, `failed`, and `uncertain` outcomes. Before calling Transmission, persist the validated torrent hash with `begin_submission()`. Worker transitions require the claim token. Mark an ambiguous RPC outcome uncertain, and never reset it automatically for retry.

`list_incomplete()` exposes claimed, submitting, and uncertain operations to the future recovery coordinator. After previous workers have stopped, claimed work can be failed safely because submission was not started. Submitting and uncertain work require a Transmission lookup by stored hash before resolution. `fail()` requires a known failure or a reconciled absence; failed selections require a fresh search and confirmation. This store does not perform those service calls itself. Schema versions newer than supported are rejected without modification.
