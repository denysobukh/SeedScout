# RuTracker parser fixtures

These are synthetic, minimal examples of the expected phpBB/RuTracker markup, not captured authenticated pages. They contain no real usernames, passwords, cookies, CAPTCHA IDs, or copyrighted release descriptions.

Live inspection on 2026-10-21 verified that the homepage and /forum/index.php return HTTP 200 with a Windows-1251 login form containing login_username, login_password, and a login submit field, with action login.php and method POST. The dedicated login page and a login POST returned HTTP 403 from this environment. The client uses the homepage form and posts to /forum/login.php.

Successful-authentication markers, CAPTCHA fields, search selectors, and description selectors still require verification against actual authenticated pages when access is available. These fixtures test expected behavior and defensive handling; they do not prove live compatibility.
