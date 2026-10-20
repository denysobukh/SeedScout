# Project progress

## Resume instructions

Read this file, IMPLEMENTATION_PLAN.md, and SPECIFICATION.md before making changes. Inspect git status and recent commits to reconcile the recorded state with the working tree. Follow the review gate: complete one step, request review, and commit only after approval. Update this file at every review checkpoint and after review feedback.

## Current checkpoint

- Language: Python, approved by the user.
- Repository: main; initial planning commit 9f31c21; step 1 commit 7a456bf; step 2 commit 223716a.
- Planning: specification, implementation plan, and progress file approved by the user.
- Implementation: steps 1–3 and follow-ups committed (browser session e59a741; plan reorder a9b72ab). Step 4 Telegram conversation and authentication-marker fix approved by the user and included in this implementation commit.
- Next action: step 5 (Transmission integration) has not started. Keep polling stopped unless the user requests another Telegram test. Backend login, session recheck, search, and topic extraction now pass live verification after the authentication-marker fix. The corrected Telegram conversation still needs retesting; polling remains stopped.
- Live verification: Chrome obtained cf_clearance and aiohttp reused the imported cookies/User-Agent to fetch the login page successfully. A later live investigation also verified credential login, session recheck, search, and full topic extraction. CAPTCHA and the fixed Telegram interaction remain unverified. Live Transmission submissions still require explicit test authorization.

## Step tracker

| Step | Work | Status |
| --- | --- | --- |
| 0 | Specification and implementation plan | Approved; included in initial commit |
| 1 | Python foundation and configuration | Approved; committed |
| 2 | Durable workflow state | Approved; committed |
| 3 | RuTracker integration | Approved; committed; live authenticated verification limited |
| 3a | Diagnostic logging follow-up | Approved; committed |
| 3b | Playwright browser-session login | Approved; committed |
| 4 | Telegram search and selection | Approved; committed |
| 5 | Transmission integration | Not started |
| 6 | Confirmed download and submission | Not started |
| 7 | Operational readiness and acceptance | Not started |

## Validation and review log

- 2026-10-21: Confirmed the repository was empty and initialized Git on main. Created specification and .gitignore.
- 2026-10-21: Recorded Python decision and prepared the implementation plan and review workflow. Documentation only; no application tests applicable. No credentials or live service calls used.

- 2026-10-21: User requested the initial commit, approving the planning baseline. Implementation has not started.

## Commit tracking

Initial commit: specification, implementation plan, progress file, and .gitignore. Use git log as the source of truth for commit hashes; approval and completion should be recorded here in the corresponding step commit.

## Step 1 review checkpoint

- Added a src-layout Python package, module and console entry points, pyproject.toml, .python-version, and uv.lock.
- Runtime requirement: Python 3.14.x; verified with local Python 3.14.5. Runtime dependencies resolved to aiogram 3.31.0, aiohttp 3.14.5, Beautiful Soup 4.15.0, and python-dotenv 1.2.4. SQLite will use the standard library in step 2.
- Added immutable configuration with required Telegram allowlist, token syntax checks, RuTracker credentials, validated Transmission URL and paired optional credentials, database path, optional download directory, and log level.
- Environment variables override the current-directory .env without interpolation or mutation of the process environment. Validation errors omit supplied values.
- Added stderr logging with configured credential and authentication-header redaction, including formatted exceptions and encoded secret values; HTTP dependency debug logging is suppressed.
- Added .env.example and README setup/check instructions. Expanded .gitignore for Python environments, build output, and SQLite runtime files.
- Validation: 52 tests passed; Ruff lint and formatting checks passed; locked dependency installation and runtime imports passed; console --help smoke check passed; git diff whitespace check passed.
- Tests cover invalid/missing configuration, allowlist limits, credential pairing, URL validation, environment precedence, invalid environment-file encoding, logging redaction, CLI exit codes, and offline configuration checks.
- Local tooling: uv installed in ignored .tools/; project dependencies installed in ignored .venv/. No global package installation, real credentials, or live service calls.
- Expected limitation: polling and integrations are not implemented in this step. --check-config validates settings offline; default invocation returns exit code 1 with an explanatory message.
- Review: approved by the user with the instruction to commit. Verified that .env and .env.* are ignored, .env.example is permitted, and no real environment files are tracked. Step 2 has not started.

## Step 2 review checkpoint

