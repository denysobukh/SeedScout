import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from tg_torrent_downloader.models import TorrentResult
from tg_torrent_downloader.rutracker import (
    AccessDenied,
    AuthenticationRequired,
    CaptchaExpired,
    CaptchaUnavailable,
    InvalidCredentials,
    NetworkError,
    RateLimited,
    RuTrackerClient,
    TopicUnavailable,
    UnexpectedPage,
    UnrecognizedSearchResults,
    _captcha_source_url,
    _Response,
    _source_url,
    parse_search,
    parse_topic,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rutracker"


def page(name, encoding="utf-8"):
    text = (FIXTURES / f"{name}.html").read_text()
    return _Response(text.encode(encoding), "text/html", encoding)


class ScriptedClient(RuTrackerClient):
    """Exercise public client behavior against fixture responses, with no network."""

    def __init__(self, responses, *, clock=lambda: 1000):
        super().__init__("test-user", "private-password", user_id=1, chat_id=-2, clock=clock)
        self.responses = list(responses)
        self.requests = []

    async def _prepare_browser_session(self):
        pass  # Browser verification and cookie handoff have separate tests.

    async def _request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


IMAGE = _Response(b"synthetic-image-bytes", "image/png", None)


def test_search_metadata_order_and_missing_fields():
    results = parse_search(page("search"))
    assert [r.topic_id for r in results] == [21, 8]
    assert results[0].title == "Тестовый релиз & Demo"
    assert results[0].seeds == 1234
    assert results[0].size == "2 GB"
    assert results[1].seeds is None
    assert results[1].quality is None


def test_search_sorts_all_distinct_topics_by_seeds_before_top_ten():
    rows = "".join(
        f'<tr><td><a class="tLink" href="viewtopic.php?t={i}">{i}</a></td>'
        f'<td class="seedmed">{i}</td></tr>'
        for i in [13, 13, *range(1, 13)]
    )
    result = parse_search(
        _Response(f'<table id="tor-tbl">{rows}</table>'.encode(), "text/html", "utf-8")
    )
    assert [r.topic_id for r in result] == list(range(13, 3, -1))
    assert [r.seeds for r in result] == list(range(13, 3, -1))


def test_empty_results_are_distinguished_from_changed_markup():
    assert parse_search(page("empty")) == ()
    for body in (b"<html>maintenance</html>", b'<table id="tor-tbl"></table>'):
        with pytest.raises(UnexpectedPage):
            parse_search(_Response(body, "text/html", "utf-8"))


def test_windows_1251_search_response():
    text = (
        (FIXTURES / "search.html").read_text().replace('charset="utf-8"', 'charset="windows-1251"')
    )
    response = _Response(text.encode("cp1251"), "text/html", None)
    assert parse_search(response)[0].title == "Тестовый релиз & Demo"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/forum/viewtopic.php?t=1",
        "//evil.example/captcha.png",
        "http://rutracker.org/forum/login.php",
        "https://user:pass@rutracker.org/forum/login.php",
        "https://rutracker.org:444/forum/login.php",
        "https://rutracker.org/forum/\nlogin.php",
        "https://rutracker.org\\@evil.example/login.php",
        "https://[invalid/login.php",
    ],
)
def test_source_urls_are_constrained(url):
    with pytest.raises(UnexpectedPage):
        _source_url(url)


def test_search_rejects_external_topic_links():
    response = _Response(
        b'<table id="tor-tbl"><tr><td><a class="tLink" '
        b'href="https://evil.example/viewtopic.php?t=1">release</a></td></tr></table>',
        "text/html",
        "utf-8",
    )
    with pytest.raises(UnexpectedPage):
        parse_search(response)


