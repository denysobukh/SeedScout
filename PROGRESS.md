# Project progress

## Resume instructions

Read this file, IMPLEMENTATION_PLAN.md, and SPECIFICATION.md before making changes. Inspect git status and recent commits to reconcile the recorded state with the working tree. Follow the review gate: complete one step, request review, and commit only after approval. Update this file at every review checkpoint and after review feedback.

## Current checkpoint

- Language: Python, approved by the user.
- Repository: main; initial planning commit 9f31c21; step 1 commit 7a456bf; step 2 commit 223716a.
- Planning: specification, implementation plan, and progress file approved by the user.
- Implementation: step 2 committed. User approved the detailed RuTracker flow and authorized its documentation commit followed by step 3 implementation.
- Next action: implement step 3 against RUTRACKER_FLOW.md, validate, and stop for review before its implementation commit.
- Blocking requirements: none for step 2. Live service checks later require credentials; live torrent submissions require explicit test authorization.

## Step tracker

| Step | Work | Status |
| --- | --- | --- |
| 0 | Specification and implementation plan | Approved; included in initial commit |
| 1 | Python foundation and configuration | Approved; committed |
| 2 | Durable workflow state | Approved; committed |
| 3 | RuTracker integration | Not started |
| 4 | Transmission integration | Not started |
| 5 | Telegram search and selection | Not started |
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
