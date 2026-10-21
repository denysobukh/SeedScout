# Confirmed RuTracker interaction flow

The user approved this flow. Download is the explicit confirmation; no extra confirmation screen is required. No screen shows a Cancel button; `/cancel` remains a text command.

M. **Menu screen after /start**
   - A fresh session shows **log in**, **search**, and **downloads**, without launching browser verification. Stored credentials alone do not mean authenticated.
   - An existing authenticated session is checked on menu entry: verified → hide log in; expired/unverifiable → show log in. Keep a valid client session across navigation.
   - log in → 0–3; success → M. search → 0–3 as needed, then 4.
   - downloads → D. /start from any idle branch → M, abandoning pending selections/claims and invalidating old controls.
D. **Active Transmission downloads**
   - No RuTracker login is required. Query Transmission read-only for all torrents in the configured instance, including torrents added elsewhere.
   - List all running/queued torrents (statuses 1–6): queued for verification, verifying, queued to download, downloading, queued to seed, and seeding. Include completed seeding torrents and torrents with zero instantaneous traffic; omit only stopped torrents. Include title, status, progress, download/upload speed, and ETA. Omit Transmission IDs; append a clickable G after every title linking to Google search for the exact title.
   - Empty list → explain. RPC unavailable/invalid → explain. Every outcome offers **reload** → D with a fresh query, and **menu** → M.
   - Split long lists across messages with reload/menu buttons on the final message. Never retrieve/add/start/stop/remove torrents from this branch. Access and callback ownership checks apply.
0. **Load credentials for the allowed Telegram user**
   - Stored validated pair exists in SQLite → 1.
   - Missing credentials → 0a. Collect credentials only in private chats.
   - 0a: prompt for RuTracker login → nonempty input → 0b.
   - 0b: prompt for password → nonempty input → keep candidate pair in memory → 1.
   - Empty input → repeat the current prompt. /cancel → discard candidates and stop.
1. **Establish browser session and check authentication**
   - If no verified browser session exists, use async Playwright to launch visible Google Chrome in an isolated context. Load `/forum/index.php` with JavaScript, then `/forum/login.php`.
   - Wait for visible username/password fields in `#login-form-full`; require a matching main-frame HTTP 200 response and final login URL.
   - Import `cf_clearance`, applicable session cookies, and Chrome's actual User-Agent into the HTTP client. Preserve cookie scope, flags, and expiry; retain them in memory for later requests.
   - Check the server's authenticated state using the imported session. Valid session → M when entered via log in, otherwise 4 or the interrupted operation; login needed → 2.
   - Browser verification fails or clearance is absent/expired → offer Retry → 1 or /cancel → stop.
2. **Attempt login** using the rendered login form (`login_username`, `login_password`), POSTing the stored or newly entered credentials to `/forum/login.php` with the browser cookies and matching User-Agent.
   - Successful → save the validated pair in SQLite → M for menu log in, 4 for search, or resume the operation interrupted by expiry.
   - CAPTCHA required → 3.
   - Invalid credentials → forget the rejected stored pair, close its client, show the failure and login prompt without buttons, then loop to 0a–0b. Preserve any interrupted operation.
   - Network error → offer Retry → 1, or /cancel → stop.
   - HTTP access denied → invalidate the browser session; Retry → 1 to obtain fresh clearance. Do not automatically replay the POST.
3. **Resolve CAPTCHA**
   - Fetch the image from the validated RuTracker CAPTCHA endpoint or HTTPS static.rutracker.cc/captcha/ CDN path. CDN requests use a separate cookie-free GET session and no authentication headers. Send the image to the requesting Telegram user and ask for the answer.
   - Submit the answer with the login form.
   - Successful → save the validated pair in SQLite → M for menu log in, 4 for search, or resume the interrupted operation.
   - Incorrect answer/refreshed challenge → send the new image and repeat 3.
   - Expired challenge → 2. /cancel → stop.
   - Keep challenge state private to the initiating user and chat. Do not log answers, form values, or cookies.
