# Confirmed RuTracker interaction flow

The user approved this flow. Download is the explicit confirmation; no extra confirmation screen is required.

1. **Check authentication** by fetching the homepage (`https://rutracker.org/`)
   - Valid session → 4.
   - Missing or expired session → 2.
2. **Attempt login** using the homepage form (`login_username`, `login_password`), posting configured credentials to `/forum/login.php`. Do not GET the dedicated login page.
   - Successful → 4 (or resume the operation interrupted by expiry).
   - CAPTCHA required → 3.
   - Invalid credentials → explain the error and stop.
   - Network error → offer Retry → 1, or Cancel → stop.
3. **Resolve CAPTCHA**
   - Send its image to the requesting Telegram user and ask for the answer.
   - Submit the answer with the login form.
   - Successful → 4, or resume the interrupted operation.
   - Incorrect answer/refreshed challenge → send the new image and repeat 3.
   - Expired challenge → 2. Cancel → stop.
   - Keep challenge state private to the initiating user and chat. Do not log answers, form values, or cookies.
4. **Request query**
   - Prompt for a query and use the submitted text unchanged as the search value.
   - Nonempty input → 5. Empty input → remain at 4. Cancel → stop.
5. **Search RuTracker**
   - Matches → 6. No matches → explain and return to 4.
   - Session expired → 2–3, then retry 5 with the saved query.
   - Network error → Retry → 5, New search → 4, or Cancel → stop.
6. **Display first 10 results**
   - Preserve source order; number results 1–10, or fewer when fewer exist.
   - Include title, size, and seed count when available.
   - Valid result number → 7. Invalid number → explain the valid range, stay at 6.
   - New search → 4. Cancel → stop. Pagination is deferred.
7. **Open selected torrent topic**
   - Follow its stored RuTracker topic link; retrieve the full release description.
   - Show the description as safe text, retaining useful links, split across Telegram messages when needed; include the source link.
   - Offer Download and Back → 8.
   - Topic unavailable → explain, return to 6.
   - Session expired → 2–3, then retry 7 with the saved topic.
   - Network error → offer Retry → 7 or Back → 6.
8. **Release decision**
   - Back → 6, preserving results and numbering.
   - Download → atomic confirmation, retrieve this exact .torrent, submit it to Transmission, and report its outcome.
   - Repeated Download clicks → report the existing operation status without another submission.

## Implementation boundaries

- Step 3: RuTracker session/login operations, user/chat-scoped CAPTCHA lifecycle, top-10 search parsing, full topic descriptions, and a separate torrent retrieval method.
- Step 5: Telegram prompts, image delivery and answer collection, numeric selection, split descriptions, and Download/Back navigation. Persist or safely reject interaction state after restart.
- Step 6: wire Download to the confirmed torrent retrieval and Transmission workflow.
- Authentication retries resume the saved operation; never silently change a query or selected topic.