- Added immutable torrent result, search, selection, and status models. Topic URLs derive from validated RuTracker topic IDs; optional metadata is preserved without invention.
- Added a versioned SQLite schema and transactional WorkflowStore with per-operation connections, bounded lock waits, explicit commits/rollback, and foreign keys. No new dependencies.
- Stored ordered search snapshots, user/chat ownership, expiry, and stable selections. Repeated selection of the same search result returns its existing selection.
- Atomic confirmation claims use database transactions and a worker claim token. Confirmation and cancellation compete for the same pending state; only one can win.
- Added submission transitions that persist a validated torrent hash before an add RPC. Terminal states cannot be reclaimed. Ambiguous outcomes remain blocked as uncertain.
- Internal list_incomplete() exposes claimed/submitting/uncertain records after restart, without automatically resetting them. The future step 6 coordinator must reconcile submitting/uncertain records through Transmission and stop prior workers before recovery.
- Documented asyncio.to_thread usage for this synchronous storage API, lifecycle, expiry behavior, and recovery obligations in README.md.
- Validation: full suite 74 passed (22 new cases), Ruff lint and formatting passed, git diff whitespace check passed. Concurrency tests use separate store instances and simultaneous threads; restart tests reopen the on-disk database and initialize it again.
- Tests cover ownership on all user operations, invalid IDs/positions, immutable snapshots, expiry boundaries, cancellation, terminal transitions, wrong worker tokens, concurrent confirmations, cancellation races, hash persistence, restart recovery, unsupported schema versions, empty results, and off-event-loop usage.
- Review: approved by the user with the instruction to commit. Step 3 has not started. No credentials read or live service calls made for this step.
- Expected limitation: recovery lookup and live submissions are implemented in later steps; this storage layer records state and enforces transitions only. Runtime startup wiring remains deferred to the bot orchestration steps.

- Specification update: user requested SOLID and KISS guidelines; added practical design principles to SPECIFICATION.md. Included with the approved step 2 commit.

## Confirmed flow update

- User approved authentication before query, user-solved CAPTCHA, unchanged queries, numbered first 10 results, full descriptions, and Download/Back navigation.
- Added RUTRACKER_FLOW.md; aligned the specification and steps 3, 5, and 6. Pagination is deferred; Download is explicit confirmation.
- User explicitly authorized committing these documentation changes before implementing step 3.

## Step 3 review checkpoint

- Added asynchronous RuTrackerClient with its own cookie session per user/chat workflow, serialized operations, safe errors, timeouts, response-size bounds, and same-origin HTTPS redirects. No dependency changes.
- Authentication reads the homepage form and POSTs login_username/login_password to /forum/login.php. No GET of the dedicated login page. This incorporates the user's correction and is covered by a regression test.
- Added in-memory user/chat-scoped CAPTCHA challenges with image retrieval, five-minute expiry, cancellation, answer submission, renewed challenge handling, and single consumption even after a network failure. No Telegram delivery yet (step 4).
- Added unchanged-query Windows-1251 search requests, up to ten distinct results in source order, available size/seed metadata, explicit empty-results handling, and session-expiry errors for caller-driven reauthentication/resumption.
- Added full first-post description extraction, including hidden spoiler text and useful image/link URLs, excluding scripts and replies. Does not retrieve embedded media or torrent bytes while browsing.
- Added separate torrent retrieval with basic MIME/framing checks. Full bencode/hash verification remains step 5; confirmed workflow wiring remains step 6.
- Validation: 120 tests passed (46 new cases); Ruff lint and formatting passed; git diff whitespace check passed. Tests cover authentication, CAPTCHA ownership/expiry/refresh/concurrency/restart, form encoding, unchanged queries, top-ten ordering, descriptions, unsafe links/redirects, response bounds, and HTTP/network failures.
- Live verification: dedicated login GET returned HTTP 403. Following the user's correction, verified both https://rutracker.org/ and /forum/index.php return HTTP 200, with a Windows-1251 login form containing the stated fields and POST action login.php.
- A single live authentication attempt using local credentials received HTTP 403 from the login POST. No successful auth, live CAPTCHA, authenticated search, or authenticated topic checks could be completed. No real torrent file was downloaded or submitted. No credential values or cookie state were printed or saved in fixtures.
- Fixtures are synthetic and explicitly labeled; auth markers, CAPTCHA markup, and authenticated search/topic selectors remain assumptions pending live verification. Do not claim successful live integration based on fixture tests.
- Updated README.md, SPECIFICATION.md, and RUTRACKER_FLOW.md with homepage login behavior and limitations.
- Review: approved by the user with the instruction to commit, including the homepage correction and documented live verification limitation. Telegram conversation remains step 4, Transmission client step 5, and Download orchestration step 6.