def test_topic_full_description_includes_spoilers_links_and_images_but_not_replies():
    details = parse_topic(page("topic"), TorrentResult(21, "Selected release"))
    assert details.result.topic_id == 21
    assert details.poster_url == "https://example.com/poster.png"
    assert any(
        block.expandable and "Hidden technical details" in block.text for block in details.blocks
    )
    for text in (
        "Full description",
        "Hidden technical details",
        "https://example.com/info",
        "Unsafe link label",
    ):
        assert text in details.description
    for text in ("do-not-display", "javascript:", "Reply should"):
        assert text not in details.description


@pytest.mark.parametrize(
    "parser", [lambda r: parse_search(r), lambda r: parse_topic(r, TorrentResult(21, "Release"))]
)
def test_expired_session_does_not_look_like_empty_results(parser):
    with pytest.raises(AuthenticationRequired):
        parser(page("login", "cp1251"))


def test_topic_missing_and_unexpected_content():
    with pytest.raises(TopicUnavailable):
        parse_topic(
            _Response("Тема не найдена".encode(), "text/html", "utf-8"), TorrentResult(1, "r")
        )
    with pytest.raises(UnexpectedPage):
        parse_search(_Response(b"{}", "application/json", None))


def test_valid_session_skips_login():
    async def run():
        client = ScriptedClient([page("authenticated")])
        assert await client.ensure_authenticated() is None
        assert len(client.requests) == 1
        assert client.requests[0][0] == "GET"

    asyncio.run(run())


def test_login_posts_credentials_and_preserves_form_encoding():
    async def run():
        client = ScriptedClient([page("login", "cp1251"), page("authenticated")])
        assert await client.ensure_authenticated() is None
        method, _, kwargs = client.requests[-1]
        assert method == "POST"
        values = parse_qs(kwargs["data"].decode("ascii"), encoding="cp1251")
        assert values["login_username"] == ["test-user"]
        assert values["login_password"] == ["private-password"]
        assert values["login"] == ["Вход"]

    asyncio.run(run())


def test_invalid_credentials_do_not_expose_submitted_values():
    async def run():
        client = ScriptedClient([page("login"), page("login")])
        with pytest.raises(InvalidCredentials) as exc:
            await client.ensure_authenticated()
        assert "private-password" not in str(exc.value)

    asyncio.run(run())


def test_captcha_is_scoped_consumed_once_and_can_be_refreshed():
    async def run():
        client = ScriptedClient(
            [
                page("login"),
                page("captcha"),
                IMAGE,
                page("captcha"),
                IMAGE,
                page("authenticated"),
            ]
        )
        first = await client.ensure_authenticated()
        assert first.user_id == 1 and first.chat_id == -2
        assert first.image == IMAGE.body
        assert "image" not in repr(first)
        assert await client.ensure_authenticated() == first  # no repeated login while user answers
        request_count = len(client.requests)
        for user, chat in ((2, -2), (1, -3)):
            with pytest.raises(CaptchaUnavailable):
                await client.answer_captcha(first.id, "answer", user_id=user, chat_id=chat)
        assert len(client.requests) == request_count
        second = await client.answer_captcha(first.id, "wrong", user_id=1, chat_id=-2)
        assert second.id != first.id
        with pytest.raises(CaptchaUnavailable):
            await client.answer_captcha(first.id, "old", user_id=1, chat_id=-2)
        assert await client.answer_captcha(second.id, "correct", user_id=1, chat_id=-2) is None
        sent = parse_qs(client.requests[-1][2]["data"].decode())
        assert sent["cap_sid"] == ["synthetic-challenge-1"]
        assert sent["cap_code_synthetic-challenge-1"] == ["correct"]
        with pytest.raises(CaptchaUnavailable):
            await client.answer_captcha(second.id, "again", user_id=1, chat_id=-2)

    asyncio.run(run())


def test_captcha_present_on_homepage_is_not_submitted_empty():
    async def run():
        client = ScriptedClient([page("captcha"), IMAGE])
        assert await client.ensure_authenticated() is not None
        assert all(method == "GET" for method, _, _ in client.requests)

    asyncio.run(run())


