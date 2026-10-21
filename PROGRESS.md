# Project progress

## Resume instructions

Read this file, IMPLEMENTATION_PLAN.md, and SPECIFICATION.md before making changes. Inspect git status and recent commits to reconcile the recorded state with the working tree. Follow the review gate: complete one step, request review, and commit only after approval. Update this file at every review checkpoint and after review feedback.

## Current checkpoint

- Language: Python, approved by the user. Repository: main; latest approved commit is 2e02cf5 (simplified torrent detail controls).
- Steps 1–4, diagnostic logging, Playwright login, SQLite credentials, seed-ranked results, and UI updates are committed.
- Step 5 is approved for commit: standalone Transmission RPC client and BitTorrent v1 validator. Step 6 (confirmed download/submission orchestration) has not started; Download is still a placeholder.
- Read-only live verification: installed Transmission 4.1.3, RPC 19/API 6.0.1; the new client negotiated the session and confirmed a missing-hash lookup. No torrent was retrieved or submitted. Live submissions require explicit authorization for the selected test torrent.
- User approved committing step 5. Next action: implement step 6 when authorized. Use the latest log entries rather than older historical runtime status when resuming.

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
| 5 | Transmission integration | Approved; included in this commit |
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

## Numbered result buttons update

- User reported that the corrected Telegram flow works as expected and requested numeric result buttons instead of New search/Cancel on the results list, with other text treated as a new query.
- Updated the specification, decision tree, implementation plan, and README. Results now show exactly 1–N buttons for the displayed torrents, in rows of at most five, preserving numbering and owner/screen token checks.
- Numeric buttons and valid typed result numbers open the selected topic. Other nonempty text (including out-of-range numbers) starts a new search unchanged from results or descriptions. Commands, CAPTCHA answers, description navigation, and /cancel retain their dedicated behavior.
- Added tests for small result lists, ten-button layout, numeric callback selection, out-of-range/string queries, invalidating old description controls, and updated concurrent/foreign callback tests for numeric buttons.
- Validation: 36 focused conversation tests and all 192 regression tests passed; Ruff lint, formatting, and git diff whitespace checks passed. User approved committing this update.
- The prior test process was no longer running when preparing the update. Started the updated local bot after validation for the ongoing user-requested Telegram test. Send /start to create fresh controls; previous screen tokens expire across restart. Included in the user-approved search interaction commit.

## Unrecognized search results input update

- User requested removal of buttons from the "RuTracker search table contains no recognizable releases" screen and treatment of subsequent text as a fresh search query.
- Added a specific UnrecognizedSearchResults error, retaining compatibility with UnexpectedPage handling. The conversation clears obsolete search/selection state, invalidates previous controls, shows the diagnostic without markup, and returns to query input.
- All nonempty text, including numbers, is searched unchanged in that state. Commands retain their existing behavior; other error categories keep their existing handling.
- Updated specification, decision tree, and README. Added parser and conversation regressions for button-free recovery and text/numeric queries. User approved committing this update together with the numbered result buttons.

- Final validation for button-free search-error recovery: all 197 tests passed; Ruff lint/format and whitespace checks passed. Started the updated local bot for the ongoing Telegram test. Send /start for fresh controls. Included in the user-approved search interaction commit.

## Live CAPTCHA CDN failure and fix

- User requested a temporary incorrect RuTracker username to trigger CAPTCHA. The override is process-local; saved .env credentials remain unchanged.
- Their screenshot showed "unsupported source URL" after failed login. Live inspection found that the rendered login form uses https://static.rutracker.cc/captcha/ image URLs, which the primary rutracker.org-only URL validator rejected.
- Added a dedicated CAPTCHA URL policy for the exact observed CDN/path and the legacy /forum/captcha.php endpoint. Normal login/search/topic/torrent URLs remain constrained to rutracker.org.
- CDN images use an isolated DummyCookieJar session and no forwarded headers or POST data; scoped redirects, timeout/size limits, MIME validation, and sanitized diagnostics remain in effect. No credentials or cookies are sent to the CDN.
- Updated the specification, flow, README, and fixture notes. Added a synthetic CDN CAPTCHA fixture and tests for challenge delivery, disallowed hosts/paths/protocols, cookie-free transport, and rejection of CDN login/torrent requests.
- Live verification retrieved an image/jpeg CAPTCHA (4108 bytes) using the temporary incorrect username. No answer was attempted, no raw token or image was printed/saved, and no torrent was retrieved.
- Targeted tests: 102 passed. Final full-suite validation and local test restart are recorded below. Changes are uncommitted, awaiting user review.

