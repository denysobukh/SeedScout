# Telegram Torrent Downloader — Application Specification

## Purpose

Build a Telegram bot that finds torrents on rutracker.org for content requested by a user, presents available results, and sends the exact torrent selected and confirmed by the user to Transmission.

## Core workflow

1. `/start` first shows "Checking RuTracker login.", verifies an existing session or authenticates using saved credentials (including any CAPTCHA), and then opens the Menu screen: **log in** (only without a verified RuTracker session), **search**, and **downloads**. Log in runs the existing credential/CAPTCHA branch and returns to menu. Search checks its RuTracker session, authenticates if necessary, and asks the requesting user to solve any CAPTCHA before prompting for a query.
2. The bot uses the user's query unchanged to search rutracker.org and requests descending seed-count order, sorts all distinct returned topics by seed count descending before applying the top-10 limit, and shows those results, numbered 1–10 (or fewer when fewer matches exist).
3. Each result includes its title and available metadata useful for choosing a release, such as size, seed count, quality, language, and format. Missing metadata is omitted rather than invented.
4. The user picks a numbered result button (or types a valid displayed result number). The bot opens its stored topic link, displays its poster first when available, and shows the full release description, split across Telegram messages as needed. RuTracker collapsible sections become native expandable Telegram quotes, with Download and Back actions on the final message. Download is explicit confirmation; Back restores the original results and numbering.
5. Only after confirmation, the bot downloads the selected topic's `.torrent` file from rutracker.org.
6. The bot submits that torrent file to Transmission through its RPC interface.
7. After confirmation, the bot sends one concise submission verdict with search results and downloads buttons; omit a separate processing message and repeated title/name/identifier. Report acceptance, rejection, duplicate, confirmed presence, or uncertainty honestly; /status retains detailed names/identifiers. Acceptance means the download was queued; it does not mean the content finished downloading.

See [RUTRACKER_FLOW.md](RUTRACKER_FLOW.md) for the confirmed decision tree and retry destinations.

## Functional requirements

### Telegram interaction

- Do not show Cancel buttons anywhere in the conversation. Keep `/cancel` as a text command with existing cancellation behavior.
- Provide `/start` for the Menu screen, `/help` for instructions, and `/status` for owned recent/unresolved confirmations. Immediately after /start show "Checking RuTracker login." before browser/network authentication work. Verify an existing authenticated session or authenticate using saved credentials through the existing login/CAPTCHA branch, then show menu without log in when successful. Missing credentials and rejected/unverifiable startup login return to menu with log in visible; forget a rejected saved pair. A startup CAPTCHA resumes menu after success. Stored credentials alone do not imply authentication. Ordinary menu returns recheck an existing authenticated session and show log in again on expiry or verification failure. Preserve a valid client when navigating through menu/downloads. Search retains its existing authentication/query behavior.
- The downloads branch displays a read-only snapshot of all running or queued torrents in the configured Transmission instance (including torrents added elsewhere), without requiring RuTracker login. Active means queued for verification, verifying, queued for download, downloading, queued to seed, or seeding (statuses 1–6), regardless of completion percentage or current transfer rate. Omit stopped torrents (status 0). Show title, status, progress, download/upload speed, and ETA. Hide Transmission IDs and append a clickable G after each title, linking to a Google search with that exact title. Preserve literal title text and safe link entities across message splitting. Split long lists safely. Offer reload and menu buttons on successful, empty, and unavailable-service responses. Reload performs a fresh read-only query and invalidates old controls without requiring a return to menu. No listing action may retrieve, add, start, stop, or remove torrents. Allowlist/private-chat and stale/foreign-control restrictions apply to this branch.
- `/start` returns to menu from an idle branch, cancels an abandoned pending selection or unsubmitted claim, and invalidates earlier controls. Login selected from menu returns to menu; search-interrupted login resumes that search.
- Accept nonempty text queries and provide clear feedback for empty or unsupported input.
- Request server-side seed sorting (`o=10&s=2`) and sort parsed results locally before taking ten. Keep source order for equal seed counts and put unknown counts after zero. Pagination remains deferred; local ranking covers the returned search page.
- Omit RuTracker topic URLs from the search result list; show numbered titles, size, and seed count when available.
- Send each numbered search result as a separate message with its title and available metadata. Follow the results with a separate final message: "Choose a numbered button or send a new search query." Attach all numbered selection buttons to that final message only. Keep safe splitting for an individual result exceeding Telegram’s message limit.
- Show one numbered button per displayed result (1–10, or fewer), replacing New search and Cancel buttons on the results list. Arrange up to five numeric buttons per row.
- Accept a numbered button or a typed valid displayed result number as selection input. Any other nonempty text on the results screen starts a new search using the original text unchanged, including out-of-range numbers. On the description screen, every nonempty text input, including valid result numbers and unrecognized slash-prefixed strings, starts a new search unchanged. Recognized commands and CAPTCHA answers retain their dedicated behavior.
- Show an available release poster as a Telegram photo before the description. Extract ordinary or lazy post-image URLs from the first post, preferring post images over decorative icons and excluding screenshot/spoiler images as poster candidates. Keep the description usable when no poster exists or Telegram cannot retrieve it; fall back to a safe poster link on photo failure.
- Preserve RuTracker spoiler sections (including Наличие серий в раздаче, Media info, Скриншоты and other headings) as expandable_blockquote entities with headings/content retained. Flatten nested spoilers into their outer quote because quote entities cannot nest. Keep screenshot links within the quote; do not try to embed image media inside a text quote. Preserve safe literal text and correct UTF-16 entity offsets across message splits. Fetching a topic/poster never retrieves or submits a torrent, and no RuTracker cookies/credentials are passed to image hosts.
- Provide only Download and Back buttons on the description; omit New search and Cancel buttons. /start, /search, /status, /help, and /cancel remain commands. Initial results are limited to the top 10; pagination is deferred.
- Keep search results and pending selections scoped to the requesting user and chat.
- Prevent another user from confirming someone else's selection. If Telegram reports an expired callback acknowledgement, abort the action before any claim, retrieval, or submission.
- Expire stale selections and explain how to search again.
- Submission response UI: use one final verdict message (no processing notice), with only search results and downloads buttons. Search results restores the saved list without another query; downloads opens the existing active-torrent branch. Never label an uncertain RPC outcome as rejection or success. Authentication/CAPTCHA prompts may still interrupt retrieval when required.
- Do not download a torrent file or submit it to Transmission before confirmation.

