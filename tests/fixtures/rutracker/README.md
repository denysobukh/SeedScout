# RuTracker parser fixtures

These are synthetic, minimal examples of the expected phpBB/RuTracker markup, not captured authenticated pages. They contain no real usernames, passwords, cookies, CAPTCHA IDs, or copyrighted release descriptions.

Live inspection on 2026-10-21 verified that the homepage and /forum/index.php return HTTP 200 with a Windows-1251 login form containing login_username, login_password, and a login submit field, with action login.php and method POST. Earlier direct HTTP requests to the dedicated login page and a login POST returned HTTP 403 without browser clearance. The current client uses Playwright with visible Chrome to verify the full login form and obtain cf_clearance and session cookies, then transfers them with Chrome's User-Agent to aiohttp. A live check confirmed successful login-page retrieval (HTTP 200) using the transferred session; credentials were not submitted in that check.

Successful-authentication markers, CAPTCHA fields, search selectors, and description selectors still require verification against actual authenticated pages when access is available. These fixtures test expected behavior and defensive handling; they do not prove live compatibility.