def test_expired_challenge_restart_and_cancellation():
    async def run():
        now = [1000]
        client = ScriptedClient(
            [page("captcha"), IMAGE, page("captcha"), IMAGE],
            clock=lambda: now[0],
        )
        first = await client.ensure_authenticated()
        now[0] = first.expires_at
        with pytest.raises(CaptchaExpired):
            await client.answer_captcha(first.id, "answer", user_id=1, chat_id=-2)
        second = await client.ensure_authenticated()
        assert first.id != second.id
        with pytest.raises(CaptchaUnavailable):
            await client.cancel_captcha(second.id, user_id=2, chat_id=-2)
        await client.cancel_captcha(second.id, user_id=1, chat_id=-2)
        with pytest.raises(CaptchaUnavailable):
            await client.answer_captcha(second.id, "answer", user_id=1, chat_id=-2)

    asyncio.run(run())


def test_search_uses_query_unchanged_and_never_downloads_a_torrent():
    async def run():
        client = ScriptedClient([page("search"), page("topic")])
        results = await client.search("  Тест & 2020  ")
        url = client.requests[0][1]
        params = parse_qs(urlsplit(url).query, encoding="cp1251")
        assert params == {"nm": ["  Тест & 2020  "], "o": ["10"], "s": ["2"]}
        details = await client.get_topic(results[0])
        assert details.result == results[0]
        assert "t=21" in client.requests[-1][1]
        assert not any("dl.php" in url for _, url, _ in client.requests)

    asyncio.run(run())


def test_session_expiry_requires_reauthentication_without_changing_query():
    async def run():
        client = ScriptedClient(
            [page("login"), page("login"), page("authenticated"), page("search")]
        )
        with pytest.raises(AuthenticationRequired):
            await client.search("saved query")
        assert await client.ensure_authenticated() is None
        await client.search("saved query")
        assert client.requests[0][1] == client.requests[-1][1]

    asyncio.run(run())


def test_torrent_retrieval_is_separate_and_rejects_html_and_bad_bytes():
    async def run():
        torrent = b"d4:infod4:name4:testee"
        client = ScriptedClient(
            [
                _Response(torrent, "application/x-bittorrent", None),
                page("login"),
                _Response(b"broken", "application/octet-stream", None),
            ]
        )
        assert await client.download_torrent(21) == torrent
        assert client.requests[0][1].endswith("dl.php?t=21")
        with pytest.raises(AuthenticationRequired):
            await client.download_torrent(21)
        with pytest.raises(UnexpectedPage):
            await client.download_torrent(21)

    asyncio.run(run())


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, _):
        yield self.body


class FakeResponse:
    def __init__(self, status=200, *, body=b"<html>ok</html>", headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(body)
        self.content_type = "text/html"
        self.charset = "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


def client_with_transport(responses):
    client = RuTrackerClient("test", "secret", user_id=1, chat_id=-2)
    session = FakeSession(responses)
    client._session = session
    return client, session


@pytest.mark.parametrize(
    "status,error",
    [
        (403, AccessDenied),
        (429, RateLimited),
        (404, TopicUnavailable),
        (503, NetworkError),
        (418, UnexpectedPage),
    ],
)
def test_http_errors_are_actionable(status, error):
    async def run():
        client, session = client_with_transport([FakeResponse(status)])
        with pytest.raises(error):
            await client._request("GET", "https://rutracker.org/forum/index.php")
        assert len(session.requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "error", [TimeoutError("private-data"), aiohttp.ClientError("private-data")]
)
def test_network_errors_do_not_leak_details_or_auto_retry(error):
    async def run():
        client, session = client_with_transport([error])
        with pytest.raises(NetworkError) as exc:
            await client._request("GET", "https://rutracker.org/forum/index.php")
        assert "private-data" not in str(exc.value)
        assert len(session.requests) == 1

    asyncio.run(run())


def test_same_origin_redirect_keeps_cookies_but_drops_post_data():
    async def run():
        client, session = client_with_transport(
            [
                FakeResponse(302, headers={"Location": "index.php"}),
                FakeResponse(),
            ]
        )
        await client._request("POST", "https://rutracker.org/forum/login.php", data=b"secret")
        assert session.requests[1][0] == "GET"
        assert session.requests[1][2]["data"] is None
        assert session.requests[0][2]["allow_redirects"] is False

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,target", [(302, "https://evil.example/login"), (307, "index.php"), (308, "index.php")]
)
def test_redirects_cannot_leak_credentials(status, target):
    async def run():
        client, session = client_with_transport(
            [FakeResponse(status, headers={"Location": target})]
        )
        with pytest.raises(UnexpectedPage):
            await client._request("POST", "https://rutracker.org/forum/login.php", data=b"secret")
        assert len(session.requests) == 1

    asyncio.run(run())