### RuTracker integration

- Search rutracker.org and retrieve topic metadata and torrent files.
- Before the first HTTP authentication check, use asynchronous Playwright with visible Google Chrome to visit `/forum/index.php`, let its JavaScript establish a session, then open `/forum/login.php`. Wait for visible username/password fields in `#login-form-full`, require a matching main-frame HTTP 200 response, and verify the final login URL.
- Capture `cf_clearance`, other applicable RuTracker session cookies, and the browser's actual User-Agent. Reuse them together in the HTTP client, preserving cookie domain/path, secure/HttpOnly flags, and expiry. Keep browser contexts isolated per user/chat workflow.
- Check authentication before asking for a query. If login is needed, use the rendered login form (`login_username`, `login_password`) and POST the user’s credentials to `/forum/login.php` with the imported browser session.
- Reuse the session for later search, topic, and torrent requests. If access is denied, invalidate the browser session and require fresh browser verification on retry; never automatically replay a login POST. Keep cookies and rendered HTML in memory, out of logs and Git. Restart requires fresh verification.
- Store validated RuTracker login/password pairs in SQLite, scoped to the allowed Telegram user. Reuse them after restarts. If credentials are absent, prompt for login and password in two separate private-chat steps before attempting authentication.
- Invalid credentials → remove that rejected saved pair, show the failure and login prompt without buttons (no New search or automatic Retry), then collect password and attempt authentication again. Repeat until authenticated or cancelled. Keep interrupted search/description state for resumption.
- Keep candidate credentials in memory until successful authentication, including any CAPTCHA challenge. Never save rejected or unverified pairs. Redact login/password inputs before logging and protect the local credential database with owner-only file permissions.
- When login requires CAPTCHA, deliver its image to the requesting user, submit their answer, and repeat with a refreshed image when necessary. Allow CAPTCHA image GET requests only from the legacy RuTracker CAPTCHA endpoint or HTTPS static.rutracker.cc/captcha/; use a separate cookie-free session for the CDN and never forward login headers, credentials, or session cookies to it. Scope challenges and answers to that user and chat, expire challenges, and support cancellation.
- Preserve queries and selected topics across session reauthentication. Retrieve the full release description only after selection; opening a description must not download a torrent file.
- Preserve the association between each displayed result, its topic, and the downloaded torrent file.
- Handle no results, authentication failures, unavailable topics, network errors, and unexpected response formats with actionable messages.
- If a search table contains no recognizable releases, show the diagnostic message without inline buttons and return to query input. Treat every subsequent nonempty text message, including numbers, as a new search string unchanged; commands retain their dedicated behavior.
- Use bounded request timeouts and conservative retries; avoid excessive requests.

