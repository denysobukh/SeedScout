"""Establish a RuTracker browser session, then transfer it to the HTTP client in memory."""

import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from tg_torrent_downloader.logging import diagnostic_event, safe_headers, safe_url

HOME_URL = "https://rutracker.org/forum/index.php"
LOGIN_URL = "https://rutracker.org/forum/login.php"


class BrowserSessionError(Exception):
    """Safe diagnostic; never includes Playwright exception text or cookie values."""


@dataclass(frozen=True, repr=False)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    secure: bool
    http_only: bool
    expires: float


@dataclass(frozen=True, repr=False)
class BrowserSession:
    cookies: tuple[BrowserCookie, ...]
    user_agent: str
    login_html: str


def _login_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == "rutracker.org"
            and parts.port in (None, 443)
            and parts.path == "/forum/login.php"
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


async def obtain_browser_session(*, user_id: int, chat_id: int) -> BrowserSession:
    """Open installed Chrome visibly, wait for verified login form, export no files.

    Uses a fresh isolated context; browser/site verification may need local user interaction.
    Does not enter credentials or solve a CAPTCHA. The HTTP login flow does that later.
    """
    started = time.monotonic()
    stage = "launch"
    page = None
    documents = []
    markers = {}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel="chrome",
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                stage = "create_page"
                context = await browser.new_context()
                page = await context.new_page()

                def record_response(response):
                    if (
                        response.request.resource_type == "document"
                        and response.frame == page.main_frame
                        and urlsplit(response.url).hostname == "rutracker.org"
                    ):
                        documents.append(response)
                        # Record navigation even when waiting for the login form later fails.
                        diagnostic_event(
                            "rutracker.browser.document",
                            user_id=user_id,
                            chat_id=chat_id,
                            state="browser_verification",
                            stage=stage,
                            url=safe_url(response.url),
                            status=response.status,
                        )

                page.on("response", record_response)
                stage = "homepage"
                await page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
                stage = "login_page"
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                stage = "login_password_field"
                form = page.locator("#login-form-full")
                await form.locator('input[type="password"]').wait_for(
                    state="visible", timeout=90000
                )
                stage = "login_username_field"
                await form.locator('input[type="text"]').first.wait_for(
                    state="visible", timeout=10000
                )
                stage = "validate_login_form"
                matching = [response for response in documents if _login_url(response.url)]
                if not matching or matching[-1].status != 200 or not _login_url(page.url):
                    raise BrowserSessionError("Chrome did not reach a verified HTTP 200 login form")
                if (
                    await form.locator('input[type="text"]').first.get_attribute("name")
                    != "login_username"
                    or await form.locator('input[type="password"]').get_attribute("name")
                    != "login_password"
                ):
                    raise BrowserSessionError("Chrome login form fields have changed")
                # Each browser document has matching request/response diagnostics.
                for response in documents:
                    request_id = uuid4().hex
                    diagnostic_event(
                        "rutracker.browser.request",
                        user_id=user_id,
                        chat_id=chat_id,
                        state="browser_verification",
                        request_id=request_id,
                        method=response.request.method,
                        url=safe_url(response.url),
                        headers=safe_headers((await response.request.all_headers()).items()),
                    )
                    diagnostic_event(
                        "rutracker.browser.response",
                        user_id=user_id,
                        chat_id=chat_id,
                        state="browser_verification",
                        request_id=request_id,
                        status=response.status,
                        headers=safe_headers((await response.all_headers()).items()),
                    )
                stage = "collect_session"
                cookies = tuple(
                    BrowserCookie(
                        name=cookie["name"],
                        value=cookie["value"],
                        domain=cookie["domain"],
                        path=cookie["path"],
                        secure=cookie["secure"],
                        http_only=cookie["httpOnly"],
                        expires=cookie["expires"],
                    )
                    for cookie in await context.cookies([HOME_URL, LOGIN_URL])
                )
                if not any(cookie.name == "cf_clearance" and cookie.value for cookie in cookies):
                    raise BrowserSessionError(
                        "Chrome did not obtain cf_clearance; retry browser verification"
                    )
                return BrowserSession(
                    cookies, await page.evaluate("navigator.userAgent"), await page.content()
                )
            except PlaywrightError, BrowserSessionError:
                # Fixed booleans only: no page content, titles, input values, or cookie data.
                if page is not None:
                    try:
                        observed = await page.evaluate("""() => ({
                            login_form: !!document.querySelector('#login-form-full'),
                            password_field: !!document.querySelector(
                                '#login-form-full input[type="password"]'),
                            cloudflare_challenge: !!document.querySelector(
                                '#challenge-running, #challenge-form, #cf-challenge-running, '
                                + 'iframe[src*="challenges.cloudflare.com"], .cf-turnstile'),
                            authenticated_marker: !!document.querySelector('#logged-in-username')
                        })""")
                        if isinstance(observed, dict):
                            markers = {
                                key: observed[key]
                                for key in (
                                    "login_form",
                                    "password_field",
                                    "cloudflare_challenge",
                                    "authenticated_marker",
                                )
                                if type(observed.get(key)) is bool
                            }
                    except PlaywrightError:
                        pass
                raise
            finally:
                await browser.close()
    except (PlaywrightError, BrowserSessionError) as error:
        timeout = isinstance(error, PlaywrightTimeoutError)
        diagnostic_event(
            "rutracker.browser.failure",
            user_id=user_id,
            chat_id=chat_id,
            state="browser_verification",
            stage=stage,
            failure="timeout"
            if timeout
            else "verification"
            if isinstance(error, BrowserSessionError)
            else "browser_error",
            elapsed_seconds=round(time.monotonic() - started, 2),
            url=safe_url(page.url) if page is not None else None,
            last_document_status=documents[-1].status if documents else None,
            **markers,
        )
        if isinstance(error, BrowserSessionError):
            raise
        if stage == "launch":
            message = (
                "Could not start Chrome for verification. Check Chrome and local display access."
            )
        elif timeout and stage in {"login_password_field", "login_username_field"}:
            message = "Chrome opened, but RuTracker's login form did not appear in time."
            if markers.get("cloudflare_challenge"):
                message += " A browser verification prompt was detected."
            message += (
                " Retry log in and complete any Chrome verification prompt "
                "while the window is open."
            )
        elif timeout:
            message = "Chrome timed out while loading RuTracker. Retry log in."
        else:
            message = "Chrome verification was interrupted. Retry log in."
        raise BrowserSessionError(message) from None
