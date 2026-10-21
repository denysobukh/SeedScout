import asyncio
import base64
import hashlib
import logging
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from tg_torrent_downloader.config import Settings
from tg_torrent_downloader.torrent import InvalidTorrent
from tg_torrent_downloader.transmission import (
    AddStatus,
    SubmissionUncertain,
    TransmissionAuthenticationError,
    TransmissionClient,
    TransmissionProtocolError,
    TransmissionRejected,
    TransmissionUnavailable,
    UnsupportedTransmission,
)

# Canonical synthetic v1 file, never submitted to a real daemon.
INFO = b"d6:lengthi1e4:name8:test.bin12:piece lengthi1e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"
DATA = b"d4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO, usedforsecurity=False).hexdigest()


@asynccontextmanager
async def server(handler):
    app = web.Application()

    async def dispatch(request):
        return await handler(request)

    app.router.add_post("/transmission/rpc", dispatch)
    runner = web.AppRunner(app, shutdown_timeout=0.2)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/transmission/rpc"
    finally:
        await runner.cleanup()


class Daemon:
    def __init__(self, *, modern=True):
        self.modern = modern
        self.token = "private-session-token"
        self.requests = []
        self.received_headers = []
        self.adds = 0
        self.queries = 0
        self.present = False
        self.duplicate = False
        self.failure = None
        self.fail_lookup = False
        self.rpc_version = 19 if modern else 17
        self.minimum = 14
        self.semver = "6.0.1"
        self.version = "4.1.3" if modern else "4.0.6"
        self.after_add = None

    def torrent(self):
        return {
            "id": 7,
            "name": "test.bin",
            "hash_string" if self.modern else "hashString": HASH.upper(),
        }

    def reply(self, request, result):
        if "jsonrpc" in request:
            return web.json_response({"jsonrpc": "2.0", "id": request["id"], "result": result})
        return web.json_response({"tag": request["tag"], "result": "success", "arguments": result})

    async def __call__(self, request):
        payload = await request.json()
        self.requests.append(payload)
        self.received_headers.append(dict(request.headers))
        if request.headers.get("X-Transmission-Session-Id") != self.token:
            return web.Response(status=409, headers={"X-Transmission-Session-Id": self.token})
        method = payload["method"]
        if method == "session-get":
            return self.reply(
                payload,
                {
                    "version": self.version,
                    "rpc-version": self.rpc_version,
                    "rpc-version-minimum": self.minimum,
                    "rpc-version-semver": self.semver,
                },
            )
        expected_method = "torrent_get" if self.modern else "torrent-get"
        if method == expected_method:
            self.queries += 1
            if self.fail_lookup:
                return web.Response(status=500, text="private-response")
            return self.reply(payload, {"torrents": [self.torrent()] if self.present else []})
        assert method == ("torrent_add" if self.modern else "torrent-add")
        self.adds += 1
        if self.failure == "reject":
            if self.modern:
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": 4, "message": "private-response"},
                    }
                )
            return web.json_response({"tag": payload["tag"], "result": "private-response"})
        if self.failure == "auth":
            return web.Response(status=401, text="private-response")
        if self.failure == "timeout-absent":
            await asyncio.sleep(0.15)
            return self.reply(payload, {})
        self.present = True
        if self.failure == "timeout-present":
            await asyncio.sleep(0.15)
        if self.failure == "disconnect":
            request.transport.close()
            return web.Response()
        if self.failure == "malformed":
            return web.Response(text="private-response")
        if self.failure == "http-error":
            return web.Response(status=500, text="private-response")
        if self.failure == "wrong-hash":
            value = self.torrent()
            value["hash_string" if self.modern else "hashString"] = "b" * 40
        else:
            value = self.torrent()
        if self.after_add:
            return self.after_add(payload, value)
        key = "torrent_duplicate" if self.duplicate else "torrent_added"
        if not self.modern:
            key = key.replace("_", "-")
        return self.reply(payload, {key: value})


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize("duplicate", [True, False])
def test_negotiation_submission_duplicates_and_optional_directory(modern, duplicate):
    async def run():
        daemon = Daemon(modern=modern)
        daemon.duplicate = duplicate
        async with server(daemon) as url:
            async with TransmissionClient(url, download_dir="/remote/downloads") as client:
                info = await client.session_info()
                assert info.modern is modern and info.rpc_version == daemon.rpc_version
                result = await client.add_torrent(DATA, expected_info_hash=HASH.upper())
                assert result.status == (AddStatus.DUPLICATE if duplicate else AddStatus.ADDED)
                assert (result.torrent.id, result.torrent.name, result.torrent.info_hash) == (
                    7,
                    "test.bin",
                    HASH,
                )
                assert (await client.find_torrent(HASH)).id == 7
            assert client._session is None
        assert daemon.requests[0] == daemon.requests[1]  # CSRF replay only.
        payload = next(
            p for p in daemon.requests if p["method"].startswith("torrent") and "add" in p["method"]
        )
        args = payload["params" if modern else "arguments"]
        assert base64.b64decode(args["metainfo"]) == DATA
        assert args["download_dir" if modern else "download-dir"] == "/remote/downloads"
        assert "filename" not in args
        assert daemon.adds == 1

    asyncio.run(run())