4. **Request query**
   - Prompt for a query and use the submitted text unchanged as the search value.
   - Nonempty input → 5. Empty input → remain at 4. /cancel → stop.
5. **Search RuTracker**
   - Send the unchanged query with `o=10&s=2` for descending seed count. Parse all distinct topics in the returned page, sort by seeds descending, then take ten; ties retain source order and unknown seed counts come last.
   - Matches → 6. No matches → explain and return to 4.
   - Search table has no recognizable releases → show the diagnostic message without buttons and return to 4. Every nonempty text input, including numbers, is then a new query unchanged; /start, /search, /help, and /cancel remain commands.
   - Session expired → 2–3, then retry 5 with the saved query.
   - Network error → Retry → 5, New search → 4, or /cancel → stop.
6. **Display first 10 results**
   - Show the seed-ranked top ten; number results 1–10, or fewer when fewer exist.
   - Include title, size, and seed count when available; omit topic URLs from the results list.
   - Show only numbered result buttons, one for each displayed torrent, in rows of up to five; do not show New search or Cancel buttons on this screen.
   - A numbered button or typed valid displayed result number → 7.
   - Any other nonempty text, including an out-of-range number → 5 with that text as the new query unchanged.
   - /search → 4. /cancel → stop. Pagination is deferred.
7. **Open selected torrent topic**
   - Follow its stored RuTracker topic link; retrieve the full release description.
   - Show the description as safe text, retaining useful links, split across Telegram messages when needed; include the source link.
   - Offer only Download and Back → 8; omit New search and Cancel buttons. Every nonempty text input, including numbers and unknown slash-prefixed strings, starts a new search → 5 unchanged. /start, /search, /status, /help, and /cancel retain their command behavior.
   - Topic unavailable → explain, return to 6.
   - Session expired → 2–3, then retry 7 with the saved topic.
   - Network error → offer Retry → 7 or Back → 6.
8. **Release decision**
   - Back → 6, preserving results and numbering.
   - Download → atomically claim the owned, unexpired pending selection before retrieving this exact .torrent.
   - RuTracker authentication expires during retrieval → retain this claim → 2–3 → resume retrieval of the same topic. /cancel or new search abandons the unsubmitted claim as failed.
   - Validate v1 metainfo, commit the hash and submitting state, and enforce the unresolved-hash guard before add. Retrieval/validation/preflight/explicit rejection failures → fail; fresh search and confirmation are required.
   - Transmission accepts → report name/ID and acceptance; duplicate → report already present. Reconciled presence → report confirmed present without claiming new versus duplicate.
   - An ambiguous add → hash lookup; present → found, absent/unreachable → uncertain. Never replay the add while unresolved.
   - /cancel stops waiting and retrieval before submission. Once add may have started, preserve uncertainty or an acknowledged result; do not remove the queued torrent.
   - Repeated Download clicks → report the existing operation status without another submission.

9. **Restart and /status**
   - Hold the database runtime lock before recovery; reject another worker using that database.
   - Startup: interrupted claimed work → failed; submitting → uncertain; uncertain → lookup only. Presence → found; missing/offline → retain uncertainty.
   - /status → owned user/chat confirmations, including all unresolved operations and up to ten terminal results; lookup only. Stop further lookups in a pass after the first unavailable-service response.
   - Old controls never restore or re-confirm a download. /start opens the menu. All torrent bytes remain in memory.

## Implementation boundaries

- Step 3: RuTracker session/login operations, user/chat-scoped CAPTCHA lifecycle, top-10 search parsing, full topic descriptions, and a separate torrent retrieval method.
- Step 4: Telegram prompts, image delivery and answer collection, numeric selection, split descriptions, and Download/Back navigation. Persist or safely reject interaction state after restart.
- Step 6: confirmed retrieval/submission, durable hash guards, cancellation, scoped status reporting, and restart reconciliation.
- Authentication retries resume the saved operation; never silently change a query or selected topic.
