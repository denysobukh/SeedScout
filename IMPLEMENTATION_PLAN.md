# Implementation plan

## Agreed workflow

- Implement one numbered step at a time.
- Update PROGRESS.md with changes, validation results, limitations, and the next action before requesting review.
- Stop for user review after each step. Do not start the next step until the user approves the current step.
- After approval, commit that step and its progress update, then proceed to the next step unless the user asks to pause.
- If review requests changes, address them within the same step and request review again before committing.
- Do not commit credentials or runtime data. Do not perform live Transmission submissions without explicit authorization for that test.
- The initial planning documents also await review before their first commit.

## 1. Python project foundation and configuration

Create the package layout, pyproject.toml, dependency lock file, supported Python version, application entry point, and configuration loader. Include .env.example, secret-safe logging, and setup instructions. Validate required settings, Telegram user IDs, and service URLs. Use aiogram, aiohttp, Beautiful Soup, and SQLite unless implementation evidence warrants a documented change.

Validation: installation and import checks, configuration validation tests, and a documented startup command. No live credentials required.

## 2. Durable workflow state

Define torrent result and selection models. Add SQLite storage for search results, user/chat ownership, selection expiry, submission status, and per-user validated RuTracker credentials. Make confirmation claims atomic to prevent competing handlers from submitting the same selection. Record enough identity and state to reconcile uncertain submissions after restart.

Validation: meaningful tests for ownership, expiry, concurrent confirmation, and reopening the database after restart.

## 3. RuTracker integration

Follow RUTRACKER_FLOW.md. Inspect actual response formats and implement Playwright/visible-Chrome session acquisition, verified login form, cookie/User-Agent transfer and reuse, session checks, login, user/chat-scoped CAPTCHA challenges and answers, server-side seed sorting and local descending seed ranking before limiting to ten distinct search results, full topic descriptions, and separate torrent file retrieval. Handle character encoding, expired sessions, no results, unavailable topics, timeouts, and unexpected HTML. Keep source URLs constrained to RuTracker. Download torrent bytes only through a separately invoked method after confirmation.

Validation: sanitized HTML fixtures and mocked HTTP tests for parsing, authentication, CAPTCHA renewal/expiry/ownership, top-10 ordering, full descriptions, and failures. Live read-only checks only when credentials and access are available; document any unverified assumptions.

## Diagnostic logging follow-up (between steps 3 and 4)

Implement the approved diagnostic logging requirements: configurable rotating file logs, sanitized RuTracker requests/responses, brief client and durable workflow state, and a user-input logging helper. Wire query/CAPTCHA events now and remaining Telegram inputs in step 4. Validate rotation, permissions, redaction, HTTP redirect/error diagnostics, and CLI behavior. Stop for review before committing.

## 4. Telegram search and selection

Implement long polling, /start, /help, access control, and the confirmed RUTRACKER_FLOW.md conversation. Menu follow-up: /start shows Checking RuTracker login., verifies an existing session or authenticates with saved credentials/CAPTCHA, then opens menu with log in hidden on success and visible when login is needed, alongside search and downloads; reuse existing login/search branches, return menu login to menu, and show a read-only active Transmission list including seeding torrents, with reload and menu return buttons on populated/empty/error results. Existing search flow: authentication before query, separate private-chat login/password prompts when credentials are missing or rejected, a credential-entry loop without buttons until authentication succeeds, saving only validated credentials in SQLite, CAPTCHA images/answers, unchanged text queries, numbered top-10 results with matching numeric buttons (no New search/Cancel buttons on the results list), button or typed-number selection, other nonempty text as an unchanged new query, full descriptions split as needed with only Download/Back buttons, and every nonempty description-screen text input (including numbers) as an unchanged new query. Download acts as confirmation; Back preserves results. Pagination is deferred. Bind all callbacks to stored user/chat state, reject stale callbacks, and escape external text for Telegram rendering. Keep upstream operations asynchronous. Omit Cancel buttons from every screen; retain the /cancel command.

Validation: handler tests covering authentication, CAPTCHA, search, numeric selection, description splitting, Back navigation, no results, ownership, cancellation, and expiry. This step displays confirmation controls but does not yet enable real submissions.

## 5. Transmission integration

Inspect the target Transmission version and support its RPC protocol. Implement session negotiation, optional authentication, torrent submission, duplicate detection, and torrent lookup by info hash. Validate torrent data and derive its identity before submission. Reconcile timeout outcomes before permitting retries. Support an optional download directory.

Implementation checkpoint: standalone client and v1 torrent validator implemented; target Transmission 4.1.3/API 6.0.1 verified with read-only RPC calls. JSON-RPC API 6.x and legacy RPC 14–17 supported. Metainfo validation is limited to v1; v2/hybrid files are rejected explicitly. Telegram/SQLite submission orchestration belongs to step 6.

Validation: synthetic metainfo and local fake-RPC tests for acceptance, duplicates, session negotiation, identity, failures, cancellation, and timeout reconciliation. Live session information and missing-hash lookup are read-only. Any live submission requires explicit test authorization.

## 6. Confirmed download and submission workflow

Wire the Download action (explicit confirmation) to the atomic state transition, RuTracker torrent retrieval, and Transmission submission. Report accepted, duplicate, failed, and uncertain outcomes accurately. Reconcile interrupted submissions on restart, handle repeated button clicks, and clean up temporary data. Ensure cancellation and unconfirmed selections never fetch torrent files or submit tasks.

Implementation checkpoint: confirmed Download wiring, metainfo validation/hash persistence, durable found outcomes and unresolved-hash guard, cancellation-aware SQLite writes, login/CAPTCHA resumption, /status, and startup recovery under a single-process database lock are implemented. Metainfo stays in memory. Operational readiness remains step 7.

Validation: fake upstream integration tests cover the full workflow, concurrent/foreign/stale clicks, cancellation during I/O and commits, schema migrations, restart recovery, scoped status, and uncertain RPC outcomes. A live selected-torrent submission remains a separate user-authorized test.

## 7. Operational readiness and acceptance review

Finish graceful shutdown, bounded retries, resource cleanup, useful secret-safe logs, and deployment/run instructions for the chosen host. Local macOS startup follow-up: configure the Transmission app path, check/start it before recovery, and notify allowed users on launch or failure; blank configuration supports remote servers. This follow-up alone does not complete step 7. Document setup of Telegram, RuTracker, and Transmission, backup of workflow state, and troubleshooting. Check all specification acceptance criteria and record remaining limitations.

Validation: run the complete automated checks and a local startup/shutdown smoke test. Perform a live end-to-end test only with available credentials and explicit authorization for the selected test torrent. Record blocked live checks honestly.