## Diagnostic logging specification update

- User requested a log file containing user inputs, RuTracker request headers and POST data, response headers, and brief bot state.
- Updated SPECIFICATION.md with those requirements, response status, event correlation, configurable file path, rotation/retention, and redaction consistent with the existing no-secrets-in-logs requirement.
- Specification only: current implementation still logs to stderr and has no HTTP/state diagnostic instrumentation. Implement file logging and RuTracker instrumentation in a follow-up change, then add user-input/state events with the Telegram workflow. Do not consider the new requirement implemented yet.
- User approved committing this documentation change. No application behavior changed; diagnostic file logging remains to be implemented.

## Diagnostic logging implementation review checkpoint

- Added LOG_FILE, LOG_MAX_BYTES, and LOG_BACKUP_COUNT configuration, defaults .runtime/bot.log / 5242880 bytes / 3 backups, with validation and .env.example entries. No local .env changes.
- Configured stderr plus private rotating UTF-8 file logging. Files and rotated backups are owner-only (0600); *.log.* is ignored alongside *.log. Startup file-open failures return a safe configuration error; write/rotation failures do not dump original records.
- RuTracker request-start, actual-headers-sent, response, and network-error events include method, safe URL, POST form data, user/chat, request ID, redirect hop, and short state. Response headers/status are recorded before HTTP errors or redirects are handled. Never log response bodies or torrent bytes.
- Sanitize headers and form/query fields before creating records, including Cookie/Set-Cookie/Authorization, login credentials, CAPTCHA answers and dynamic field names, challenge/session identifiers, and unknown values. Configured secret values are additionally redacted in both outputs, including JSON/URL-encoded variants.
- Added client state summaries, search input logging, redacted CAPTCHA input logging, and a helper for remaining Telegram inputs. Added durable selection/submission state logging after successful database commits, excluding worker claim tokens.
- Preserved --check-config without filesystem/network side effects; normal startup opens the log file. Added README configuration, lifecycle, retention, and limitations, and inserted this follow-up in the implementation plan.
- Validation: 142 tests passed (22 new cases), Ruff lint/format checks passed, git diff whitespace check passed. Includes an actual aiohttp local-server test for outgoing auto headers, cookies, redirects and a 403 response; verifies secrets are absent and requests remain unchanged. Other tests cover rotation/retention, owner-only permissions, failure diagnostics, configuration validation, input redaction, and durable state events.
- No external service calls, real credentials, or live torrent downloads/submissions were used for this follow-up. Tests use temporary logs and a local HTTP server. Live RuTracker login limitation is unchanged.
- Review: approved by the user with the instruction to commit. Remaining Telegram command/numeric/button input logging will be wired in step 4, and live Transmission workflow remains steps 5/6. This change does not implement those handlers or claim full end-to-end logging coverage yet.

## Playwright browser-session login review checkpoint

- User requested Playwright session acquisition following the supplied visible-Chrome example. This supersedes the earlier homepage-only login approach.
- Added async browser_session.py: launch installed Chrome visibly with the supplied options, visit /forum/index.php then /forum/login.php, wait for #login-form-full fields, verify final URL and matching main-frame HTTP 200, capture cf_clearance and applicable session cookies with browser User-Agent, and close the browser.
- Imported cookies into a fresh aiohttp cookie jar, preserving host-only/domain scope, path, secure/HttpOnly flags, and expiry. Reuse the matching User-Agent for subsequent HTTP operations. Prefer the rendered full login form for the initial POST; preserve server-side CAPTCHA behavior.
- Invalid/missing/expired clearance is rejected. Later HTTP 401/403 invalidates the browser session for renewal on explicit retry, without automatic login POST replay. Sessions/HTML remain in memory; no session files are exported.
- Added Playwright to pyproject.toml and uv.lock (resolved 1.64.0), plus local-artifact ignore rules. Google Chrome is already installed locally; no new browser binary or global package was installed.
- Live check: visible Chrome acquired cf_clearance and the verified login form; aiohttp reused its cookie jar and User-Agent to fetch /forum/login.php successfully (HTTP 200, 11095 response bytes). No credentials were submitted, no cookies printed, no torrent files retrieved, and no session artifacts saved.
- Automated validation: 153 tests passed (11 new cases), Ruff lint and formatting passed. Tests cover browser status/URL/form checks, missing clearance, timeouts and cleanup, scoped cookie/User-Agent transfer, expired clearance, rendered-form login, no POST on browser failure, and denied-session invalidation. Most browser failure tests use a fake Playwright API; the live check confirms real browser capture and HTTP reuse.
- Updated specification, decision tree, implementation plan, and README. Deployment now requires installed Google Chrome and an active graphical session. Actual credential login/CAPTCHA/search remain unverified by this update; earlier direct-HTTP 403 attempts are historical.
- Review: user approved the browser-session login update and requested documentation updates and commit. Next numbered step is 4; it has not started.