def test_body_size_is_bounded():
    async def run():
        client, _ = client_with_transport([FakeResponse(body=b"x" * 11)])
        with pytest.raises(UnexpectedPage, match="size"):
            await client._request("GET", "https://rutracker.org/forum/index.php", limit=10)

    asyncio.run(run())


def test_context_manager_closes_session_on_failure(monkeypatch):
    async def run():
        session = FakeSession([])
        monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
        with pytest.raises(RuntimeError, match="test failure"):
            async with RuTrackerClient("test", "secret", user_id=1, chat_id=-2):
                raise RuntimeError("test failure")
        assert session.closed

    asyncio.run(run())


def test_malformed_description_links_do_not_break_the_description():
    response = _Response(
        b'<div class="post_body">Description<a href="https://[invalid">label</a>'
        b'<a href="https://user:password@example.com">private link</a>'
        b'<img src="https://user:password@example.com/image"></div>',
        "text/html",
        "utf-8",
    )
    text = parse_topic(response, TorrentResult(1, "release")).description
    assert "Description" in text and "label" in text
    assert "password" not in text


def test_invalid_credentials_message_is_not_treated_as_another_captcha():
    async def run():
        rejection = page("captcha")
        rejection = _Response(
            rejection.body + b"invalid username or password", "text/html", "utf-8"
        )
        client = ScriptedClient([page("login"), rejection])
        with pytest.raises(InvalidCredentials):
            await client.ensure_authenticated()

    asyncio.run(run())


def test_captcha_answer_network_failure_consumes_challenge_without_replaying_login():
    async def run():
        client = ScriptedClient([page("captcha"), IMAGE, NetworkError("Could not reach RuTracker")])
        challenge = await client.ensure_authenticated()
        with pytest.raises(NetworkError):
            await client.answer_captcha(challenge.id, "answer", user_id=1, chat_id=-2)
        with pytest.raises(CaptchaUnavailable):
            await client.answer_captcha(challenge.id, "answer", user_id=1, chat_id=-2)

    asyncio.run(run())


def test_concurrent_captcha_answers_only_submit_once():
    async def run():
        client = ScriptedClient([page("captcha"), IMAGE, page("authenticated")])
        challenge = await client.ensure_authenticated()
        results = await asyncio.gather(
            *(
                client.answer_captcha(challenge.id, "answer", user_id=1, chat_id=-2)
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, CaptchaUnavailable) for result in results) == 1
        assert sum(method == "POST" for method, _, _ in client.requests) == 1

    asyncio.run(run())


def test_check_authenticated_reports_guest_and_valid_session():
    async def run():
        client = ScriptedClient([page("login"), page("authenticated")])
        assert await client.check_authenticated() is False
        assert await client.check_authenticated() is True

    asyncio.run(run())


