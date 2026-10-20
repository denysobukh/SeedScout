"""Establish a RuTracker browser session, then transfer it to the HTTP client in memory."""

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
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
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel="chrome",
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = await browser.new_context()
                page = await context.new_page()
                documents = []

                def record_response(response):
                    if (
                        response.request.resource_type == "document"
                        and response.frame == page.main_frame
                        and urlsplit(response.url).hostname == "rutracker.org"
                    ):
                        documents.append(response)

                page.on("response", record_response)
                await page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                form = page.locator("#login-form-full")
                await form.locator('input[type="password"]').wait_for(
                    state="visible", timeout=90000
                )
                await form.locator('input[type="text"]').first.wait_for(
                    state="visible", timeout=10000
                )
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
            finally:
                await browser.close()
    except PlaywrightError:
        raise BrowserSessionError(
            "Browser verification failed; check installed Google Chrome and local display access"
        ) from None
