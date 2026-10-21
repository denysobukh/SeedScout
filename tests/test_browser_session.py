import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest
from yarl import URL

import tg_torrent_downloader.browser_session as module
from tg_torrent_downloader.browser_session import (
    BrowserCookie,
    BrowserSession,
    BrowserSessionError,
    obtain_browser_session,
)
from tg_torrent_downloader.rutracker import AccessDenied, RuTrackerClient, UnexpectedPage, _Response


def browser_result(*cookies):
    return BrowserSession(
        cookies
        or (
            BrowserCookie(
                "cf_clearance",
                "clearance-secret",
                ".rutracker.org",
                "/",
                True,
                True,
                time.time() + 3600,
            ),
            BrowserCookie("session", "session-secret", "rutracker.org", "/forum", True, True, -1),
        ),
        "Chrome/Test",
        '<form id="login-form-full" action="login.php"><input name="login_username">'
        '<input type="password" name="login_password"><input type="hidden" '
        'name="browser_nonce" value="synthetic-nonce"></form>',
    )


def fake_browser(monkeypatch, *, status=200, final_url=module.LOGIN_URL, cookies=None):
    username = MagicMock()
    username.first = username
    username.wait_for = AsyncMock()
    username.get_attribute = AsyncMock(return_value="login_username")
    password = MagicMock()
    password.wait_for = AsyncMock()
    password.get_attribute = AsyncMock(return_value="login_password")
    form = MagicMock()
    form.locator.side_effect = lambda selector: password if "password" in selector else username
    page = MagicMock()
    page.main_frame = object()
    page.url = final_url
    page.locator.return_value = form
    page.evaluate = AsyncMock(return_value="Chrome/Test")
    page.content = AsyncMock(return_value="<html>private login page</html>")
    callback = []
    page.on.side_effect = lambda event, function: callback.append(function)

    async def goto(url, **kwargs):
        response = SimpleNamespace(
            url=url,
            status=status if url == module.LOGIN_URL else 200,
            frame=page.main_frame,
            request=SimpleNamespace(
                resource_type="document",
                method="GET",
                all_headers=AsyncMock(return_value={"Cookie": "cookie-secret"}),
            ),
            all_headers=AsyncMock(return_value={"Set-Cookie": "cookie-secret"}),
        )
        callback[0](response)
        return response

    page.goto = AsyncMock(side_effect=goto)
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.cookies = AsyncMock(
        return_value=cookies
        if cookies is not None
        else [
            {
                "name": "cf_clearance",
                "value": "clearance-secret",
                "domain": ".rutracker.org",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": time.time() + 3600,
            }
        ]
    )
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    engine = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=engine)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "async_playwright", lambda: manager)
    return browser, page, engine, password


def test_browser_verifies_login_form_and_transfers_no_secrets_to_logs(monkeypatch, caplog):
    browser, page, engine, _ = fake_browser(monkeypatch)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(obtain_browser_session(user_id=1, chat_id=2))
    assert result.user_agent == "Chrome/Test"
    assert result.cookies[0].name == "cf_clearance"
    assert "clearance-secret" not in repr(result)
    assert "clearance-secret" not in caplog.text
    assert "cookie-secret" not in caplog.text
    assert [call.args[0] for call in page.goto.call_args_list] == [
        module.HOME_URL,
        module.LOGIN_URL,
    ]
    assert engine.chromium.launch.call_args.kwargs["channel"] == "chrome"
    assert engine.chromium.launch.call_args.kwargs["headless"] is False
    browser.close.assert_awaited_once()


@pytest.mark.parametrize(
    "options",
    [
        {"status": 403},
        {"final_url": "https://example.com/forum/login.php"},
        {"cookies": []},
    ],
)
def test_browser_rejects_unverified_page_or_missing_clearance_and_closes(monkeypatch, options):
    browser, _, _, _ = fake_browser(monkeypatch, **options)
    with pytest.raises(BrowserSessionError):
        asyncio.run(obtain_browser_session(user_id=1, chat_id=2))
    browser.close.assert_awaited_once()


def test_browser_timeout_is_safe_and_closes(monkeypatch):
    browser, _, _, password = fake_browser(monkeypatch)
    password.wait_for.side_effect = module.PlaywrightError("private-cookie-data")
    with pytest.raises(BrowserSessionError) as exc:
        asyncio.run(obtain_browser_session(user_id=1, chat_id=2))
    assert "private-cookie-data" not in str(exc.value)
    browser.close.assert_awaited_once()


def test_cookie_handoff_preserves_scope_flags_expiry_and_user_agent(monkeypatch):
    import tg_torrent_downloader.rutracker as client_module

    async def run():
        obtain = AsyncMock(return_value=browser_result())
        monkeypatch.setattr(client_module, "obtain_browser_session", obtain)
        async with RuTrackerClient("user", "pass", user_id=1, chat_id=2) as client:
            original = client._session
            await client._prepare_browser_session()
            assert original.closed
            assert client._session.headers["User-Agent"] == "Chrome/Test"
            jar = client._session.cookie_jar
            assert set(jar.filter_cookies(URL("https://rutracker.org/forum/login.php"))) == {
                "cf_clearance",
                "session",
            }
            assert "session" not in jar.filter_cookies(URL("https://rutracker.org/outside"))
            assert "session" not in jar.filter_cookies(
                URL("https://sub.rutracker.org/forum/login.php")
            )
            assert "cf_clearance" in jar.filter_cookies(URL("https://sub.rutracker.org/"))
            assert not jar.filter_cookies(URL("http://rutracker.org/forum/login.php"))
            assert not jar.filter_cookies(URL("https://example.com/"))
            values = {cookie.key: cookie for cookie in jar}
            assert values["session"]["httponly"]
            assert values["cf_clearance"]["max-age"]
            await client._prepare_browser_session()
            obtain.assert_awaited_once()
        assert client._browser_ready is False

    asyncio.run(run())