def test_new_client_does_not_restore_old_captcha():
    async def run():
        old = ScriptedClient([page("captcha"), IMAGE])
        challenge = await old.ensure_authenticated()
        await old.close()
        fresh = ScriptedClient([])
        with pytest.raises(CaptchaUnavailable):
            await fresh.answer_captcha(challenge.id, "answer", user_id=1, chat_id=-2)
        with pytest.raises(CaptchaUnavailable):
            await old.answer_captcha(challenge.id, "answer", user_id=1, chat_id=-2)

    asyncio.run(run())


def test_authentication_reads_homepage_form_and_only_posts_to_login_endpoint():
    async def run():
        client = ScriptedClient([page("login"), page("authenticated")])
        assert await client.ensure_authenticated() is None
        assert [(method, url) for method, url, _ in client.requests] == [
            ("GET", "https://rutracker.org/"),
            ("POST", "https://rutracker.org/forum/login.php"),
        ]
        assert client.requests[1][2]["headers"]["Referer"] == "https://rutracker.org/"

    asyncio.run(run())


def test_real_http_trace_logs_actual_headers_redirect_and_denied_response(monkeypatch, caplog):
    import json
    import logging
    from urllib.parse import urljoin

    from aiohttp import web

    import tg_torrent_downloader.rutracker as module

    async def run():
        received = []

        async def login(request):
            received.append(dict(await request.post()))
            response = web.Response(status=302, headers={"Location": "/target?sid=redirect-secret"})
            response.set_cookie("session", "cookie-secret")
            return response

        async def target(request):
            assert request.cookies.get("session") == "cookie-secret"
            return web.Response(
                status=403,
                text="response-body-secret",
                headers={
                    "Set-Cookie": "session=response-cookie-secret",
                    "X-Api-Key": "response-api-secret",
                },
            )

        app = web.Application()
        app.router.add_post("/login", login)
        app.router.add_get("/target", target)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://localhost:{port}"
        # Only this test redirects the constrained production transport to a local server.
        monkeypatch.setattr(module, "_source_url", lambda value, base="": urljoin(base, value))
        try:
            async with RuTrackerClient("test", "unused", user_id=11, chat_id=22) as client:
                client._state = "logging_in"
                with pytest.raises(AccessDenied):
                    await client._request(
                        "POST",
                        base + "/login",
                        data=b"login_username=user-secret&login_password=password-secret&"
                        b"cap_code_token-secret=captcha-secret&login=Submit",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
        finally:
            await runner.cleanup()
        assert received[0]["login_password"] == "password-secret"  # redaction never alters request

    with caplog.at_level(logging.INFO, logger="tg_torrent_downloader.diagnostics"):
        asyncio.run(run())
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "tg_torrent_downloader.diagnostics"
    ]
    requests = [event for event in events if event["event"] == "rutracker.request.headers"]
    responses = [event for event in events if event["event"] == "rutracker.response"]
    assert [event["method"] for event in requests] == ["POST", "GET"]
    assert any(key.lower() == "content-length" for key, _ in requests[0]["headers"])
    assert ["Cookie", "[REDACTED]"] in requests[1]["headers"]
    assert [event["status"] for event in responses] == [302, 403]
    assert len({event["request_id"] for event in requests + responses}) == 1
    assert all(event["user_id"] == 11 and event["chat_id"] == 22 for event in events)
    assert all(event["state"] == "logging_in" for event in events)
    for secret in (
        "user-secret",
        "password-secret",
        "token-secret",
        "captcha-secret",
        "redirect-secret",
        "cookie-secret",
        "response-cookie-secret",
        "response-api-secret",
        "response-body-secret",
    ):
        assert secret not in caplog.text


def test_denied_session_is_invalidated_without_replaying_request():
    async def run():
        client, session = client_with_transport([FakeResponse(403)])
        client._browser_ready = True
        client._browser_login_html = "stale login form"
        with pytest.raises(AccessDenied):
            await client._request(
                "POST", "https://rutracker.org/forum/login.php", data=b"login=test"
            )
        assert not client._browser_ready
        assert client._browser_login_html is None
        assert len(session.requests) == 1

    asyncio.run(run())


