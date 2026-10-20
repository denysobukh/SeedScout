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

Define torrent result and selection models. Add SQLite storage for search results, user/chat ownership, selection expiry, and submission status. Make confirmation claims atomic to prevent competing handlers from submitting the same selection. Record enough identity and state to reconcile uncertain submissions after restart.

Validation: meaningful tests for ownership, expiry, concurrent confirmation, and reopening the database after restart.

## 3. RuTracker integration

Inspect actual response formats and implement authenticated sessions, search pagination, topic details, and torrent file retrieval. Handle character encoding, expired sessions, no results, unavailable topics, timeouts, and unexpected HTML. Keep source URLs constrained to RuTracker. Download torrent bytes only through a separately invoked method after confirmation.

Validation: sanitized HTML fixtures and mocked HTTP tests for parsing, authentication, pagination, and failures. Live read-only checks only when credentials and access are available; document any unverified assumptions.

## 4. Transmission integration

Inspect the target Transmission version and support its RPC protocol. Implement session negotiation, optional authentication, torrent submission, duplicate detection, and torrent lookup by info hash. Validate torrent data and derive its identity before submission. Reconcile timeout outcomes before permitting retries. Support an optional download directory.

Validation: mocked RPC tests for acceptance, duplicates, session negotiation, failures, and timeout reconciliation. Any live submission requires explicit test authorization.

## 5. Telegram search and selection

Implement long polling, /start, /help, access control, text search, paginated result buttons, release details, and Confirm/Cancel buttons. Bind all callbacks to stored user/chat state, reject stale callbacks, and escape external text for Telegram rendering. Keep upstream operations asynchronous.

Validation: handler tests covering search, pagination, no results, ownership, cancellation, and expiry. This step displays confirmation controls but does not yet enable real submissions.

## 6. Confirmed download and submission workflow

Wire confirmation to the atomic state transition, RuTracker torrent retrieval, and Transmission submission. Report accepted, duplicate, failed, and uncertain outcomes accurately. Reconcile interrupted submissions on restart, handle repeated button clicks, and clean up temporary data. Ensure cancellation and unconfirmed selections never fetch torrent files or submit tasks.

Validation: integration tests with fake upstream services covering the full workflow, concurrent clicks, restarts, and ambiguous RPC outcomes.

## 7. Operational readiness and acceptance review

Finish graceful shutdown, bounded retries, resource cleanup, useful secret-safe logs, and deployment/run instructions for the chosen host. Document setup of Telegram, RuTracker, and Transmission, backup of workflow state, and troubleshooting. Check all specification acceptance criteria and record remaining limitations.

Validation: run the complete automated checks and a local startup/shutdown smoke test. Perform a live end-to-end test only with available credentials and explicit authorization for the selected test torrent. Record blocked live checks honestly.