def test_default_directory_is_omitted_and_absence_is_explicit():
    async def run():
        daemon = Daemon()
        async with server(daemon) as url, TransmissionClient(url) as client:
            assert await client.find_torrent(HASH) is None
            await client.add_torrent(DATA, expected_info_hash=HASH)
        assert set(daemon.requests[-1]["params"]) == {"metainfo"}

    asyncio.run(run())


def test_session_rotation_and_basic_auth_without_cookie_persistence(caplog):
    async def run():
        daemon = Daemon()
        async with server(daemon) as url:
            async with TransmissionClient(
                url, username="rpc-user", password="rpc-password"
            ) as client:
                await client.session_info()
                daemon.token = "rotated-private-token"
                await client.find_torrent(HASH)
        auth = "Basic " + base64.b64encode(b"rpc-user:rpc-password").decode()
        assert all(headers.get("Authorization") == auth for headers in daemon.received_headers)
        assert all("Cookie" not in headers for headers in daemon.received_headers)
        assert daemon.queries == 1

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "transmission.request" in caplog.text
    assert all(
        secret not in caplog.text
        for secret in (
            "rpc-user",
            "rpc-password",
            "private-token",
            "metainfo",
            "private-session-token",
            base64.b64encode(DATA).decode(),
        )
    )


@pytest.mark.parametrize(
    "mode", ["timeout-present", "disconnect", "malformed", "wrong-hash", "http-error"]
)
def test_ambiguous_add_is_looked_up_without_replay(mode):
    async def run():
        daemon = Daemon()
        daemon.failure = mode
        async with server(daemon) as url, TransmissionClient(url, timeout=0.05) as client:
            result = await client.add_torrent(DATA, expected_info_hash=HASH)
            assert result.status == AddStatus.FOUND
            assert result.torrent.info_hash == HASH
        assert daemon.adds == 1 and daemon.queries == 1

    asyncio.run(run())


@pytest.mark.parametrize("lookup_fails", [True, False])
def test_unresolved_add_blocks_subsequent_add_and_can_later_be_reconciled(lookup_fails):
    async def run():
        daemon = Daemon()
        daemon.failure = "timeout-absent"
        daemon.fail_lookup = lookup_fails
        async with server(daemon) as url, TransmissionClient(url, timeout=0.05) as client:
            for _ in range(2):
                with pytest.raises(SubmissionUncertain) as caught:
                    await client.add_torrent(DATA, expected_info_hash=HASH)
                assert caught.value.info_hash == HASH
            assert daemon.adds == 1
            daemon.fail_lookup = False
            daemon.present = True
            result = await client.add_torrent(DATA, expected_info_hash=HASH)
            assert result.status == AddStatus.FOUND
            assert daemon.adds == 1

    asyncio.run(run())


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize(
    "failure,exception",
    [("reject", TransmissionRejected), ("auth", TransmissionAuthenticationError)],
)
def test_explicit_rejection_is_safe_and_never_replayed(modern, failure, exception, caplog):
    async def run():
        daemon = Daemon(modern=modern)
        daemon.failure = failure
        async with server(daemon) as url, TransmissionClient(url) as client:
            with pytest.raises(exception) as caught:
                await client.add_torrent(DATA, expected_info_hash=HASH)
            assert "private-response" not in str(caught.value)
        assert daemon.adds == 1 and daemon.queries == 0

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "private-response" not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404, 500, 302])
def test_http_errors_and_redirects_are_not_followed(status):
    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.Response(status=status, headers={"Location": "/secret"})

        async with server(handler) as url, TransmissionClient(url) as client:
            expected = (
                TransmissionAuthenticationError if status in {401, 403} else TransmissionUnavailable
            )
            with pytest.raises(expected):
                await client.session_info()
        assert calls == ["/transmission/rpc"]

    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "same", "rotating"])
def test_session_negotiation_has_a_bounded_retry_limit(token):
    async def run():
        calls = []

        async def handler(request):
            calls.append(1)
            headers = (
                {}
                if token is None
                else {
                    "X-Transmission-Session-Id": str(len(calls)) if token == "rotating" else token
                }
            )
            return web.Response(status=409, headers=headers)

        async with server(handler) as url, TransmissionClient(url) as client:
            with pytest.raises(TransmissionProtocolError):
                await client.session_info()
        assert len(calls) <= 3

    asyncio.run(run())


