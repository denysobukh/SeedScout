"""RuTracker HTTP client and parsers; no Telegram or Transmission dependencies.

Use one client per Telegram user/chat workflow. Cookies and CAPTCHA form state stay
in memory, are never persisted, and are discarded on close/restart. Callers resume
their saved query/topic after AuthenticationRequired and user-assisted login.
"""

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import uuid4

import aiohttp
from bs4 import BeautifulSoup, Tag

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
    return (
        page.select_one('a[href*="login.php?logout"], a[href*="login.php?mode=logout"]') is not None
    )


def _login_form(page: BeautifulSoup) -> _LoginForm | None:
    username = page.select_one('input[name="login_username"]')
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
        raise UnexpectedPage("RuTracker search table contains no recognizable releases")
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

    async def __aenter__(self) -> RuTrackerClient:
        if self._session is not None:
            raise RuntimeError("RuTracker client is already open")
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25, connect=10),
            headers={"User-Agent": "SeedScoutBot/0.1"},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        async with self._lock:
            self._pending = None
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
        for _ in range(6):
            try:
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
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
            except aiohttp.ClientError, TimeoutError:
                raise NetworkError("Could not reach RuTracker; retry the operation") from None
        raise UnexpectedPage("RuTracker returned too many redirects")

    async def check_authenticated(self) -> bool:
        async with self._lock:
            page = _soup(await self._request("GET", _HOME))
            if _authenticated(page):
                return True
            if _login_form(page) is not None or page.select_one('a[href*="login.php"]'):
                return False
            raise UnexpectedPage("RuTracker session status could not be determined")

    async def ensure_authenticated(self) -> CaptchaChallenge | None:
        """Check server session first, then login, or return a pending user challenge."""
        async with self._lock:
            if self._pending is not None:
                if self._pending.challenge.expires_at > self._clock():
                    return self._pending.challenge
                self._pending = None
            page = _soup(await self._request("GET", _HOME))
            if _authenticated(page):
                return None
            form = _login_form(page)
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
        return challenge

    async def _submit_login(
        self, form: _LoginForm, answer: str | None = None
    ) -> CaptchaChallenge | None:
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
        async with self._lock:
            pending = self._owned_challenge(challenge_id, user_id, chat_id)
            if not answer.strip() or len(answer) > 128:
                raise ValueError("CAPTCHA answer must contain 1–128 characters")
            # Consume once even if request outcome is unknown. Never replay a login POST.
            self._pending = None
            return await self._submit_login(pending.form, answer)

    async def cancel_captcha(self, challenge_id: str, *, user_id: int, chat_id: int) -> None:
        async with self._lock:
            self._owned_challenge(challenge_id, user_id, chat_id)
            self._pending = None

    async def search(self, query: str) -> tuple[TorrentResult, ...]:
        if not query.strip():
            raise ValueError("Search query must not be empty")
        # RuTracker's legacy search field is Windows-1251; never rewrite the query.
        try:
            params = urlencode({"nm": query}, encoding="windows-1251", errors="strict")
        except UnicodeError:
            raise ValueError("RuTracker search does not support some query characters") from None
        async with self._lock:
            return parse_search(await self._request("GET", _FORUM + "tracker.php?" + params))

    async def get_topic(self, result: TorrentResult) -> TopicDetails:
        async with self._lock:
            return parse_topic(await self._request("GET", result.topic_url), result)

    async def download_torrent(self, topic_id: int) -> bytes:
        """Only the confirmed workflow may call this method; description retrieval never does."""
        result = TorrentResult(topic_id, "Torrent")
        async with self._lock:
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
            # Full bencode validation and info-hash derivation belong to step 4.
            return response.body