### Transmission integration

- Allow an optional macOS `TRANSMISSION_APP_PATH`. Before recovery/polling, check the current user’s Transmission process and launch the configured bundle in the background if stopped, with bounded waits. Notify all allowed Telegram users when the bot starts Transmission or cannot start it; already running or disabled startup is quiet. Notification/launch failure must not prevent normal polling. Keep RPC configuration separate; do not change app preferences or submit torrents during this check. A blank/omitted path disables local launch for remote or daemon-only deployments.
- Connect to a configurable Transmission RPC endpoint with optional authentication. Negotiate the target version; use JSON-RPC 2.0 for API 6.x and the legacy format for RPC versions 14–17. Reject unsupported versions before mutation.
- Validate canonical BitTorrent v1 metainfo and derive its info hash from the original encoded info dictionary before submission. Limit bytes/depth/nodes and check file layouts, paths, piece lengths and piece counts. Reject unsupported v2/hybrid and symlink metainfo explicitly.
- Negotiate the RPC session token with bounded HTTP 409 retries, match response IDs, reject redirects, and bound RPC timeouts/response sizes. Do not log raw RPC errors, authorization/session headers, or base64 metainfo.
- Submit the downloaded `.torrent` file to Transmission; Transmission performs the content download.
- Support a configurable download directory, otherwise use Transmission's default.
- Handle RPC authentication and session negotiation, connection errors, invalid torrent files, and rejected requests.
- Report an already-added torrent as a duplicate instead of claiming a new download was created.
- Prevent repeated confirmation clicks from adding the same pending selection multiple times.
- If a submission times out or has an ambiguous result, look up the persisted hash. Report a match as confirmed present without claiming a new add versus duplicate. If missing or lookup fails, keep the operation uncertain and block add replay; immediate absence alone does not prove that an in-flight add cannot finish.
- Keep uncertain operations blocked across restarts through durable workflow state. Persist identity before the add RPC; cancellation during submission also requires recovery. The confirmed workflow supplies durable orchestration around the RPC adapter.

## Configuration and security

- Configure the Telegram bot token, optional Transmission app path, Transmission RPC URL and credentials, and optional download directory outside source code. RuTracker login/password come from the Telegram credential flow and SQLite, never environment variables.
- Support an allowlist of Telegram user IDs to restrict use of the bot; reject unauthorized requests before making upstream calls.
- Never commit credentials, session cookies, downloaded torrent files, or local runtime state.
- Do not expose credentials, tokens, or cookies in logs or Telegram messages.
- Validate selection identifiers against server-side state; never trust callback data as an arbitrary download URL.
- Keep torrent metainfo in memory and release it after completion/failure; do not create temporary torrent files.

## Reliability and observability

- Keep Telegram interaction responsive while search, download, and RPC operations run.
- Write diagnostic events to a local log file with timestamps and user/chat or operation identifiers so related events can be traced.
- Log user inputs, including search queries, result numbers, and button actions. Redact credentials, tokens, and CAPTCHA answers rather than recording their raw values.
- For each RuTracker request, log the HTTP method, URL, request headers, and POST form data when present. For each response, log the HTTP status and response headers. Redact sensitive values in URLs, headers, and form fields, including passwords, usernames used for authentication, Authorization, Cookie, Set-Cookie, and session/CAPTCHA tokens.
- Include a brief bot state summary with relevant events: current workflow stage, selected topic when present, and submission status when applicable. Avoid dumping complete state objects or binary data.
- Log search, selection, confirmation, and submission outcomes, including safe error details. Response bodies are not required in the log.
- Use a configurable log-file path with bounded rotation and retention. Keep log files out of Git.
- Provide clear user-facing errors and allow retrying a failed operation without silently choosing another torrent.
- A restarted bot must reject stale UI controls and ask for a new search rather than restoring an unconfirmed selection. Hold a single-process lock for the database before startup recovery and for the running bot’s lifetime.
- On startup, fail interrupted claims that never began submission; convert submitting operations to uncertain and look up their hashes. Resolve presence as a separate found outcome; absence/unreachable RPC remains uncertain. Never fetch or add during recovery.
- `/status` lists only the requesting user/chat’s confirmations, including every unresolved operation and up to ten recent terminal results. It reconciles by lookup only, stopping further RPC checks after an offline failure.
- Apply a transactional hash guard across selections to prevent fresh confirmations bypassing submitting/uncertain operations. Let pending SQLite writes settle before propagating cancellation. Retain acknowledged success when cancellation arrives after its commit.