def test_logged_in_username_is_recognized_without_a_logout_link():
    async def run():
        client = ScriptedClient([page("authenticated"), page("authenticated")])
        assert await client.ensure_authenticated() is None
        assert await client.check_authenticated() is True
        assert all(method == "GET" for method, _, _ in client.requests)

    asyncio.run(run())


def test_empty_logged_in_marker_is_not_authentication_evidence():
    async def run():
        response = _Response(
            b'<span id="logged-in-username"></span><a href="login.php">Login</a>',
            "text/html",
            "utf-8",
        )
        client = ScriptedClient([response])
        assert await client.check_authenticated() is False

    asyncio.run(run())


def test_unrecognizable_search_table_has_specific_recoverable_error():
    with pytest.raises(UnrecognizedSearchResults):
        parse_search(
            _Response(
                b'<table id="tor-tbl"><tr><td>Unsupported release</td></tr></table>',
                "text/html",
                "utf-8",
            )
        )


def test_real_captcha_cdn_url_is_accepted_and_delivered_as_challenge():
    async def run():
        client = ScriptedClient([page("captcha-cdn"), IMAGE])
        challenge = await client.ensure_authenticated()
        assert challenge.image == IMAGE.body
        assert client.requests[-1][1] == "https://static.rutracker.cc/captcha/synthetic-image.jpg"
        assert client.requests[-1][2]["captcha_image"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "https://static.rutracker.cc/private/secret.jpg",
        "https://evil.example/captcha/image.jpg",
        "http://static.rutracker.cc/captcha/image.jpg",
        "https://static.rutracker.cc:444/captcha/i.jpg",
        "https://user:secret@static.rutracker.cc/captcha/i.jpg",
        "https://rutracker.org/forum/login.php",
        "https://static.rutracker.cc/captcha/../private.jpg",
        "https://static.rutracker.cc/captcha/%2e%2e/private.jpg",
    ],
)
def test_captcha_hosts_and_paths_remain_constrained(url):
    with pytest.raises(UnexpectedPage):
        _captcha_source_url(url)


def test_captcha_cdn_is_not_a_login_or_torrent_source():
    with pytest.raises(UnexpectedPage):
        _source_url("https://static.rutracker.cc/captcha/image.jpg")


def test_cdn_transport_uses_cookie_free_session_and_no_post_data(monkeypatch):
    async def run():
        client, primary = client_with_transport([])
        primary.headers = {"User-Agent": "Chrome/Test"}
        images = FakeSession([FakeResponse(body=b"image")])
        images.responses[0].content_type = "image/jpeg"
        opened = []

        class ImageContext:
            async def __aenter__(self):
                return images

            async def __aexit__(self, *args):
                await images.close()

        def open_images(**kwargs):
            opened.append(kwargs)
            return ImageContext()

        monkeypatch.setattr(client, "_new_http_session", open_images)
        response = await client._request(
            "GET",
            "https://static.rutracker.cc/captcha/image.jpg",
            captcha_image=True,
            headers={"Cookie": "primary-secret", "Authorization": "Bearer primary-secret"},
        )
        assert response.body == b"image"
        assert isinstance(opened[0]["jar"], aiohttp.DummyCookieJar)
        assert opened[0]["user_agent"] == "Chrome/Test"
        assert not primary.requests
        assert images.requests[0][2]["data"] is None
        assert images.requests[0][2]["headers"] is None
        assert images.closed
        with pytest.raises(UnexpectedPage):
            await client._request(
                "POST",
                "https://static.rutracker.cc/captcha/image.jpg",
                data=b"private",
                captcha_image=True,
            )

    asyncio.run(run())


