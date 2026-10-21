# Telegram Torrent Downloader

Python Telegram bot for searching RuTracker, confirming an exact release, and sending its torrent file to Transmission. See [SPECIFICATION.md](SPECIFICATION.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current status

Steps 1–4 provide project setup, diagnostic logging, durable workflow storage, the RuTracker client, and the Telegram search conversation. The default command runs long polling. Step 5 adds a standalone Transmission client and torrent validator. Confirmed torrent submission remains step 6; Download currently explains that downloads are not enabled and does not retrieve torrent files.

## Setup

Use Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --locked
cp .env.example .env
```

Edit `.env` with your Telegram bot token, your own Telegram user ID and Transmission RPC address. The example values are placeholders. The comma-separated user allowlist is mandatory. Transmission credentials must both be set or both left empty. Keep authentication outside the RPC URL. RuTracker's host is fixed by the specification; it is not a configurable download URL.

Environment variables override `.env`, including empty values. Only `.env` in the current working directory is read; secrets are not interpolated. Run commands from the project root. Relative paths are relative to that directory. `DATABASE_PATH` defaults to `.runtime/bot.db`; normal startup initializes the SQLite store, while configuration validation does not create it. `TRANSMISSION_RPC_URL` defaults to `http://127.0.0.1:9091/transmission/rpc` when omitted; step 4 never contacts Transmission. Leave `TRANSMISSION_DOWNLOAD_DIR` empty to use Transmission's default; a supplied path refers to the Transmission host.

Validate configuration without contacting any service:

```bash
uv run --locked tg-torrent-downloader --check-config
# Equivalent module entry point:
uv run --locked python -m tg_torrent_downloader --check-config
```

Start the bot from the project root:

```bash
uv run --locked tg-torrent-downloader
```

RuTracker login and password are stored per Telegram user in SQLite, rather than environment variables. In a private chat, `/start` reuses stored credentials; if none exist, the bot asks for login, then password in separate messages. A rejected pair returns to the login prompt with Cancel only, repeating until authentication succeeds or you cancel. CAPTCHA challenges remain part of authentication. Only a successfully authenticated pair is saved, including when authentication completes after CAPTCHA; interrupted searches and descriptions resume afterward. Old `RUTRACKER_USERNAME` / `RUTRACKER_PASSWORD` environment variables are ignored and should be removed from `.env`.

Credentials are stored as plaintext in the ignored local database, with file permissions restricted to its owner (`0600`). Treat database backups as secrets. Login/password inputs are redacted before logging, and pending credentials are discarded on cancellation, expiry, or restart. Only validated credentials survive restarts; cookies remain in memory.

In this workspace, the installed uv executable is also available as `.tools/bin/uv`. Keep the terminal running, open [@SeedScoutBot](https://t.me/SeedScoutBot), and send `/start` from an allowed account. Chrome may open on the host for browser verification. Stop with Ctrl+C.

Exit codes: `0` for a successful configuration check or clean shutdown, `2` for configuration/argument errors, and `1` for startup/polling failures.

## Development

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`uv.lock` pins application, development, and transitive dependencies. Commit intentional dependency updates together with `pyproject.toml`. SQLite storage uses Python's standard library and requires no additional service.

Logs go to stderr and a rotating UTF-8 diagnostic file (default `.runtime/bot.log`). HTTP headers and POST fields are sanitized before logging; configured secrets and credentials loaded or entered at runtime are also redacted during formatting. Dependency debug logging is suppressed. Never log raw headers, cookies, response bodies, or settings objects. `.env`, local databases, log files and their rotated backups, and temporary torrent files are ignored by Git.

Read [PROGRESS.md](PROGRESS.md) when resuming work. Each implementation step requires user review before its commit.

## Workflow storage

`WorkflowStore(Settings.database_path)` provides durable searches, selections, and per-user RuTracker credentials. Call `initialize()` once at startup to create its directory and versioned schema (version 2 migrates existing workflow data without losing it). In async handlers, run store operations with `await asyncio.to_thread(...)`; each operation opens and closes its own connection with a bounded lock timeout. No network operation belongs inside a database transaction.

Searches preserve ordered result snapshots and expire after 15 minutes by default. Select and confirm callbacks must supply the requesting Telegram user and chat. Selecting the same result twice returns the same selection. Only a successful `claim_confirmation()` permits torrent retrieval; repeated or competing claims fail. Cancellation only applies to pending selections. Expiry blocks new claims but permits an already confirmed operation to finish.

The workflow is `pending → claimed → submitting → accepted/duplicate`, with `cancelled`, `failed`, and `uncertain` outcomes. Before calling Transmission, persist the validated torrent hash with `begin_submission()`. Worker transitions require the claim token. Mark an ambiguous RPC outcome uncertain, and never reset it automatically for retry.

`list_incomplete()` exposes claimed, submitting, and uncertain operations to the future recovery coordinator. After previous workers have stopped, claimed work can be failed safely because submission was not started. Submitting and uncertain work require a Transmission lookup by stored hash before resolution. `fail()` requires a known failure or a reconciled absence; failed selections require a fresh search and confirmation. This store does not perform those service calls itself. Schema versions newer than supported are rejected without modification.

## RuTracker client

`RuTrackerClient` is an asynchronous context manager bound to one Telegram user/chat workflow. It owns a cookie session and serializes its requests. `ensure_authenticated()` first establishes a browser session through asynchronous Playwright and visible Google Chrome. Chrome loads `/forum/index.php`, then `/forum/login.php`; a matching HTTP 200 and visible `#login-form-full` username/password fields are required. The client imports `cf_clearance`, applicable session cookies, and the exact browser User-Agent, then checks authentication and either completes login or returns a `CaptchaChallenge`. Credentials are posted through the HTTP client using the rendered form and imported session.

The Telegram handlers deliver `challenge.image` and collect an answer. `answer_captcha()` requires the challenge ID and matching user/chat IDs, and returns either successful authentication or a new challenge. Challenges expire after five minutes, can be cancelled, and are consumed once. Cookies, CAPTCHA fields, and images remain in memory; a restart requires fresh authentication. The client does not log credentials, answers, or response bodies.

After authentication, `search(query)` sends the original query as the `nm` search value and requests descending seed-count order (`o=10&s=2`). It parses all distinct topics on the returned page, sorts them locally by seeds descending, then returns the top ten with available size and seed metadata. Ties retain source order; unknown seed counts follow zero. Pagination is deferred. The authenticated live search form was checked: `o=10` selects seed count and `s=2` selects descending; a read-only query returned 50 rows in descending seed order. RuTracker's legacy search encoding is Windows-1251; unsupported characters produce an explicit error rather than a rewritten query. `get_topic(result)` returns the full first-post description as plain text, including spoiler contents and useful links/image URLs, without fetching images or torrent bytes. Telegram handlers split the complete description into safe plain-text messages within the UTF-16 length limit, with buttons only on the final message.

`AuthenticationRequired` tells the conversation to authenticate and resume its saved query/topic. Other explicit errors cover denied access, rate limits, unavailable topics, network failures, and changed markup. Requests have timeouts and response-size limits; redirects remain on HTTPS rutracker.org. Failures do not automatically replay login POSTs. Retry actions preserve the saved query or selected topic; reauthentication can pause for CAPTCHA and then resume the original operation.

`download_torrent(topic_id)` is a separate operation reserved for step 6 after explicit Download confirmation. It checks response type and basic torrent framing. The step 5 `validate_torrent()` function provides full v1 metainfo validation and info-hash calculation; step 6 will apply it to downloaded bytes before submission. No live torrent retrieval has been performed.

Live verification: a visible-Chrome run obtained `cf_clearance`, verified the full login form, and successfully reused the cookies/User-Agent to fetch `/forum/login.php` through aiohttp (HTTP 200). That browser-transfer check did not submit credentials. A later login investigation verified real credential login, a repeat authenticated-session check, ten search results for a read-only Ubuntu query, and full topic-description extraction. Live CAPTCHA image acquisition has also been verified against static.rutracker.cc; answer submission and the full Telegram CAPTCHA round trip still need live verification. Earlier direct HTTP login attempts without browser clearance received HTTP 403. Fixtures are synthetic examples, explicitly labeled in `tests/fixtures/rutracker/README.md`; passing them does not establish live compatibility.

## Diagnostic logging

Configure `LOG_FILE` (default `.runtime/bot.log`), `LOG_MAX_BYTES` (default `5242880`, 5 MiB), `LOG_BACKUP_COUNT` (default `3`), and `LOG_LEVEL` (default `INFO`). Parent directories are created on normal startup. The current file and up to three backups (`bot.log.1`, etc.) use owner-only permissions. Rotation occurs at the size threshold; a single oversized event can exceed it. Use a `.log` filename or an ignored runtime directory when choosing another path.

Events have timestamps and JSON payloads. RuTracker diagnostics include user/chat IDs, a request ID and redirect hop, method, sanitized URL and POST fields, actual outgoing headers, and response status/headers, including redirects and denied responses. Header names and duplicate headers are retained; authentication and unknown header values are redacted. Common diagnostic headers such as Content-Type, Server, Date, and User-Agent remain readable. Only recognized nonsensitive form/query fields are preserved; passwords, cookies, session/challenge tokens, CAPTCHA answers, and unknown field values are redacted. Nonstandard URL paths are redacted because they may contain tokens. Response bodies and torrent bytes are never logged.

Brief state events cover authentication, CAPTCHA, search, description retrieval, and torrent retrieval. SQLite selection/submission transitions are logged after commit with user/chat, selection ID, topic ID, and status, without worker claim tokens. Telegram commands, queries, result numbers, and button actions are logged. CAPTCHA answers are redacted before log records are created, and HTTP diagnostics retain their existing credential/session redaction.

`--check-config` remains an offline, side-effect-free validation command: it does not open a log file or verify filesystem write access. Normal startup reports a safe configuration error if the file cannot be opened. Later write/rotation failures report a fixed stderr message without dumping raw log records. File rotation assumes a single bot process writes the configured file.

## Browser session prerequisite

Install Google Chrome on the machine that runs the bot and provide an active graphical desktop. `uv sync --locked` installs the Python Playwright dependency; this implementation uses the installed `chrome` channel, so it does not download another browser. Initial or renewed session acquisition opens a visible window and may need local browser verification. This prerequisite must be considered for eventual server deployment.

Cookies retain their domain/path, secure/HttpOnly flags, and expiry in the HTTP cookie jar. The same browser User-Agent is used for subsequent requests. Cookies, rendered login HTML, and CAPTCHA state remain in memory, isolated per user/chat client. Browser contexts close after capture, and HTTP sessions close on client exit. Missing/expired clearance or a later access denial requires fresh browser verification; failed POSTs are not replayed automatically. This replaces the earlier homepage-only login approach.

No Netscape cookie jar, User-Agent export, or rendered HTML file is written by default. The example artifact names `.rutracker-cookies.txt`, `.rutracker-user-agent.txt`, and `rutracker_login.html` are ignored as a precaution if used during manual diagnostics. Browser document request/response headers use the same sanitization as other diagnostics; session values and HTML are never logged.

## Transmission client and torrent validation (step 5)

`TransmissionClient.from_settings(settings)` is an asynchronous context manager with `session_info()`, `find_torrent(info_hash)`, and `add_torrent(data, expected_info_hash=...)`. It is independent of Telegram and SQLite; step 6 will connect it to the confirmed workflow. The current Download button remains a placeholder.

A read-only probe verified the installed Transmission **4.1.3**, RPC version **19**, API **6.0.1**. The client bootstraps with the compatible read-only `session-get` request, then uses JSON-RPC 2.0/snake_case for API 6.x. Legacy RPC versions 14–17 use the older request format. Unsupported versions fail before any add. See the [Transmission 4.1.3 RPC specification](https://github.com/transmission/transmission/blob/4.1.3/docs/rpc-spec.md) and [legacy RPC specification](https://github.com/transmission/transmission/blob/4.0.6/docs/rpc-spec.md).

The adapter negotiates `X-Transmission-Session-Id` with at most three attempts per call, supports optional HTTP Basic authentication, refuses redirects, disables environment proxies and cookie persistence, and bounds request timeouts and response sizes. Request/response IDs must match. The optional download directory refers to the Transmission host and is omitted when unset. The aiohttp minimum is 3.14 for its current authentication-header API; the lock retains version 3.14.5.

`validate_torrent(data)` accepts at most 16 MiB of canonical bencoded **BitTorrent v1** metainfo. It rejects malformed/trailing data, unsorted or duplicate dictionary keys, excessive depth/node counts, inconsistent piece counts, invalid file layouts and unsafe paths. It derives SHA-1 from the exact original `info` bytes, following [BEP 3](https://www.bittorrent.org/beps/bep_0003.html), without rewriting the metainfo. It supports single and multiple files; v2/hybrid and symlink entries are explicitly unsupported. Run validation with `asyncio.to_thread` in async orchestration. Parsed objects exclude raw bytes from their representations.

Before `add_torrent()`, the caller must claim the user-confirmed selection and persist the validated hash with `begin_submission()`. The adapter revalidates the bytes against `expected_info_hash`, submits base64 metainfo (never a file path or URL), and verifies the returned hash, ID, and name. Results distinguish `ADDED`, `DUPLICATE`, and `FOUND`. `FOUND` means a lookup confirmed presence after an ambiguous add; whether that attempt created a new torrent remains unknown.

An add timeout, lost response, malformed result, or unexpected HTTP failure triggers a hash lookup, never an automatic add replay. A match returns `FOUND`; absence or a failed lookup raises `SubmissionUncertain`. An immediate empty lookup does not prove the original request cannot still finish. The same client blocks another add of that hash while unresolved, allowing only reconciliation. Step 6 must persist uncertainty across restarts, handle cancelled adds conservatively, and reconcile before considering another confirmed submission. Explicit RPC rejection or denied authentication raises a safe error without add replay.

Diagnostics contain operation names, IDs, HTTP statuses, hash/outcome metadata, and safe error types. They omit authentication headers, session tokens, metainfo, tracker passkeys, download paths, raw RPC errors, and torrent names. Tests use synthetic torrents and an ephemeral local fake daemon. Live checks used only session information and a missing-hash lookup; no real torrent was downloaded or submitted.

## Telegram conversation

- `/start`: authenticate first, solve any CAPTCHA, then enter a query.
- `/search` or New search: begin another search using the existing client session when valid.
- Send a query unchanged, then choose a numbered button from the first ten results (or type a valid displayed number). Results show only numeric buttons, up to five per row, matching the number of available torrents. Each result includes available size/seed count, without its topic link. Any other nonempty text, including an out-of-range number, starts a new search unchanged. /cancel remains available.
- A selection retrieves the full topic description with only Download and Back buttons. Every nonempty text input on this screen starts a new search unchanged, including valid result numbers and unknown slash-prefixed strings; recognized commands retain their behavior. Back restores the same search results and numbering. Download is displayed but remains a placeholder until step 6, without claiming or downloading the torrent.
- `/help` shows instructions. `/cancel` or Cancel interrupts active work, closes the client session, and cancels a pending selection when possible.

Access control runs before upstream operations. Conversation state and controls are bound to the requesting user/chat; each screen has a fresh callback token. Old buttons, wrong-owner controls, and callbacks from before a restart are rejected. Chats are serialized independently, with a responsive cancellation path during browser/network work.

Search snapshots and selections persist in SQLite. The active UI and browser/CAPTCHA session stay in memory and expire after 15 minutes of inactivity; idle sessions are cleaned periodically. Search selections also retain their original database expiry. After a restart, use `/start`; stale buttons never silently resume a download.

Automated conversation tests use fake upstream services and an actual aiogram router dispatch check. A read-only Telegram getMe check verified the saved token belongs to @SeedScoutBot. A user-requested live Telegram run exposed an authentication-marker bug after /start. The bot was stopped, and the parser was fixed to recognize RuTracker's nonempty #logged-in-username marker. Live backend login/search/topic checks now pass; the fixed Telegram conversation and real CAPTCHA delivery still need a walkthrough. Polling remains stopped until explicitly started again.

When RuTracker returns a search table with no recognizable releases, the error response has no inline buttons. The bot returns to query input: any nonempty text, including a number, becomes a fresh unchanged search string. Old controls are invalidated; commands remain available.

CAPTCHA images may be hosted at `https://static.rutracker.cc/captcha/`. The client permits that exact CDN/path only for image GETs, using a separate cookie-free HTTP session and no forwarded authentication headers. Login, search, topic, and torrent requests remain constrained to rutracker.org.
