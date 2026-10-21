# Telegram Torrent Downloader

Python Telegram bot for searching RuTracker, confirming an exact release, and sending its torrent file to Transmission. See [SPECIFICATION.md](SPECIFICATION.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Current status

Steps 1–6 provide the Telegram search flow, SQLite credentials and workflow state, RuTracker login/search, torrent validation, and confirmed submission to Transmission. The default command runs long polling. Download explicitly confirms retrieval and submission of that selected torrent; /status checks recent results and unresolved submissions. Operational readiness and final acceptance review remain step 7.

## Setup

Use Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --locked
cp .env.example .env
```

Edit `.env` with your Telegram bot token, your own Telegram user ID and Transmission RPC address. The example values are placeholders. The comma-separated user allowlist is mandatory. Transmission credentials must both be set or both left empty. Keep authentication outside the RPC URL. RuTracker's host is fixed by the specification; it is not a configurable download URL.

Environment variables override `.env`, including empty values. Only `.env` in the current working directory is read; secrets are not interpolated. Run commands from the project root. Relative paths are relative to that directory. `DATABASE_PATH` defaults to `.runtime/bot.db`; normal startup initializes the SQLite store, while configuration validation does not create it. `TRANSMISSION_RPC_URL` defaults to `http://127.0.0.1:9091/transmission/rpc` when omitted; startup recovery and /status may make read-only Transmission lookups; only Download can submit a selected torrent. Leave `TRANSMISSION_DOWNLOAD_DIR` empty to use Transmission's default; a supplied path refers to the Transmission host.

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

RuTracker login and password are stored per Telegram user in SQLite, rather than environment variables. In a private chat, `/start` opens the menu. Choose **log in** or **search** to reuse stored credentials; if none exist, the bot asks for login, then password in separate messages. A rejected pair returns to the login prompt without buttons, repeating until authentication succeeds or you use /cancel. CAPTCHA challenges remain part of authentication. Only a successfully authenticated pair is saved, including when authentication completes after CAPTCHA; interrupted searches and descriptions resume afterward. Old `RUTRACKER_USERNAME` / `RUTRACKER_PASSWORD` environment variables are ignored and should be removed from `.env`.

Credentials are stored as plaintext in the ignored local database, with file permissions restricted to its owner (`0600`). Treat database backups as secrets. Login/password inputs are redacted before logging, and pending credentials are discarded on cancellation, expiry, or restart. Only validated credentials survive restarts; cookies remain in memory.

In this workspace, the installed uv executable is also available as `.tools/bin/uv`. Keep the terminal running, open [@SeedScoutBot](https://t.me/SeedScoutBot), and send `/start` from an allowed account to open the menu. Choose **log in** or **search**; Chrome may then open on the host for browser verification. Stop with Ctrl+C.

Exit codes: `0` for a successful configuration check or clean shutdown, `2` for configuration/argument errors, and `1` for startup/polling failures.

## Install as a macOS login service

Prerequisites: installed **Python 3.14**, **Transmission.app**, **Google Chrome**, and **uv** (the checkout's `.tools/bin/uv` is also accepted). Configure Remote Access in Transmission separately. Run as your normal user from an interactive terminal, with a graphical login session:

```bash
./install.sh
```

The script finds installed Python 3.14, installs the locked runtime dependencies into this checkout's `.venv` using `uv sync --locked --no-dev --no-python-downloads`, and verifies the Transmission bundle. It does not install Python or Transmission automatically. A nonempty configured Transmission app path must be usable; an invalid one is reported without replacing it. If none is configured, the installer checks `/Applications/Transmission.app` and `~/Applications/Transmission.app`.

Choose the service type and enter `TELEGRAM_BOT_TOKEN` and comma-separated `TELEGRAM_ALLOWED_USER_IDS`. Defaults come from exported environment variables first, then this checkout's `.env`. Press **Enter** to keep a default; the current token is offered as a masked default and token entry is hidden. At every installer prompt, **`:restart`** restarts input from the original values and **`:quit`** cancels. Nothing is saved from unfinished input. Review the summary and enter **yes** to save and register the service; Enter at that final prompt cancels. Other `.env` settings, including explicit blanks, remain intact. The saved file is owner-only (`0600`); the installer detects concurrent file edits instead of overwriting them. Dependencies may already have been synchronized before cancellation.

The default, selected for this application, is a **LaunchAgent** at `~/Library/LaunchAgents/org.seedscout.bot.plist`. It starts now and on subsequent graphical logins, running the absolute `.venv/bin/python -u -m tg_torrent_downloader` with this checkout as its working directory. Python, source, `.env`, SQLite state, and logs stay in place; keep the checkout at the same path. The plist contains no bot token. Logs are `.runtime/launchd.stdout.log`, `.runtime/launchd.stderr.log`, and the existing rotating bot log. A manual bot worker must be stopped before installation; an installed service is stopped/reloaded on repeat installation. A bootstrap failure attempts to restore the prior configuration/plist and reload a previously loaded service.

```bash
./install.sh --uninstall          # unload and remove only the installed service
./install.sh --uninstall --agent  # explicitly select the login service
```

Uninstall keeps `.env`, database, logs, source, `.venv`, and Transmission. It does not need uv, runtime dependencies, a valid `.env`, or Transmission; it still needs an installed Python 3.10+ to run the helper. The installer refuses to replace/remove a plist belonging to a different checkout. To change service types, uninstall the previous one first.

`./install.sh --daemon` offers a system LaunchDaemon at `/Library/LaunchDaemons/org.seedscout.bot.plist`, running as your normal user through `UserName`; system registration/removal requests sudo for those operations only. It starts at boot, but cannot access the graphical session required by visible Chrome and automatic GUI app startup. Use the default agent for the current bot. See [Apple's daemon/agent guidance](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/DesigningDaemons.html). `./install.sh --uninstall --daemon` removes only that daemon. `./install.sh --help` shows the options without changing dependencies or services.

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

`WorkflowStore(Settings.database_path)` provides durable searches, selections, and per-user RuTracker credentials. Call `initialize()` once at startup to create its directory and versioned schema (version 3 preserves workflow/credential data and adds a durable confirmed-present outcome). In async handlers, run store operations with `await asyncio.to_thread(...)`; each operation opens and closes its own connection with a bounded lock timeout. No network operation belongs inside a database transaction.

Searches preserve ordered result snapshots and expire after 15 minutes by default. Select and confirm callbacks must supply the requesting Telegram user and chat. Selecting the same result twice returns the same selection. Only a successful `claim_confirmation()` permits torrent retrieval; repeated or competing claims fail. Pending selections can be cancelled. A confirmed request stopped before submission becomes failed; an interrupted submission remains uncertain unless an acknowledgment was already persisted. Expiry blocks new claims but permits an already confirmed operation to finish.

The workflow is `pending → claimed → submitting → accepted/duplicate/found`, with `cancelled`, `failed`, and `uncertain` outcomes. Before calling Transmission, persist the validated torrent hash with `begin_submission()`. Worker transitions require the claim token. Mark an ambiguous RPC outcome uncertain, and never reset it automatically for retry.

`DownloadWorkflow` coordinates `list_incomplete()` recovery. Startup holds an owner-only POSIX `flock` for the configured database path throughout polling and shutdown, rejecting a second bot process before it can recover active work. Old claimed work is failed without retrieval; submitting work becomes uncertain. Hash lookup confirms presence as `found`, while missing/unreachable results remain uncertain. `fail()` is reserved for known failures; immediate absence is not proof that a timed-out request cannot still finish. Failed selections require a fresh search and confirmation. Schema versions newer than supported are rejected without modification.

## RuTracker client

`RuTrackerClient` is an asynchronous context manager bound to one Telegram user/chat workflow. It owns a cookie session and serializes its requests. `ensure_authenticated()` first establishes a browser session through asynchronous Playwright and visible Google Chrome. Chrome loads `/forum/index.php`, then `/forum/login.php`; a matching HTTP 200 and visible `#login-form-full` username/password fields are required. The client imports `cf_clearance`, applicable session cookies, and the exact browser User-Agent, then checks authentication and either completes login or returns a `CaptchaChallenge`. Credentials are posted through the HTTP client using the rendered form and imported session.

The Telegram handlers deliver `challenge.image` and collect an answer. `answer_captcha()` requires the challenge ID and matching user/chat IDs, and returns either successful authentication or a new challenge. Challenges expire after five minutes, can be cancelled, and are consumed once. Cookies, CAPTCHA fields, and images remain in memory; a restart requires fresh authentication. The client does not log credentials, answers, or response bodies.

After authentication, `search(query)` sends the original query as the `nm` search value and requests descending seed-count order (`o=10&s=2`). It parses all distinct topics on the returned page, sorts them locally by seeds descending, then returns the top ten with available size and seed metadata. Ties retain source order; unknown seed counts follow zero. Pagination is deferred. The authenticated live search form was checked: `o=10` selects seed count and `s=2` selects descending; a read-only query returned 50 rows in descending seed order. RuTracker's legacy search encoding is Windows-1251; unsupported characters produce an explicit error rather than a rewritten query. `get_topic(result)` returns the full first-post description plus structured plain/expandable blocks and an optional poster URL. It recognizes ordinary images and RuTracker’s lazy `var.postImg` placeholders, prefers post images over decorative images, and excludes spoiler images from poster selection. Original spoiler headings/content and safe screenshot links are retained; nested spoilers are flattened into the outer expandable block. The parser does not fetch images or torrent bytes. Telegram receives the public poster URL as a photo before the description; no RuTracker cookies/headers are forwarded to image hosts. If photo delivery fails, the description still opens with a poster link. Expandable sections use native `expandable_blockquote` entities, with correct UTF-16 offsets across long messages and Download/Back only on the last message. Screenshot images remain links inside the quotes.

`AuthenticationRequired` tells the conversation to authenticate and resume its saved query/topic. Other explicit errors cover denied access, rate limits, unavailable topics, network failures, and changed markup. Requests have timeouts and response-size limits; redirects remain on HTTPS rutracker.org. Failures do not automatically replay login POSTs. Retry actions preserve the saved query or selected topic; reauthentication can pause for CAPTCHA and then resume the original operation.

`download_torrent(topic_id)` is called by step 6 only after an owned, unexpired Download confirmation. It checks response type and basic torrent framing. The step 5 `validate_torrent()` function provides full v1 metainfo validation and info-hash calculation; the confirmed workflow applies it to downloaded bytes before submission. No live torrent retrieval has been performed.

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

## Local Transmission startup

The local app is `/Applications/Transmission.app`. Set `TRANSMISSION_APP_PATH=/Applications/Transmission.app` in `.env` to enable automatic startup on macOS. Leave it empty or omit it for a remote server or daemon-only setup. `--check-config` validates the setting without opening applications.

Before workflow recovery and Telegram polling, the bot checks whether Transmission is running for the current macOS user. If stopped, it opens the configured app in the background, waits for its process, and tells each allowed Telegram user: "Transmission was not running, so I started it." An already running app is left open without a notification. Launch failures produce a diagnostic notification and the bot continues polling; a failed notification does not prevent other recipients or startup.

Enable Remote Access in Transmission and configure the matching RPC address/authentication separately. Starting the GUI does not guarantee RPC readiness or change its preferences. Startup never retrieves or submits a torrent.

## Transmission client and torrent validation (step 5)

`TransmissionClient.from_settings(settings)` is an asynchronous context manager with `session_info()`, `find_torrent(info_hash)`, `active_downloads()`, and `add_torrent(data, expected_info_hash=...)`. It is independent of Telegram and SQLite; `DownloadWorkflow` connects it to the user-confirmed workflow.

A read-only probe verified the installed Transmission **4.1.3**, RPC version **19**, API **6.0.1**. The client bootstraps with the compatible read-only `session-get` request, then uses JSON-RPC 2.0/snake_case for API 6.x. Legacy RPC versions 14–17 use the older request format. Unsupported versions fail before any add. See the [Transmission 4.1.3 RPC specification](https://github.com/transmission/transmission/blob/4.1.3/docs/rpc-spec.md) and [legacy RPC specification](https://github.com/transmission/transmission/blob/4.0.6/docs/rpc-spec.md).

The adapter negotiates `X-Transmission-Session-Id` with at most three attempts per call, supports optional HTTP Basic authentication, refuses redirects, disables environment proxies and cookie persistence, and bounds request timeouts and response sizes. Request/response IDs must match. The optional download directory refers to the Transmission host and is omitted when unset. The aiohttp minimum is 3.14 for its current authentication-header API; the lock retains version 3.14.5.

`validate_torrent(data)` accepts at most 16 MiB of canonical bencoded **BitTorrent v1** metainfo. It rejects malformed/trailing data, unsorted or duplicate dictionary keys, excessive depth/node counts, inconsistent piece counts, invalid file layouts and unsafe paths. It derives SHA-1 from the exact original `info` bytes, following [BEP 3](https://www.bittorrent.org/beps/bep_0003.html), without rewriting the metainfo. It supports single and multiple files; v2/hybrid and symlink entries are explicitly unsupported. Run validation with `asyncio.to_thread` in async orchestration. Parsed objects exclude raw bytes from their representations.

Before `add_torrent()`, the caller must claim the user-confirmed selection and persist the validated hash with `begin_submission()`. The adapter revalidates the bytes against `expected_info_hash`, submits base64 metainfo (never a file path or URL), and verifies the returned hash, ID, and name. Results distinguish `ADDED`, `DUPLICATE`, and `FOUND`. `FOUND` means a lookup confirmed presence after an ambiguous add; whether that attempt created a new torrent remains unknown.

An add timeout, lost response, malformed result, or unexpected HTTP failure triggers a hash lookup, never an automatic add replay. A match returns `FOUND`; absence or a failed lookup raises `SubmissionUncertain`. An immediate empty lookup does not prove the original request cannot still finish. The same client blocks another add of that hash while unresolved, allowing only reconciliation. The workflow persists uncertainty across restarts, handles cancelled adds conservatively, and blocks the same hash from another submission while any earlier operation is submitting or uncertain. Explicit RPC rejection or denied authentication raises a safe error without add replay.

Diagnostics contain operation names, IDs, HTTP statuses, hash/outcome metadata, and safe error types. They omit authentication headers, session tokens, metainfo, tracker passkeys, download paths, raw RPC errors, and torrent names. Tests use synthetic torrents and an ephemeral local fake daemon. Agent-run live checks used only session information and a missing-hash lookup; no real torrent was downloaded or submitted by those checks. A live end-to-end submission still requires a user-selected test torrent.

## Confirmed submission and recovery (step 6)

Download claims a pending selection before any torrent retrieval. Retrieval may pause for RuTracker login/CAPTCHA; the in-memory claim is scoped to the same user, chat, and exact selected topic and resumes without another confirmation. Abandoning that authentication flow releases it as failed. The validated hash is committed before the add RPC, and the returned Transmission identity is checked against that hash. A transactional hash guard prevents a new search/selection from bypassing an unresolved submission of the same content.

Cancellation waits for in-flight SQLite writes to settle before classifying the operation. A stopped claim becomes failed; an unacknowledged submission becomes uncertain; a persisted success is retained. Startup recovery never fetches torrents or replays an add. Presence resolves an interrupted submission as `found`, without claiming whether it was a newly created or duplicate task. Missing results stay blocked. All metainfo stays in memory; no temporary .torrent files are written.

## Telegram conversation

Cancel buttons are omitted from every screen, including login, CAPTCHA, progress, and error responses. `/cancel` remains available as a text command.

- `/start`: first show **Checking RuTracker login.**, then open **Menu**. Verify an existing authenticated session, or use saved credentials for the existing browser/login flow; resolve any CAPTCHA before returning to menu. Successful authentication hides **log in**, leaving **search** and **downloads**. Missing, rejected, or unverifiable credentials/session leave **log in** visible; downloads remain available. No Transmission RPC request is made by this login check.
- **log in**: run the existing credentials/CAPTCHA flow, save only valid credentials, then return to the menu. **search**: authenticate as needed, then prompt for a query. `/start` returns to the menu from any idle branch, abandoning an unconfirmed selection while preserving a valid client session.
- **downloads**: show a read-only snapshot of running or queued Transmission torrents: queued for verification, verifying, queued to download, downloading, queued to seed, or seeding. Include title, status, percent complete, download/upload speed, and ETA. Hide Transmission IDs; append a clickable G after each title linking to a Google search for that exact title. Completed torrents remain visible while seeding; only stopped torrents are omitted. **reload** fetches a fresh snapshot without leaving this screen. Populated, empty, and unavailable-service screens all have **reload** and **menu** buttons. Long lists are split into messages with the buttons on the last message. RuTracker login is not required. This view covers the configured Transmission instance, including torrents added outside this bot; it is available only to allowed users in private chats. `/status` remains the separate view of bot-owned confirmation history.
- `/search` or New search: begin another search using the existing client session when valid.
- Send a query unchanged, then choose a numbered button from the first ten results (or type a valid displayed number). Each result is sent as its own message. A final separate message says "Choose a numbered button or send a new search query." and contains only numeric buttons, up to five per row, matching the number of available torrents. Each result includes available size/seed count, without its topic link. Any other nonempty text, including an out-of-range number, starts a new search unchanged. /cancel remains available.
- A selection displays the page’s poster image first (when available), followed by the full topic description with only Download and Back buttons. RuTracker collapsible sections such as Наличие серий в раздаче, Media info, and Скриншоты become expandable Telegram quotes retaining their headings and content; screenshot images remain links within those text quotes. Every nonempty text input on this screen starts a new search unchanged, including valid result numbers and unknown slash-prefixed strings; recognized commands retain their behavior. Back restores the same search results and numbering. Download atomically claims that exact selection, retrieves its torrent, validates/persists the hash, and queues it in Transmission. The submission response is one short verdict message, without a separate processing message or repeated title/name/ID. It offers **search results** (restore the existing list) and **downloads** (open the active Transmission list). Accepted, duplicate, confirmed presence, rejection, and uncertain outcomes remain distinct; /status retains detailed confirmation history.
- `/status` works without /start, checks only the requesting user/chat, and reports all unresolved operations plus the ten latest terminal results. It only performs lookups and never retrieves or submits torrent files. An offline service stops further lookup attempts in that status pass.
- `/help` shows instructions. `/cancel` stops this bot operation and closes its sessions. Before submission it prevents the add; after submission may have begun, it retains uncertainty or the already acknowledged result. It does not remove queued torrents or stop their content download in Transmission.

Access control runs before upstream operations. Conversation state and controls are bound to the requesting user/chat; each screen has a fresh callback token. Old buttons, wrong-owner controls, and callbacks from before a restart are rejected. Chats are serialized independently, with a responsive cancellation path during browser/network work. Confirmed metainfo workflows share an async lock; waiting tasks remain cancellable and ordinary searches remain responsive.

Search snapshots and selections persist in SQLite. The active UI and browser/CAPTCHA session stay in memory and expire after 15 minutes of inactivity; idle sessions are cleaned periodically. Search selections also retain their original database expiry. After a restart, use `/start` for the menu or `/status` for prior confirmed operations; stale buttons never silently resume a download.

Automated conversation tests use fake upstream services and an actual aiogram router dispatch check. A read-only Telegram getMe check verified the saved token belongs to @SeedScoutBot. A user-requested live Telegram run exposed an authentication-marker bug after /start. The bot was stopped, and the parser was fixed to recognize RuTracker's nonempty #logged-in-username marker. Live backend login/search/topic checks now pass; the user subsequently reported the corrected search flow working. Full live download/submission verification remains pending. Consult PROGRESS.md for the latest local runtime status.

When RuTracker returns a search table with no recognizable releases, the error response has no inline buttons. The bot returns to query input: any nonempty text, including a number, becomes a fresh unchanged search string. Old controls are invalidated; commands remain available.

CAPTCHA images may be hosted at `https://static.rutracker.cc/captcha/`. The client permits that exact CDN/path only for image GETs, using a separate cookie-free HTTP session and no forwarded authentication headers. Login, search, topic, and torrent requests remain constrained to rutracker.org.