## Plan order update

- User requested Telegram conversation before Transmission integration. Renumbered remaining steps: 4 Telegram search and selection, 5 Transmission integration, 6 confirmed download/submission, 7 operational readiness.
- Updated cross-references to the new numbering; next implementation step is 4 (Telegram). Documentation/order change only; no runtime behavior changed. User approved committing this update.

## Step 4 Telegram conversation review checkpoint

- Saved the user-provided Telegram allowlist ID in the ignored local .env, preserving existing credentials and 0600 permissions. No local configuration or credential values are committed.
- Added TelegramConversation and aiogram long-polling runtime with /start, /search, /help, /cancel, access control before upstream work, and per-user/chat browser clients.
- Authenticate before prompting for a query; deliver user/chat-scoped CAPTCHA images, redact answers, refresh expired/incorrect challenges, and resume saved searches/topics after reauthentication.
- Accept unchanged queries, display the first ten results with original order, numbering, links, size/seed metadata, and validate numeric selection. Retrieve full descriptions as plain text with disabled previews and UTF-16-safe splitting; show Download/Back/New search/Cancel on the final message.
- Back preserves the stored results and numbering. Download remains a safe step-4 placeholder: no confirmation claim, torrent file retrieval, or Transmission operation. Cancellation interrupts in-flight work and cancels pending selections when possible.
- Scope controls to the current user/chat and fresh screen token, reject stale/forged/restarted controls, enforce search expiry, and expire/clean idle UI/client sessions. SQLite operations run via asyncio.to_thread; active conversation/browser state is safely rejected after restart.
- Wire command, numeric input, and button diagnostic logging; CAPTCHA answers remain redacted. Added bounded Telegram rate-limit retries and client/bot cleanup on shutdown.
- Normal entry point now runs polling. --check-config remains offline and side-effect free. Omitted TRANSMISSION_RPC_URL now uses the standard local default so step 4 needs no Transmission configuration or connection.
- Validation: full suite 187 tests passed (34 new cases), Ruff lint/format checks and git diff whitespace checks passed. Coverage includes actual aiogram routing, mocked polling lifecycle, access control, top-10 results, numeric input, CAPTCHA, retries, description splitting, cancellation, concurrent callbacks, stale/foreign controls, restart/expiry, and placeholder Download behavior.
- Read-only Telegram getMe verified the stored token belongs to @SeedScoutBot. Local --check-config passed with the saved allowlist and credentials. No live polling, Telegram messages, torrent retrievals, or Transmission submissions were performed.
- Updated README with startup instructions, current behavior, default RPC configuration, and restart/expiry semantics. Real credential login and a live Telegram conversation remain unverified; browser clearance transfer was verified in step 3b.
- Review: user approved committing step 4 together with the authentication-marker fix after investigation.

## Live Telegram test and authentication-marker fix

- At the user's request, started local polling. Their /start reached browser verification and a successful login POST/homepage redirect, but the bot reported an unknown login result. Retry reported that no login form was found.
- Stopped polling with SIGTERM at the user's request and confirmed clean process exit. Polling remains stopped throughout this investigation.
- Root cause: _authenticated() only recognized logout URLs. A sanitized live inspection confirmed the authenticated page contains a nonempty #logged-in-username marker, no matching logout link, and no login input fields. The credentials had succeeded; the parser misclassified the authenticated page.
- Added the observed marker as positive authentication evidence while retaining legacy logout-link support. Updated the anonymized authenticated fixture and added regressions for recognition without a logout link and rejection of an empty marker.
- Live verification after the fix: credential login recognized; repeated authentication check True; read-only Ubuntu search returned 10 results; selected topic description extracted (8708 characters). No torrent file retrieved or submitted, no Telegram polling/messages during the investigation, and no raw credentials/cookies/HTML saved or printed.
- Validation: 83 focused RuTracker/conversation tests passed; full suite 189 tests passed; lint, formatting, and whitespace checks passed.
- Updated README and fixture notes. User approved committing step 4 and this fix. Next testing action, only when requested: restart the bot for the user's Telegram walkthrough. Do not automatically restart polling.
