# Telegram Torrent Downloader — Application Specification

## Purpose

Build a Telegram bot that finds torrents on rutracker.org for content requested by a user, presents available results, and sends the exact torrent selected and confirmed by the user to Transmission.

## Core workflow

1. The user sends a content request to the Telegram bot, such as a movie title, series, album, or other search query.
2. The bot searches rutracker.org and returns a paginated list of matching torrents.
3. Each result includes its title, a link to the RuTracker topic, and available metadata useful for choosing a release, such as size, seed count, quality, language, and format. Missing metadata is omitted rather than invented.
4. The user selects a specific result. The bot displays its details and asks for explicit confirmation, with Confirm and Cancel actions.
5. Only after confirmation, the bot downloads the selected topic's `.torrent` file from rutracker.org.
6. The bot submits that torrent file to Transmission through its RPC interface.
7. The bot reports whether Transmission accepted the torrent, including its name and identifier when available. Acceptance means the download was queued; it does not mean the content finished downloading.

## Functional requirements

### Telegram interaction

- Provide `/start` and `/help` commands explaining how to search and add a torrent.
- Accept nonempty text queries and provide clear feedback for empty or unsupported input.
- Use buttons for result pagination, selection, confirmation, and cancellation.
- Keep search results and pending selections scoped to the requesting user and chat.
- Prevent another user from confirming someone else's selection.
- Expire stale selections and explain how to search again.
- Do not download a torrent file or submit it to Transmission before confirmation.

### RuTracker integration

- Search rutracker.org and retrieve topic metadata and torrent files.
- Support authenticated access where required, using configured credentials and session handling.
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
- Log search, selection, confirmation, and submission outcomes with enough context to diagnose failures without logging secrets.
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

## Initial scope

The initial version covers searching, listing, selecting, confirming, downloading torrent files, and submitting them to Transmission. Content recommendations, automatic release selection, media playback, and download-completion notifications are outside the initial scope. Implementation language: Python (approved). Proposed libraries: aiogram, aiohttp, and Beautiful Soup, with SQLite for durable workflow state. Start with Telegram long polling; finalize dependency versions and runtime requirements during step 1.
