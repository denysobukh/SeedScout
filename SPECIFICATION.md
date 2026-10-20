# Telegram Torrent Downloader — Application Specification

## Purpose

Build a Telegram bot that finds torrents on rutracker.org for content requested by a user, presents available results, and sends the exact torrent selected and confirmed by the user to Transmission.

## Core workflow

1. The bot checks its RuTracker session, authenticates if necessary, and asks the requesting user to solve any CAPTCHA before prompting for a query.
2. The bot uses the user's query unchanged to search rutracker.org and shows the first 10 results in returned order, numbered 1–10 (or fewer when fewer matches exist).
3. Each result includes its title, a link to the RuTracker topic, and available metadata useful for choosing a release, such as size, seed count, quality, language, and format. Missing metadata is omitted rather than invented.
4. The user picks a result number. The bot opens its stored topic link and displays the full release description, split across Telegram messages as needed, with Download and Back actions. Download is explicit confirmation; Back restores the original results and numbering.
5. Only after confirmation, the bot downloads the selected topic's `.torrent` file from rutracker.org.
6. The bot submits that torrent file to Transmission through its RPC interface.
7. The bot reports whether Transmission accepted the torrent, including its name and identifier when available. Acceptance means the download was queued; it does not mean the content finished downloading.

See [RUTRACKER_FLOW.md](RUTRACKER_FLOW.md) for the confirmed decision tree and retry destinations.

## Functional requirements

### Telegram interaction

- Provide `/start` and `/help` commands explaining how to search and add a torrent.
- Accept nonempty text queries and provide clear feedback for empty or unsupported input.
- Accept result numbers as selection input. Provide Download and Back buttons on the description, and New search and Cancel actions. Initial results are limited to the top 10; pagination is deferred.
- Keep search results and pending selections scoped to the requesting user and chat.
- Prevent another user from confirming someone else's selection.
- Expire stale selections and explain how to search again.
- Do not download a torrent file or submit it to Transmission before confirmation.

### RuTracker integration

- Search rutracker.org and retrieve topic metadata and torrent files.
- Before the first HTTP authentication check, use asynchronous Playwright with visible Google Chrome to visit `/forum/index.php`, let its JavaScript establish a session, then open `/forum/login.php`. Wait for visible username/password fields in `#login-form-full`, require a matching main-frame HTTP 200 response, and verify the final login URL.
- Capture `cf_clearance`, other applicable RuTracker session cookies, and the browser's actual User-Agent. Reuse them together in the HTTP client, preserving cookie domain/path, secure/HttpOnly flags, and expiry. Keep browser contexts isolated per user/chat workflow.
- Check authentication before asking for a query. If login is needed, use the rendered login form (`login_username`, `login_password`) and POST configured credentials to `/forum/login.php` with the imported browser session.
- Reuse the session for later search, topic, and torrent requests. If access is denied, invalidate the browser session and require fresh browser verification on retry; never automatically replay a login POST. Keep cookies and rendered HTML in memory, out of logs and Git. Restart requires fresh verification.
- When login requires CAPTCHA, deliver its image to the requesting user, submit their answer, and repeat with a refreshed image when necessary. Scope challenges and answers to that user and chat, expire challenges, and support cancellation.
- Preserve queries and selected topics across session reauthentication. Retrieve the full release description only after selection; opening a description must not download a torrent file.
- Preserve the association between each displayed result, its topic, and the downloaded torrent file.
- Handle no results, authentication failures, unavailable topics, network errors, and unexpected response formats with actionable messages.
- Use bounded request timeouts and conservative retries; avoid excessive requests.

### Transmission integration

- Connect to a configurable Transmission RPC endpoint with optional authentication.
- Submit the downloaded `.torrent` file to Transmission; Transmission performs the content download.
- Support a configurable download directory, otherwise use Transmission's default.
- Handle RPC authentication and session negotiation, connection errors, invalid torrent files, and rejected requests.
- Report an already-added torrent as a duplicate instead of claiming a new download was created.
- Prevent repeated confirmation clicks from adding the same pending selection multiple times.
- If a submission times out with an unknown outcome, check Transmission for the torrent before retrying.

## Configuration and security

- Configure the Telegram bot token, RuTracker credentials, Transmission RPC URL and credentials, and optional download directory outside source code.
- Support an allowlist of Telegram user IDs to restrict use of the bot; reject unauthorized requests before making upstream calls.
- Never commit credentials, session cookies, downloaded torrent files, or local runtime state.
- Do not expose credentials, tokens, or cookies in logs or Telegram messages.
- Validate selection identifiers against server-side state; never trust callback data as an arbitrary download URL.
- Clean up temporary torrent files after submission or failure.

## Reliability and observability

- Keep Telegram interaction responsive while search, download, and RPC operations run.
- Write diagnostic events to a local log file with timestamps and user/chat or operation identifiers so related events can be traced.
- Log user inputs, including search queries, result numbers, and button actions. Redact credentials, tokens, and CAPTCHA answers rather than recording their raw values.
- For each RuTracker request, log the HTTP method, URL, request headers, and POST form data when present. For each response, log the HTTP status and response headers. Redact sensitive values in URLs, headers, and form fields, including passwords, usernames used for authentication, Authorization, Cookie, Set-Cookie, and session/CAPTCHA tokens.
- Include a brief bot state summary with relevant events: current workflow stage, selected topic when present, and submission status when applicable. Avoid dumping complete state objects or binary data.
- Log search, selection, confirmation, and submission outcomes, including safe error details. Response bodies are not required in the log.
- Use a configurable log-file path with bounded rotation and retention. Keep log files out of Git.
- Provide clear user-facing errors and allow retrying a failed operation without silently choosing another torrent.
- A restarted bot must reject any pending selection it cannot safely restore and ask the user to search again.

## Acceptance criteria

- A search returns matching RuTracker topics with enough available detail to distinguish releases.
- A no-results search produces a clear response and permits another query.
- Selecting a result displays the exact release and requires explicit confirmation.
- Cancellation creates no Transmission task and downloads no torrent file.
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