- Final CAPTCHA CDN fix validation: all 208 tests passed; Ruff lint/format and whitespace checks passed. Restarted the local bot with the temporary wrong username for the user to verify CAPTCHA delivery. Saved .env credentials are unchanged. Fix and docs remain uncommitted.


## SQLite RuTracker credentials and interactive login

- User requested removing RuTracker login/password from environment configuration, separate login/password prompts, and repeated credential entry after failed authentication, with no New search button on the failure screen.
- Added schema version 2 with per-Telegram-user credential storage; migration preserves searches and selections. The database is created and reopened with owner-only permissions. Credentials are excluded from object representations and diagnostic inputs, with runtime formatter redaction for newly entered or loaded values.
- Missing credentials prompt for login and then password in private chat. Invalid credentials remove only that rejected saved pair and return to login with Cancel only. Candidates remain in memory until authentication succeeds, including CAPTCHA completion. Cancellation/expiry clears pending values; authenticated pairs persist across restarts.
- Search/topic resumption survives credential replacement. Removed RuTracker fields from Settings and .env.example; legacy environment variables are ignored. Updated specification, flow, and README. Earlier uncommitted CAPTCHA CDN fix remains included.
- Validation and local credential migration status follow below. Changes remain uncommitted pending user review.

- Validation: all 213 regression tests passed; Ruff lint and formatting checks, git whitespace checks, and offline configuration validation passed. Tests cover credential retry loops, deferred saving through CAPTCHA, rejected-pair cleanup, per-user isolation, restart reuse, secret redaction, schema migration, and resuming an interrupted search.
- Migrated the existing local credential pair previously verified by the earlier live login test into SQLite for the sole allowed Telegram user, then removed both RuTracker keys from local .env. Verified database permissions 0600 and that .env/database remain ignored. Future credential pairs are saved only by successful authentication in the conversation.
- Restarted the updated local bot for the ongoing Telegram test. It received /start and entered browser verification using SQLite credentials. Full live testing of the new credential-entry loop remains for user review. Runtime process uses no temporary incorrect-username override. No torrent download or Transmission submission was performed.
- Review checkpoint: implementation, docs, tests, and the earlier CAPTCHA CDN fix are uncommitted. Next action: user verifies Telegram behavior and requests any changes or commit.

- User approved committing the SQLite credential flow and accompanying CAPTCHA CDN fix. Validation remains 213 passing tests with lint, formatting, and whitespace checks passed. Next planned implementation step is Transmission integration (step 5), pending user instruction.

## Search result URL removal

- User requested removing RuTracker topic links from search results. Removed the URL line from each result; numbered titles, size, seed count, and selection controls remain. Updated specification and flow. Changes await review and commit.
- Validation: all 47 conversation tests passed; Ruff formatting, lint, and whitespace checks passed. The prior bot process was no longer running; starting the updated app for the ongoing Telegram test.

## Seed-ranked search results

- User requested sorting by seed count descending before applying the top-ten limit, and checking RuTracker's server-side sort controls.
- Verified the authenticated live search form: select `o` option `10` is "Количество сидов"; radio `s=1` is ascending and `s=2` is descending. A read-only Ubuntu search with `o=10&s=2` returned 50 seed counts in descending order. No torrent was downloaded or submitted.
- Search requests now include `o=10&s=2` while preserving the original query and Windows-1251 encoding. The parser collects all distinct topics from the returned page, stably sorts by seeds descending, then takes ten. Unknown counts follow zero; equal counts retain source order. Pagination remains deferred.
- Added regressions for highly seeded rows beyond the former early limit, deduplication, stable ties, missing/invalid counts, and exact request parameters. Sort parameters remain visible in sanitized diagnostics. Updated specification, flow, implementation plan, and README.
- Validation: all 120 targeted RuTracker, conversation, and logging tests passed; Ruff lint/format and whitespace checks passed. Live form and server ordering verification succeeded after browser-session authentication.
- The previous bot process was no longer running; starting the updated app for the ongoing Telegram test. This change and the preceding result-link removal remain uncommitted for user review.