@pytest.mark.parametrize("version,semver", [(13, "5.0.0"), (20, "7.0.0")])
def test_unsupported_rpc_versions_are_rejected_before_add(version, semver):
    async def run():
        daemon = Daemon()
        daemon.rpc_version = version
        daemon.minimum = min(14, version)
        daemon.semver = semver
        async with server(daemon) as url, TransmissionClient(url) as client:
            with pytest.raises(UnsupportedTransmission):
                await client.add_torrent(DATA, expected_info_hash=HASH)
        assert daemon.adds == 0

    asyncio.run(run())


def test_invalid_torrent_and_wrong_hash_never_reach_rpc():
    async def run():
        daemon = Daemon()
        async with server(daemon) as url, TransmissionClient(url) as client:
            with pytest.raises(InvalidTorrent):
                await client.add_torrent(b"<html>private</html>", expected_info_hash=HASH)
            with pytest.raises(InvalidTorrent):
                await client.add_torrent(DATA, expected_info_hash="b" * 40)
            with pytest.raises(ValueError):
                await client.find_torrent("private-invalid-hash")
        assert not daemon.requests

    asyncio.run(run())


def test_response_limits_bad_envelopes_and_wrong_lookup_identity():
    async def run():
        mode = ["large"]

        async def handler(request):
            await request.json()
            if mode[0] == "large":
                return web.Response(body=b" " * (1024 * 1024 + 1))
            if mode[0] == "tag":
                return web.json_response({"result": "success", "tag": -1, "arguments": {}})
            return web.json_response([])

        async with server(handler) as url, TransmissionClient(url) as client:
            for case in ("large", "tag", "list"):
                mode[0] = case
                with pytest.raises(TransmissionProtocolError):
                    await client.session_info()
        daemon = Daemon()
        daemon.present = True
        daemon.torrent = lambda: {"id": 7, "name": "test.bin", "hash_string": "b" * 40}
        async with server(daemon) as url, TransmissionClient(url) as client:
            with pytest.raises(TransmissionProtocolError):
                await client.find_torrent(HASH)

    asyncio.run(run())


def test_cancellation_leaves_add_blocked_until_reconciled():
    async def run():
        daemon = Daemon()
        daemon.failure = "timeout-absent"
        async with server(daemon) as url, TransmissionClient(url) as client:
            task = asyncio.create_task(client.add_torrent(DATA, expected_info_hash=HASH))
            for _ in range(100):
                if daemon.adds:
                    break
                await asyncio.sleep(0.001)
            assert daemon.adds == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(SubmissionUncertain):
                await client.add_torrent(DATA, expected_info_hash=HASH)
            assert daemon.adds == 1

    asyncio.run(run())


def test_settings_factory_and_closed_client_usage():
    settings = Settings.load(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
        },
        env_file=None,
    )
    client = TransmissionClient.from_settings(settings)
    assert client._url == settings.transmission_rpc_url
    with pytest.raises(RuntimeError):
        asyncio.run(client.session_info())


@pytest.mark.parametrize(
    "kwargs", [{"username": "user"}, {"download_dir": ""}, {"timeout": 0}, {"timeout": 301}]
)
def test_invalid_client_configuration(kwargs):
    with pytest.raises(ValueError):
        TransmissionClient("http://localhost:9091/transmission/rpc", **kwargs)


@pytest.mark.parametrize(
    "mode", ["wrong-id", "missing-version", "invalid-error", "both-statuses", "missing-status"]
)
def test_malformed_add_responses_are_reconciled_without_another_add(mode):
    async def run():
        daemon = Daemon()

        def response(payload, torrent):
            envelope = {"jsonrpc": "2.0", "id": payload["id"], "result": {"torrent_added": torrent}}
            if mode == "wrong-id":
                envelope["id"] = -1
            elif mode == "missing-version":
                del envelope["jsonrpc"]
            elif mode == "invalid-error":
                envelope["error"] = {"message": "private-response"}
            elif mode == "both-statuses":
                envelope["result"]["torrent_duplicate"] = torrent
            else:
                envelope["result"] = {}
            return web.json_response(envelope)

        daemon.after_add = response
        async with server(daemon) as url, TransmissionClient(url) as client:
            result = await client.add_torrent(DATA, expected_info_hash=HASH)
            assert result.status == AddStatus.FOUND
        assert daemon.adds == 1 and daemon.queries == 1

    asyncio.run(run())
