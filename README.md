# Telegram Torrent Downloader

Python Telegram bot for searching RuTracker, confirming an exact release, and sending its torrent file to Transmission. See [SPECIFICATION.md](SPECIFICATION.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current status

Steps 1–3 provide project setup, configuration validation, logging, durable workflow storage, and the RuTracker client. Telegram polling and Transmission operations will be added in later steps. The default command exits with an explanatory message until polling is implemented.

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

Logs go to stderr and a rotating UTF-8 diagnostic file (default `.runtime/bot.log`). HTTP headers and POST fields are sanitized before logging; configured credentials are also redacted during formatting. Dependency debug logging is suppressed. Never log raw headers, cookies, response bodies, or settings objects. `.env`, local databases, log files and their rotated backups, and temporary torrent files are ignored by Git.

Read [PROGRESS.md](PROGRESS.md) when resuming work. Each implementation step requires user review before its commit.

## Workflow storage

`WorkflowStore(Settings.database_path)` provides durable searches and selections. Call `initialize()` once at startup to create its directory and versioned schema. In async handlers, run store operations with `await asyncio.to_thread(...)`; each operation opens and closes its own connection with a bounded lock timeout. No network operation belongs inside a database transaction.

Searches preserve ordered result snapshots and expire after 15 minutes by default. Select and confirm callbacks must supply the requesting Telegram user and chat. Selecting the same result twice returns the same selection. Only a successful `claim_confirmation()` permits torrent retrieval; repeated or competing claims fail. Cancellation only applies to pending selections. Expiry blocks new claims but permits an already confirmed operation to finish.

The workflow is `pending → claimed → submitting → accepted/duplicate`, with `cancelled`, `failed`, and `uncertain` outcomes. Before calling Transmission, persist the validated torrent hash with `begin_submission()`. Worker transitions require the claim token. Mark an ambiguous RPC outcome uncertain, and never reset it automatically for retry.

`list_incomplete()` exposes claimed, submitting, and uncertain operations to the future recovery coordinator. After previous workers have stopped, claimed work can be failed safely because submission was not started. Submitting and uncertain work require a Transmission lookup by stored hash before resolution. `fail()` requires a known failure or a reconciled absence; failed selections require a fresh search and confirmation. This store does not perform those service calls itself. Schema versions newer than supported are rejected without modification.

## RuTracker client

`RuTrackerClient` is an asynchronous context manager bound to one Telegram user/chat workflow. It owns a cookie session and serializes its requests. `ensure_authenticated()` reads the homepage form and either completes login or returns a `CaptchaChallenge`. It never fetches the dedicated login page; credentials are posted to `/forum/login.php`.

The future Telegram handlers deliver `challenge.image` and collect an answer. `answer_captcha()` requires the challenge ID and matching user/chat IDs, and returns either successful authentication or a new challenge. Challenges expire after five minutes, can be cancelled, and are consumed once. Cookies, CAPTCHA fields, and images remain in memory; a restart requires fresh authentication. The client does not log credentials, answers, or response bodies.

After authentication, `search(query)` sends the original query as the `nm` search value and returns up to ten distinct topics in source order, with available size and seed metadata. RuTracker's legacy search encoding is Windows-1251; unsupported characters produce an explicit error rather than a rewritten query. `get_topic(result)` returns the full first-post description as plain text, including spoiler contents and useful links/image URLs, without fetching images or torrent bytes. Telegram splitting and rendering belong to step 5.

`AuthenticationRequired` tells the conversation to authenticate and resume its saved query/topic. Other explicit errors cover denied access, rate limits, unavailable topics, network failures, and changed markup. Requests have timeouts and response-size limits; redirects remain on HTTPS rutracker.org. Failures do not automatically replay login POSTs. The user-facing Retry actions are wired in step 5.

`download_torrent(topic_id)` is a separate operation reserved for step 6 after explicit Download confirmation. It checks response type and basic torrent framing; full bencode validation and info-hash calculation belong to step 4. No live torrent retrieval has been performed.

Live verification: homepage and `/forum/index.php` return HTTP 200 with the expected login form and Windows-1251 encoding. The login POST returns HTTP 403 in this environment, so successful authentication, actual CAPTCHA markup, and authenticated search/topic parsing still need live verification. Fixtures are synthetic examples, explicitly labeled in `tests/fixtures/rutracker/README.md`; passing them does not establish live compatibility.

## Diagnostic logging

Configure `LOG_FILE` (default `.runtime/bot.log`), `LOG_MAX_BYTES` (default `5242880`, 5 MiB), `LOG_BACKUP_COUNT` (default `3`), and `LOG_LEVEL` (default `INFO`). Parent directories are created on normal startup. The current file and up to three backups (`bot.log.1`, etc.) use owner-only permissions. Rotation occurs at the size threshold; a single oversized event can exceed it. Use a `.log` filename or an ignored runtime directory when choosing another path.

Events have timestamps and JSON payloads. RuTracker diagnostics include user/chat IDs, a request ID and redirect hop, method, sanitized URL and POST fields, actual outgoing headers, and response status/headers, including redirects and denied responses. Header names and duplicate headers are retained; authentication and unknown header values are redacted. Common diagnostic headers such as Content-Type, Server, Date, and User-Agent remain readable. Only recognized nonsensitive form/query fields are preserved; passwords, cookies, session/challenge tokens, CAPTCHA answers, and unknown field values are redacted. Nonstandard URL paths are redacted because they may contain tokens. Response bodies and torrent bytes are never logged.

Brief state events cover authentication, CAPTCHA, search, description retrieval, and torrent retrieval. SQLite selection/submission transitions are logged after commit with user/chat, selection ID, topic ID, and status, without worker claim tokens. Search queries and redacted CAPTCHA inputs are instrumented now; `log_user_input()` is ready for Telegram command, numeric input, and button handlers in step 5. Those handlers do not exist yet.

`--check-config` remains an offline, side-effect-free validation command: it does not open a log file or verify filesystem write access. Normal startup reports a safe configuration error if the file cannot be opened. Later write/rotation failures report a fixed stderr message without dumping raw log records. File rotation assumes a single bot process writes the configured file.