def test_rendered_login_form_used_for_http_login_with_browser_session(monkeypatch):
    import tg_torrent_downloader.rutracker as client_module

    async def run():
        monkeypatch.setattr(
            client_module, "obtain_browser_session", AsyncMock(return_value=browser_result())
        )
        async with RuTrackerClient("user", "pass", user_id=1, chat_id=2) as client:
            client._request = AsyncMock(
                side_effect=[
                    _Response(b'<a href="login.php">Login</a>', "text/html", "utf-8"),
                    _Response(b'<a href="login.php?logout=true">Logout</a>', "text/html", "utf-8"),
                ]
            )
            assert await client.ensure_authenticated() is None
            post = client._request.call_args_list[-1]
            assert post.args[:2] == ("POST", module.LOGIN_URL)
            assert parse_qs(post.kwargs["data"].decode())["browser_nonce"] == ["synthetic-nonce"]

    asyncio.run(run())


def test_browser_failure_does_not_send_login_post(monkeypatch):
    import tg_torrent_downloader.rutracker as client_module

    async def run():
        monkeypatch.setattr(
            client_module,
            "obtain_browser_session",
            AsyncMock(side_effect=BrowserSessionError("Browser verification failed")),
        )
        async with RuTrackerClient("user", "pass", user_id=1, chat_id=2) as client:
            client._request = AsyncMock()
            with pytest.raises(AccessDenied):
                await client.ensure_authenticated()
            client._request.assert_not_awaited()

    asyncio.run(run())


def test_out_of_scope_browser_cookie_is_rejected_without_changing_http_session(monkeypatch):
    import tg_torrent_downloader.rutracker as client_module

    async def run():
        bad = browser_result(
            BrowserCookie("session", "secret", "evil.example", "/", True, True, -1)
        )
        monkeypatch.setattr(client_module, "obtain_browser_session", AsyncMock(return_value=bad))
        async with RuTrackerClient("user", "pass", user_id=1, chat_id=2) as client:
            original = client._session
            with pytest.raises(UnexpectedPage):
                await client._prepare_browser_session()
            assert client._session is original and not original.closed

    asyncio.run(run())


def test_expired_clearance_is_not_imported(monkeypatch):
    import tg_torrent_downloader.rutracker as client_module

    async def run():
        expired = browser_result(
            BrowserCookie("cf_clearance", "old-secret", ".rutracker.org", "/", True, True, 0)
        )
        monkeypatch.setattr(
            client_module, "obtain_browser_session", AsyncMock(return_value=expired)
        )
        async with RuTrackerClient("user", "pass", user_id=1, chat_id=2) as client:
            with pytest.raises(UnexpectedPage, match="clearance"):
                await client._prepare_browser_session()
            assert not client._browser_ready

    asyncio.run(run())


def test_login_form_timeout_records_stage_status_and_challenge_safely(monkeypatch, caplog):
    browser, page, _, password = fake_browser(monkeypatch, status=403)
    password.wait_for.side_effect = module.PlaywrightTimeoutError("cookie=private-error-secret")
    page.evaluate = AsyncMock(
        return_value={
            "login_form": False,
            "password_field": False,
            "cloudflare_challenge": True,
            "authenticated_marker": False,
            "untrusted_content": "private-page-secret",
        }
    )
    with caplog.at_level(logging.INFO), pytest.raises(BrowserSessionError) as caught:
        asyncio.run(obtain_browser_session(user_id=1, chat_id=2))
    assert "login form did not appear in time" in str(caught.value)
    assert "verification prompt was detected" in str(caught.value)
    assert '"stage": "login_password_field"' in caplog.text
    assert '"failure": "timeout"' in caplog.text
    assert '"last_document_status": 403' in caplog.text
    assert '"cloudflare_challenge": true' in caplog.text
    assert "rutracker.browser.document" in caplog.text
    for secret in (
        "private-error-secret",
        "private-page-secret",
        "cookie-secret",
        "clearance-secret",
    ):
        assert secret not in caplog.text and secret not in str(caught.value)
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("stage", ["launch", "homepage", "login_page"])
def test_browser_failure_reports_actual_stage(monkeypatch, caplog, stage):
    browser, page, engine, _ = fake_browser(monkeypatch)
    if stage == "launch":
        engine.chromium.launch.side_effect = module.PlaywrightError("private-launch-secret")
    else:
        original = page.goto.side_effect

        async def goto(url, **kwargs):
            if url == (module.HOME_URL if stage == "homepage" else module.LOGIN_URL):
                raise module.PlaywrightTimeoutError("private-navigation-secret")
            return await original(url, **kwargs)

        page.goto.side_effect = goto
    with caplog.at_level(logging.INFO), pytest.raises(BrowserSessionError) as caught:
        asyncio.run(obtain_browser_session(user_id=1, chat_id=2))
    assert f'"stage": "{stage}"' in caplog.text
    assert "private-launch-secret" not in caplog.text + str(caught.value)
    assert "private-navigation-secret" not in caplog.text + str(caught.value)
    if stage == "launch":
        assert "Could not start Chrome" in str(caught.value)
    else:
        assert "timed out while loading RuTracker" in str(caught.value)
        browser.close.assert_awaited_once()