def test_seed_sort_preserves_ties_and_places_unknown_counts_after_zero():
    rows = "".join(
        f'<tr><td><a class="tLink" href="viewtopic.php?t={topic}">{topic}</a></td>'
        f'<td class="seedmed">{seeds}</td></tr>'
        for topic, seeds in [(1, "5"), (2, ""), (3, "0"), (4, "5"), (5, "7"), (6, "n/a")]
    )
    results = parse_search(
        _Response(f'<table id="tor-tbl">{rows}</table>'.encode(), "text/html", "utf-8")
    )
    assert [result.topic_id for result in results] == [5, 1, 4, 3, 2, 6]
    assert [result.seeds for result in results] == [7, 5, 5, 0, None, None]


def test_topic_lazy_poster_and_named_nested_spoilers_preserve_order():
    html = """<div class="post_body"><var class="postImg img-right" title="https://example.com/poster.jpg"></var>
    <p>Intro &amp; story 🙂</p>
    <div class="sp-wrap"><div class="sp-head">Наличие серий в раздаче</div>
    <div class="sp-body">Episodes 1–3</div></div>
    <p>Between sections</p>
    <div class="sp-wrap"><div class="sp-head">Media info</div><div class="sp-body">Codec details
      <div class="sp-wrap"><div class="sp-head">Nested</div>
      <div class="sp-body">Nested text</div></div>
    </div></div>
    <div class="sp-wrap"><div class="sp-head">Скриншоты</div><div class="sp-body">
      <var class="postImg" title="https://example.com/shot.jpg"></var></div></div>
    <p>After sections</p></div><div class="post_body">Reply ignored</div>"""
    details = parse_topic(
        _Response(html.encode(), "text/html", "utf-8"), TorrentResult(21, "Title")
    )
    assert details.poster_url == "https://example.com/poster.jpg"
    assert [block.expandable for block in details.blocks] == [False, True, False, True, True, False]
    quotes = [block.text for block in details.blocks if block.expandable]
    assert quotes[0].startswith("Наличие серий в раздаче") and "Episodes 1–3" in quotes[0]
    assert "Media info" in quotes[1] and "Nested text" in quotes[1]
    assert "https://example.com/shot.jpg" in quotes[2]
    assert (
        "Reply ignored" not in details.description
        and "SEEDSCOUT_SPOILER" not in details.description
    )
    assert details.description == "\n\n".join(block.text for block in details.blocks)


def test_screenshot_inside_spoiler_is_not_selected_as_poster():
    html = (
        b'<div class="post_body"><div class="sp-wrap"><div class="sp-head">Screenshots</div>'
        b'<div class="sp-body"><img src="https://example.com/shot.jpg"></div></div></div>'
    )
    details = parse_topic(_Response(html, "text/html", "utf-8"), TorrentResult(21, "Title"))
    assert details.poster_url is None
    assert details.blocks[0].expandable and "shot.jpg" in details.blocks[0].text


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "https://user:password@example.com/poster.jpg", "https://[broken"],
)
def test_unsafe_posters_are_omitted(url):
    html = f'<div class="post_body"><var class="postImg" title="{url}"></var><p>Text</p></div>'
    details = parse_topic(
        _Response(html.encode(), "text/html", "utf-8"), TorrentResult(21, "Title")
    )
    assert details.poster_url is None and details.description == "Text"


def test_poster_only_topic_and_post_image_preferred_over_decorative_image():
    html = b'<div class="post_body"><var class="postImg" title="https://example.com/poster.jpg"></var></div>'
    result = TorrentResult(21, "Title")
    details = parse_topic(_Response(html, "text/html", "utf-8"), result)
    assert details.poster_url == "https://example.com/poster.jpg" and details.description == ""
    html = html.replace(b"<var ", b'<img src="https://example.com/icon.gif"><var ')
    details = parse_topic(_Response(html, "text/html", "utf-8"), result)
    assert details.poster_url == "https://example.com/poster.jpg"
    assert "icon.gif" in details.description