- User approved committing seed-ranked search results and the preceding result-link removal. Included implementation, regression tests, and documentation; validation remains 120 targeted tests passed with lint, formatting, and whitespace checks passed. Next planned step: Transmission integration (step 5), pending user instruction.

## Simplified torrent details controls

- User requested removing New search and Cancel buttons from a single torrent detail screen, with arbitrary text treated as a search query.
- Details now offer only Download and Back. All nonempty text, including valid result numbers and unknown slash-prefixed strings, searches unchanged; recognized /start, /search, /help, and /cancel commands retain their behavior. New queries clear the selected topic and invalidate its old controls. Result-list numeric selection remains available.
- Updated specification, flow, implementation plan, and README; corrected the README's obsolete statement that search results include topic links. Updated existing conversation coverage for numeric/arbitrary detail queries, exact buttons, stale controls, and /cancel.
- Changes remain uncommitted pending user review. Validation and local runtime status follow below.
- Validation: all 51 conversation tests passed; Ruff lint/format and whitespace checks passed. Stopped the previous bot process and restarting the updated app for the ongoing Telegram review; send /start for fresh controls. No commit made.

- User approved committing the simplified torrent detail controls and text-to-search behavior. Validation: 51 conversation tests passed, with Ruff lint/format and whitespace checks passed. Next planned step remains Transmission integration (step 5), pending user instruction.

## Step 5 review checkpoint — Transmission integration

- User authorized implementing step 5. Inspected the installed app and made read-only RPC probes: Transmission 4.1.3, RPC version 19, minimum 14, semantic API version 6.0.1.
- Added a standalone async TransmissionClient with settings factory, session/version negotiation, API 6.x JSON-RPC and legacy RPC 14–17 support, bounded CSRF token rotation, optional Basic authentication/download directory, response ID checks, timeouts/size bounds, and no redirect/proxy/cookie forwarding. Adjusted aiohttp minimum to 3.14 for its current authentication helper; no new dependency is introduced.
- Added bounded canonical bencode and BitTorrent v1 validation for single/multifile torrents, declared sizes/pieces, file paths, and the exact original info dictionary SHA-1. Unsupported v2/hybrid and symlink metainfo fail explicitly. Raw torrent bytes/passkeys are excluded from representations and diagnostic errors.
- add_torrent requires an expected persisted hash, revalidates bytes, submits base64 metainfo, checks returned identity, and distinguishes ADDED, DUPLICATE, and FOUND after reconciliation. Lost/invalid responses, unexpected HTTP failures and timeouts trigger lookup, never blind add replay. Missing/failed lookups leave SubmissionUncertain; the client blocks repeated adds for that hash. Cancellation leaves it blocked too. Durable cross-restart enforcement and user confirmation wiring remain step 6.
- Diagnostics omit raw RPC payloads/errors, credentials, session headers, metainfo, paths, and names. Reused RPC URL validation between configuration and the adapter. Updated specification, implementation plan, and README, including the previously stale core-workflow statement about result URLs.
- Tests use synthetic torrents and an ephemeral fake Transmission server; live validation of the actual implemented client used only session_info and find_torrent for a nonexistent hash. Modern protocol negotiation and the missing result passed against the real daemon. No live torrent download/add was performed.
- Full-suite result before final documentation/log metadata updates: 308 tests passed with no warnings; lint passed. Final checks are recorded below. Changes remain uncommitted pending user review.

- Final verification: full regression suite 308 passed; after final diagnostic and dependency metadata changes, all 142 focused validator/RPC/configuration tests passed. Ruff lint and formatting, offline lock regeneration, and git whitespace checks passed. Locked dependency versions are unchanged (aiohttp 3.14.5); only its supported minimum increased. No live add, torrent retrieval, or Telegram Download wiring was performed. Step 5 is ready for review and remains uncommitted; next action is user review before commit, followed by step 6 when authorized.

- User approved committing step 5. Included the Transmission client, v1 metainfo validator, synthetic/fake-server tests, shared configuration validation, dependency metadata, and documentation. Validation remains 308 full-suite tests passed and 142 final focused tests passed, with lint/format and whitespace checks passed. Step 6 has not started.
