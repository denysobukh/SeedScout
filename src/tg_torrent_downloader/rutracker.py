"""RuTracker HTTP client and parsers; no Telegram or Transmission dependencies.

Use one client per Telegram user/chat workflow. Cookies and CAPTCHA form state stay
in memory, are never persisted, and are discarded on close/restart. Callers resume
their saved query/topic after AuthenticationRequired and user-assisted login.
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import uuid4

import aiohttp
from bs4 import BeautifulSoup, Tag
from yarl import URL

from tg_torrent_downloader.browser_session import BrowserSessionError, obtain_browser_session
from tg_torrent_downloader.logging import (
    diagnostic_event,
    log_state,
    log_user_input,
    safe_headers,
    safe_post_data,
    safe_url,
)
from tg_torrent_downloader.models import TorrentResult

_ORIGIN = "https://rutracker.org"
_FORUM = f"{_ORIGIN}/forum/"
_HOME = f"{_ORIGIN}/"
_MAX_HTML = 4 * 1024 * 1024
_MAX_IMAGE = 2 * 1024 * 1024
_MAX_TORRENT = 16 * 1024 * 1024


class RuTrackerError(Exception):
    """Safe user-facing error; never includes response bodies or credentials."""


class NetworkError(RuTrackerError):
    pass


class AccessDenied(RuTrackerError):
    pass


class RateLimited(RuTrackerError):
    pass


class UnexpectedPage(RuTrackerError):
    pass


class UnrecognizedSearchResults(UnexpectedPage):
    """Search returned a table with no releases the client can recognize."""


class AuthenticationRequired(RuTrackerError):
    pass


class InvalidCredentials(RuTrackerError):
    pass


class TopicUnavailable(RuTrackerError):
    pass


class CaptchaExpired(RuTrackerError):
    pass


class CaptchaUnavailable(RuTrackerError):
    """Unknown challenge or wrong owner; intentionally indistinguishable."""


@dataclass(frozen=True)
class TopicDetails:
    result: TorrentResult
    description: str


@dataclass(frozen=True, repr=False)
class CaptchaChallenge:
    id: str
    user_id: int
    chat_id: int
    image: bytes
    content_type: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class _LoginForm:
    action: str
    fields: dict[str, str]
    encoding: str
    captcha_field: str | None
    image_url: str | None


@dataclass(frozen=True, repr=False)
class _Response:
    body: bytes
    content_type: str
    charset: str | None


@dataclass(repr=False)
class _PendingCaptcha:
    challenge: CaptchaChallenge
    form: _LoginForm = field(repr=False)


def _source_url(value: str, base: str = _FORUM) -> str:
    if "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        raise UnexpectedPage("RuTracker returned an unsupported source URL")
    try:
        url = urljoin(base, value)
        parts = urlsplit(url)
        allowed = (
            parts.scheme == "https"
            and parts.hostname == "rutracker.org"
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and "\\" not in url
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise UnexpectedPage("RuTracker returned an unsupported source URL")
    return url


def _soup(response: _Response) -> BeautifulSoup:
    if response.content_type not in {"text/html", "application/xhtml+xml"}:
        raise UnexpectedPage("RuTracker returned an unexpected page format")
    # Respect HTTP charset first, then HTML declarations; legacy pages use Windows-1251.
    try:
        if response.charset:
            return BeautifulSoup(response.body.decode(response.charset), "html.parser")
        return BeautifulSoup(response.body, "html.parser", from_encoding=None)
    except UnicodeError, LookupError:
        raise UnexpectedPage("RuTracker returned an unsupported text encoding") from None


def _authenticated(page: BeautifulSoup) -> bool:
    username = page.select_one("#logged-in-username")
    if username is not None and username.get_text(strip=True):
        return True
    return (
        page.select_one('a[href*="login.php?logout"], a[href*="login.php?mode=logout"]') is not None
    )


def _login_form(page: BeautifulSoup) -> _LoginForm | None:
    scope = page.select_one("#login-form-full") or page
    username = scope.select_one('input[name="login_username"]')
    if username is None:
        return None
    form = username.find_parent("form")
    if not isinstance(form, Tag) or form.select_one('input[name="login_password"]') is None:
        raise UnexpectedPage("RuTracker login form has changed")
    action = _source_url(str(form.get("action") or "login.php"))
    if urlsplit(action).path != "/forum/login.php":
        raise UnexpectedPage("RuTracker returned an unsupported login action")
    fields = {
        str(node["name"]): str(node.get("value", ""))
        for node in form.select('input[name][type="hidden"]')
    }
    submit = form.select_one('input[name="login"], button[name="login"]')
    fields["login"] = str(submit.get("value", "Вход")) if submit else "Вход"
    fields.setdefault("redirect", "index.php")
    captcha = form.select_one('input[name^="cap_code"], input[name="captcha"]')
    image_url = None
    if captcha is not None:
        image = form.select_one('img[src*="captcha"]')
        if image is None:
            raise UnexpectedPage("RuTracker CAPTCHA image is missing")
        image_url = _source_url(str(image.get("src", "")))
    meta = page.select_one("meta[charset]")
    encoding = str(meta.get("charset")) if meta else (page.original_encoding or "windows-1251")
    if not meta:
        declaration = page.select_one('meta[http-equiv="Content-Type" i]')
        match = (
            re.search(r"charset=([\w-]+)", str(declaration.get("content", "")))
            if declaration
            else None
        )
        if match:
            encoding = match[1]
    return _LoginForm(
        action, fields, encoding, str(captcha["name"]) if captcha else None, image_url
    )


def _require_session(page: BeautifulSoup) -> None:
    if _login_form(page) is not None:
        raise AuthenticationRequired("RuTracker session expired; authenticate and resume")


def _topic_id(href: str) -> int:
    parts = urlsplit(_source_url(href))
    values = parse_qs(parts.query).get("t", [])
    if (
        parts.path != "/forum/viewtopic.php"
        or len(values) != 1
        or not re.fullmatch(r"[0-9]{1,18}", values[0])
    ):
        raise UnexpectedPage("RuTracker returned an invalid topic link")
    value = int(values[0])
    if value <= 0:
        raise UnexpectedPage("RuTracker returned an invalid topic ID")
    return value


def parse_search(response: _Response) -> tuple[TorrentResult, ...]:
    page = _soup(response)
    _require_session(page)
    table = page.select_one("#tor-tbl")
    if table is None:
        text = page.get_text(" ", strip=True).casefold()
        if any(
            marker in text
            for marker in (
                "не найдено",
                "темы не найдены",
                "ничего не найдено",
                "no results found",
            )
        ):
            return ()
        raise UnexpectedPage("RuTracker search results format has changed")
    results = []
    seen = set()
    for row in table.select("tr"):
        link = row.select_one("a.tLink[href]")
        if link is None:
            continue
        topic_id = _topic_id(str(link["href"]))
        if topic_id in seen:
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            raise UnexpectedPage("RuTracker result title is missing")
        size = row.select_one(".tor-size")
        seeds = row.select_one(".seedmed, .seed")
        seed_text = (
            seeds.get_text("", strip=True).replace(",", "").replace(" ", "").replace("\xa0", "")
            if seeds
            else ""
        )
        results.append(
            TorrentResult(
                topic_id,
                title,
                size=size.get_text(" ", strip=True) or None if size else None,
                seeds=int(seed_text) if seed_text.isascii() and seed_text.isdigit() else None,
            )
        )
        seen.add(topic_id)
        if len(results) == 10:
            break
    if not results:
        raise UnrecognizedSearchResults("RuTracker search table contains no recognizable releases")
    return tuple(results)


def _display_url(value: str, base: str) -> str | None:
    """Keep useful HTTP links as text, excluding malformed or credential-bearing URLs."""
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        url = urljoin(base, value)
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
        ):
            return url
    except ValueError:
        pass
    return None


def parse_topic(response: _Response, result: TorrentResult) -> TopicDetails:
    page = _soup(response)
    _require_session(page)
    description = page.select_one(".post_body, .postbody")
    if description is None:
        text = page.get_text(" ", strip=True).casefold()
        if any(
            marker in text
            for marker in ("тема не найдена", "тема не существует", "topic does not exist")
        ):
            raise TopicUnavailable("This RuTracker topic is unavailable")
        raise UnexpectedPage("RuTracker release description format has changed")
    for node in description.select("script, style, form, iframe"):
        node.decompose()
    # Convert links/images to safe plain text; never execute markup or fetch embedded media.
    for link in description.select("a[href]"):
        url = _display_url(str(link["href"]), result.topic_url)
        label = link.get_text(" ", strip=True)
        link.replace_with(f"{label} ({url})" if url and label and label != url else url or label)
    for image in description.select("img"):
        url = _display_url(str(image.get("src", "")), result.topic_url)
        if url:
            image.replace_with(f"[Image: {url}]")
        else:
            image.decompose()
    text = description.get_text("\n", strip=True)
    if not text:
        raise UnexpectedPage("RuTracker release description is empty")
    return TopicDetails(result, text)


class RuTrackerClient:
    """One session for one initiating user/chat; serialize operations on that session."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        user_id: int,
        chat_id: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._username = username
        self._password = password
        self._user_id = user_id
        self._chat_id = chat_id
        self._clock = clock
        self._session: aiohttp.ClientSession | None = None
        self._pending: _PendingCaptcha | None = None
        self._lock = asyncio.Lock()
        self._browser_ready = False
        self._browser_login_html: str | None = None
        self._state = "idle"
        self._topic_id: int | None = None

    async def __aenter__(self) -> RuTrackerClient:
        if self._session is not None:
            raise RuntimeError("RuTracker client is already open")
        self._session = self._new_http_session()
        return self

    def _new_http_session(self, *, jar=None, user_agent="SeedScoutBot/0.1"):
        trace = aiohttp.TraceConfig()
        trace.on_request_headers_sent.append(self._headers_sent)
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25, connect=10),
            headers={"User-Agent": user_agent},
            cookie_jar=jar,
            trace_configs=[trace],
        )

    async def _prepare_browser_session(self) -> None:
        if self._browser_ready:
            return
        if self._session is None:
            raise RuntimeError("Open the RuTracker client before browser verification")
        self._set_state("browser_verification")
        try:
            browser = await obtain_browser_session(user_id=self._user_id, chat_id=self._chat_id)
        except BrowserSessionError as error:
            raise AccessDenied(str(error)) from None
        if not browser.user_agent or any(c in browser.user_agent for c in "\r\n"):
            raise UnexpectedPage("Chrome returned an invalid User-Agent")
        jar = aiohttp.CookieJar()
        for cookie in browser.cookies:
            if cookie.domain.lstrip(".") != "rutracker.org" or not cookie.path.startswith("/"):
                raise UnexpectedPage("Chrome returned an invalid RuTracker cookie scope")
            if cookie.expires >= 0 and cookie.expires <= self._clock():
                continue
            morsels = SimpleCookie()
            try:
                morsels[cookie.name] = cookie.value
            except CookieError:
                raise UnexpectedPage("Chrome returned an invalid cookie") from None
            morsel = morsels[cookie.name]
            morsel["path"] = cookie.path
            if cookie.domain.startswith("."):
                morsel["domain"] = cookie.domain
            morsel["secure"] = cookie.secure
            morsel["httponly"] = cookie.http_only
            if cookie.expires > 0:
                morsel["max-age"] = str(max(1, int(cookie.expires - self._clock())))
            jar.update_cookies(morsels, response_url=URL("https://rutracker.org" + cookie.path))
        if "cf_clearance" not in jar.filter_cookies(URL("https://rutracker.org/forum/login.php")):
            raise UnexpectedPage("Chrome clearance is missing or expired; verify again")
        # Install the prepared jar intact, retaining host-only scope and absolute expiry.
        replacement = self._new_http_session(jar=jar, user_agent=browser.user_agent)
        await self._session.close()
        self._session = replacement
        self._browser_login_html = browser.login_html
        self._browser_ready = True
        diagnostic_event(
            "rutracker.browser.imported",
            user_id=self._user_id,
            chat_id=self._chat_id,
            state="browser_ready",
            cookie_count=len(browser.cookies),
        )

    def _set_state(self, state: str) -> None:
        self._state = state
        log_state(state, user_id=self._user_id, chat_id=self._chat_id, topic_id=self._topic_id)

    @asynccontextmanager
    async def _operation(self, stage: str, topic_id: int | None = None) -> AsyncIterator[None]:
        async with self._lock:
            self._topic_id = topic_id
            self._set_state(stage)
            try:
                yield
            except RuTrackerError as error:
                diagnostic_event(
                    "rutracker.error",
                    user_id=self._user_id,
                    chat_id=self._chat_id,
                    state=self._state,
                    topic_id=self._topic_id,
                    error=type(error).__name__,
                )
                self._set_state("error")
                raise

    async def _headers_sent(self, session, context, params) -> None:
        info = context.trace_request_ctx
        diagnostic_event(
            "rutracker.request.headers",
            user_id=self._user_id,
            chat_id=self._chat_id,
            state=self._state,
            topic_id=self._topic_id,
            request_id=info["request_id"],
            hop=info["hop"],
            method=params.method,
            url=safe_url(str(params.url)),
            headers=safe_headers(params.headers.items()),
            post_data=info["post_data"],
        )

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        async with self._lock:
            self._pending = None
            self._browser_ready = False
            self._browser_login_html = None
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        limit: int = _MAX_HTML,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        if self._session is None:
            raise RuntimeError("Use RuTrackerClient as an async context manager")
        url = _source_url(url)
        # Follow only same-origin redirects, checking BEFORE each request (including credentials).
        request_id = uuid4().hex
        for hop in range(6):
            context = {"request_id": request_id, "hop": hop, "post_data": safe_post_data(data)}
            diagnostic_event(
                "rutracker.request.start",
                user_id=self._user_id,
                chat_id=self._chat_id,
                state=self._state,
                topic_id=self._topic_id,
                request_id=request_id,
                hop=hop,
                method=method,
                url=safe_url(url),
                post_data=context["post_data"],
            )
            try:
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    trace_request_ctx=context,
                ) as response:
                    diagnostic_event(
                        "rutracker.response",
                        user_id=self._user_id,
                        chat_id=self._chat_id,
                        state=self._state,
                        topic_id=self._topic_id,
                        request_id=request_id,
                        hop=hop,
                        status=response.status,
                        headers=safe_headers(response.headers.items()),
                    )
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise UnexpectedPage("RuTracker redirect target is missing")
                        target = _source_url(location, url)
                        if method == "POST" and response.status in {307, 308}:
                            raise UnexpectedPage(
                                "RuTracker requested an unsupported login redirect"
                            )
                        url = target
                        if response.status in {301, 302, 303}:
                            method, data, headers = "GET", None, None
                        continue
                    if response.status in {401, 403}:
                        self._browser_ready = False
                        self._browser_login_html = None
                        self._pending = None
                        raise AccessDenied(
                            "RuTracker denied access; retry later or check connectivity"
                        )
                    if response.status in {404, 410}:
                        raise TopicUnavailable("The requested RuTracker page is unavailable")
                    if response.status == 429:
                        raise RateLimited("RuTracker rate limit reached; retry later")
                    if response.status >= 500:
                        raise NetworkError("RuTracker is temporarily unavailable; retry later")
                    if response.status != 200:
                        raise UnexpectedPage("RuTracker returned an unexpected HTTP response")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > limit:
                            raise UnexpectedPage("RuTracker response exceeded the allowed size")
                    return _Response(bytes(body), response.content_type, response.charset)
            except (aiohttp.ClientError, TimeoutError) as error:
                diagnostic_event(
                    "rutracker.request.failed",
                    user_id=self._user_id,
                    chat_id=self._chat_id,
                    state=self._state,
                    request_id=request_id,
                    hop=hop,
                    error=type(error).__name__,
                )
                raise NetworkError("Could not reach RuTracker; retry the operation") from None
        raise UnexpectedPage("RuTracker returned too many redirects")

    async def check_authenticated(self) -> bool:
        async with self._operation("checking_auth"):
            await self._prepare_browser_session()
            page = _soup(await self._request("GET", _HOME))
            if _authenticated(page):
                self._browser_login_html = None
                self._set_state("authenticated")
                return True
            if _login_form(page) is not None or page.select_one('a[href*="login.php"]'):
                self._set_state("authentication_required")
                return False
            raise UnexpectedPage("RuTracker session status could not be determined")

    async def ensure_authenticated(self) -> CaptchaChallenge | None:
        """Check server session first, then login, or return a pending user challenge."""
        async with self._operation("checking_auth"):
            if self._pending is not None:
                if self._pending.challenge.expires_at > self._clock():
                    self._set_state("waiting_captcha")
                    return self._pending.challenge
                self._pending = None
            await self._prepare_browser_session()
            page = _soup(await self._request("GET", _HOME))
            if _authenticated(page):
                self._browser_login_html = None
                self._set_state("authenticated")
                return None
            login = page
            if self._browser_login_html is not None:
                login = _soup(
                    _Response(self._browser_login_html.encode("utf-8"), "text/html", "utf-8")
                )
                self._browser_login_html = None
            form = _login_form(login)
            if form is None:
                raise UnexpectedPage("RuTracker login form was not found")
            if form.captcha_field:
                return await self._challenge(form)
            return await self._submit_login(form)

    async def _challenge(self, form: _LoginForm) -> CaptchaChallenge:
        if form.image_url is None:
            raise UnexpectedPage("RuTracker CAPTCHA image was not found")
        response = await self._request("GET", form.image_url, limit=_MAX_IMAGE)
        if (
            response.content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            or not response.body
        ):
            raise UnexpectedPage("RuTracker returned an invalid CAPTCHA image")
        challenge = CaptchaChallenge(
            uuid4().hex,
            self._user_id,
            self._chat_id,
            response.body,
            response.content_type,
            self._clock() + 300,
        )
        self._pending = _PendingCaptcha(challenge, form)
        self._set_state("waiting_captcha")
        return challenge

    async def _submit_login(
        self, form: _LoginForm, answer: str | None = None
    ) -> CaptchaChallenge | None:
        self._set_state("logging_in")
        fields = dict(form.fields)
        fields.update(login_username=self._username, login_password=self._password)
        if form.captcha_field:
            fields[form.captcha_field] = answer or ""
        try:
            data = urlencode(fields, encoding=form.encoding, errors="strict").encode("ascii")
        except UnicodeError, LookupError:
            raise InvalidCredentials("Login values cannot be encoded for RuTracker") from None
        response = await self._request(
            "POST",
            form.action,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": _HOME,
            },
        )
        page = _soup(response)
        if _authenticated(page):
            self._set_state("authenticated")
            return None
        text = page.get_text(" ", strip=True).casefold()
        if re.search(r"(?:неверн|неправильн).{0,80}(?:парол|имя пользователя)", text) or (
            "invalid username or password" in text
        ):
            raise InvalidCredentials("RuTracker login failed; check configured credentials")
        refreshed = _login_form(page)
        if refreshed and refreshed.captcha_field:
            return await self._challenge(refreshed)
        if refreshed:
            raise InvalidCredentials("RuTracker login failed; check configured credentials")
        raise UnexpectedPage("RuTracker login result could not be determined")

    def _owned_challenge(self, challenge_id: str, user_id: int, chat_id: int) -> _PendingCaptcha:
        pending = self._pending
        if (
            pending is None
            or pending.challenge.id != challenge_id
            or self._user_id != user_id
            or self._chat_id != chat_id
        ):
            raise CaptchaUnavailable("CAPTCHA is unavailable for this user and chat")
        if pending.challenge.expires_at <= self._clock():
            self._pending = None
            raise CaptchaExpired("CAPTCHA expired; restart authentication")
        return pending

    async def answer_captcha(
        self,
        challenge_id: str,
        answer: str,
        *,
        user_id: int,
        chat_id: int,
    ) -> CaptchaChallenge | None:
        async with self._operation("answering_captcha"):
            pending = self._owned_challenge(challenge_id, user_id, chat_id)
            if not answer.strip() or len(answer) > 128:
                raise ValueError("CAPTCHA answer must contain 1–128 characters")
            log_user_input(user_id, chat_id, answer, sensitive=True)
            # Consume once even if request outcome is unknown. Never replay a login POST.
            self._pending = None
            return await self._submit_login(pending.form, answer)

    async def cancel_captcha(self, challenge_id: str, *, user_id: int, chat_id: int) -> None:
        async with self._operation("cancelling_captcha"):
            self._owned_challenge(challenge_id, user_id, chat_id)
            self._pending = None
            self._set_state("captcha_cancelled")

    async def search(self, query: str) -> tuple[TorrentResult, ...]:
        if not query.strip():
            raise ValueError("Search query must not be empty")
        # RuTracker's legacy search field is Windows-1251; never rewrite the query.
        try:
            params = urlencode({"nm": query}, encoding="windows-1251", errors="strict")
        except UnicodeError:
            raise ValueError("RuTracker search does not support some query characters") from None
        async with self._operation("searching"):
            log_user_input(self._user_id, self._chat_id, query)
            results = parse_search(await self._request("GET", _FORUM + "tracker.php?" + params))
            self._set_state("results_ready" if results else "no_results")
            return results

    async def get_topic(self, result: TorrentResult) -> TopicDetails:
        async with self._operation("loading_topic", result.topic_id):
            details = parse_topic(await self._request("GET", result.topic_url), result)
            self._set_state("description_ready")
            return details

    async def download_torrent(self, topic_id: int) -> bytes:
        """Only the confirmed workflow may call this method; description retrieval never does."""
        result = TorrentResult(topic_id, "Torrent")
        async with self._operation("downloading_torrent", topic_id):
            response = await self._request(
                "GET",
                _FORUM + f"dl.php?t={result.topic_id}",
                limit=_MAX_TORRENT,
                headers={"Referer": result.topic_url},
            )
            if response.content_type in {"text/html", "application/xhtml+xml"}:
                _require_session(_soup(response))
                raise UnexpectedPage("RuTracker returned a page instead of a torrent file")
            if response.content_type not in {
                "application/x-bittorrent",
                "application/octet-stream",
            }:
                raise UnexpectedPage("RuTracker returned an unexpected torrent format")
            if not response.body.startswith(b"d") or not response.body.endswith(b"e"):
                raise UnexpectedPage("RuTracker returned invalid torrent bytes")
            # Full bencode validation and info-hash derivation belong to step 5.
            self._set_state("torrent_received")
            return response.body