## Acceptance criteria

- `/start` opens menu; log in is visible only without a verified session. Login and search reach their existing flows, and downloads returns to menu without requiring RuTracker.
- Active Transmission downloads display progress with empty/error handling and no torrent mutations.
- A search returns matching RuTracker topics with enough available detail to distinguish releases.
- A no-results search produces a clear response and permits another query.
- Selecting a result displays the exact release and requires explicit confirmation.
- Before confirmation, cancellation creates no Transmission task and retrieves no torrent file. After confirmation, cancellation stops the bot operation; any add already issued is retained as uncertain or acknowledged and is not removed from Transmission.
- Confirmation downloads the chosen torrent file and submits that same torrent to Transmission.
- Successful submission reports acceptance; a duplicate is identified correctly.
- Repeated confirmation and uncertain RPC outcomes do not create duplicate submissions.
- Unauthorized users and users interacting with another user's buttons cannot trigger downloads.
- RuTracker and Transmission failures produce useful messages without disclosing secrets.
- The diagnostic log records user inputs, RuTracker request headers and POST data, response status and headers, and brief workflow state, with sensitive values redacted.

## Design principles

Strive to follow SOLID and KISS principles when designing and implementing the application:

- Keep modules focused on a clear responsibility, with explicit boundaries between Telegram interaction, RuTracker access, Transmission access, and workflow storage.
- Apply SOLID where it improves clarity, maintainability, and testability. Keep interfaces small and dependencies explicit.
- Prefer the simplest solution that satisfies current requirements. Avoid speculative abstractions, unnecessary layers, and premature generalization.

## Initial scope

The initial version covers searching, listing, selecting, confirming, downloading torrent files, and submitting them to Transmission. Content recommendations, automatic release selection, media playback, and download-completion notifications are outside the initial scope. Implementation language: Python (approved). Proposed libraries: aiogram, aiohttp, and Beautiful Soup, with SQLite for durable workflow state. Start with Telegram long polling; finalize dependency versions and runtime requirements during step 1.

## macOS installer and service management

- Provide an executable `install.sh` for the existing checkout. Check installed Python 3.14 and a usable Transmission bundle, synchronize the locked runtime dependencies into `.venv` without downloading Python, and report missing prerequisites. uv and installed Chrome are documented prerequisites.
- Prompt for required Telegram bot token and allowed-user IDs. Offer existing exported environment/file values as defaults, keep them on Enter, and mask token defaults/input. Preserve unrelated `.env` values, including explicit blanks. Validate before an atomic owner-only save; detect concurrent edits rather than silently overwrite them.
- Every installer prompt, including service selection and final review, supports :restart to discard staged input and begin from original defaults. :quit, Ctrl+C, EOF, or declining final review must not save unfinished input. Require explicit yes at the final review before changing configuration or registering the service.
- Default to a login LaunchAgent as selected by the user, because the bot uses visible Chrome. Support an explicitly selected boot LaunchDaemon with its GUI limitation documented. Use absolute `.venv/bin/python`, `-u`, and the bot module in ProgramArguments, this checkout as WorkingDirectory, private log files, bounded crash restart frequency, and no secrets in the plist. Run as the normal user; only system plist registration/removal uses sudo.
- Reinstall only a service belonging to this checkout, guard against a running manual worker, and attempt rollback of config/plist/service on bootstrap failure. Keep source and all runtime data at their existing paths.
- `--uninstall` unloads/removes only this checkout's launchd service; all configuration, databases, logs, source, dependencies, and Transmission remain. Uninstall must not require uv, runtime dependencies, valid configuration, or Transmission. It may use Python 3.10+ for the helper. Refuse to overwrite or uninstall another checkout's service.
