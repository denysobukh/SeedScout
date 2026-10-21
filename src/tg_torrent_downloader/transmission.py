"""Async Transmission RPC adapter. Calling add_torrent requires prior workflow confirmation."""

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from enum import StrEnum

import aiohttp

from tg_torrent_downloader.config import Settings, validate_rpc_url
from tg_torrent_downloader.logging import diagnostic_event
from tg_torrent_downloader.torrent import InvalidTorrent, validate_torrent

_MAX_RESPONSE = 1024 * 1024
_SESSION_HEADER = "X-Transmission-Session-Id"


class TransmissionError(Exception):
    """Safe diagnostic; never includes raw RPC responses, credentials, or metainfo."""


class TransmissionUnavailable(TransmissionError):
    pass


class TransmissionAuthenticationError(TransmissionError):
    pass


class TransmissionProtocolError(TransmissionError):
    pass


class UnsupportedTransmission(TransmissionError):
    pass


class TransmissionRejected(TransmissionError):
    """The server explicitly rejected the operation; no add replay is performed."""


class SubmissionUncertain(TransmissionError):
    """An add may have succeeded; persist uncertainty and reconcile before another submission."""

    def __init__(self, info_hash: str) -> None:
        self.info_hash = info_hash
        super().__init__("Transmission submission is uncertain; reconcile it before retrying")


@dataclass(frozen=True)
class TransmissionInfo:
    version: str
    rpc_version: int
    modern: bool


@dataclass(frozen=True, repr=False)
class TransmissionTorrent:
    id: int
    name: str
    info_hash: str


class AddStatus(StrEnum):
    ADDED = "added"
    DUPLICATE = "duplicate"
    FOUND = "found"  # Present after an ambiguous response; new versus duplicate is unknown.


@dataclass(frozen=True, repr=False)
class AddResult:
    status: AddStatus
    torrent: TransmissionTorrent


def _hash(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", value):
        raise ValueError("A hexadecimal BitTorrent v1 info hash is required")
    return value.lower()


class TransmissionClient:
    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        download_dir: str | None = None,
        timeout: float = 30,
    ) -> None:
        validate_rpc_url(url)
        if (username is None) != (password is None):
            raise ValueError("Transmission username and password must be paired")
        if download_dir is not None and (not download_dir.strip() or "\x00" in download_dir):
            raise ValueError("Transmission download directory must be a nonempty path")
        if not 0 < timeout <= 300:
            raise ValueError("Transmission timeout must be within 0–300 seconds")
        try:
            self._authorization = (
                aiohttp.encode_basic_auth(username, password) if username is not None else None
            )
        except ValueError:
            raise ValueError("Transmission authentication configuration is invalid") from None
        self._url = url
        self._download_dir = download_dir
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._session_id: str | None = None
        self._info: TransmissionInfo | None = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._uncertain: set[str] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> TransmissionClient:
        return cls(
            settings.transmission_rpc_url,
            username=settings.transmission_username,
            password=settings.transmission_password,
            download_dir=settings.transmission_download_dir,
        )

    async def __aenter__(self) -> TransmissionClient:
        if self._session is not None:
            raise RuntimeError("Transmission client is already open")
        self._session = aiohttp.ClientSession(
            headers={"Authorization": self._authorization} if self._authorization else {},
            timeout=aiohttp.ClientTimeout(total=self._timeout, connect=min(10, self._timeout)),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._session_id = None
        self._info = None

    async def _rpc(self, method: str, arguments: dict, *, modern: bool | None = None) -> dict:
        if self._session is None:
            raise RuntimeError("Open the Transmission client before calling RPC")
        modern = self._info.modern if modern is None else modern
        self._counter += 1
        request_id = self._counter
        if modern:
            payload = {
                "jsonrpc": "2.0",
                "method": method.replace("-", "_"),
                "params": arguments,
                "id": request_id,
            }
        else:
            payload = {"method": method, "arguments": arguments, "tag": request_id}
        for attempt in range(3):
            headers = {_SESSION_HEADER: self._session_id} if self._session_id else {}
            diagnostic_event(
                "transmission.request", method=method, request_id=request_id, attempt=attempt
            )
            try:
                async with self._session.post(
                    self._url, json=payload, headers=headers, allow_redirects=False
                ) as response:
                    diagnostic_event(
                        "transmission.response",
                        method=method,
                        request_id=request_id,
                        status=response.status,
                    )
                    if response.status == 409:
                        token = response.headers.get(_SESSION_HEADER, "")
                        if not re.fullmatch(r"[!-~]{1,512}", token) or token == self._session_id:
                            raise TransmissionProtocolError(
                                "Transmission session negotiation failed"
                            )
                        self._session_id = token
                        continue  # A rejected CSRF request was not executed; replay is safe.
                    if response.status in {401, 403}:
                        raise TransmissionAuthenticationError("Transmission RPC access was denied")
                    if response.status != 200:
                        raise TransmissionUnavailable(
                            "Transmission RPC returned an unexpected HTTP status"
                        )
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE:
                            raise TransmissionProtocolError(
                                "Transmission RPC response exceeded its size limit"
                            )
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except ValueError, UnicodeError, RecursionError:
                    raise TransmissionProtocolError(
                        "Transmission RPC returned invalid JSON"
                    ) from None
                if not isinstance(decoded, dict):
                    raise TransmissionProtocolError("Transmission RPC response must be an object")
                key = "id" if modern else "tag"
                if type(decoded.get(key)) is not int or decoded[key] != request_id:
                    raise TransmissionProtocolError(
                        "Transmission RPC response identifier did not match"
                    )
                if modern:
                    if decoded.get("jsonrpc") != "2.0":
                        raise TransmissionProtocolError("Transmission JSON-RPC version is invalid")
                    if "error" in decoded:
                        error = decoded["error"]
                        if (
                            "result" in decoded
                            or not isinstance(error, dict)
                            or type(error.get("code")) is not int
                            or not isinstance(error.get("message"), str)
                        ):
                            raise TransmissionProtocolError(
                                "Transmission RPC error envelope is invalid"
                            )
                        raise TransmissionRejected("Transmission rejected the RPC operation")
                    result = decoded.get("result")
                else:
                    status = decoded.get("result")
                    if not isinstance(status, str) or not status:
                        raise TransmissionProtocolError("Transmission RPC result is missing")
                    if status != "success":
                        raise TransmissionRejected("Transmission rejected the RPC operation")
                    result = decoded.get("arguments", {})
                if not isinstance(result, dict):
                    raise TransmissionProtocolError("Transmission RPC result must be an object")
                return result
            except (aiohttp.ClientError, TimeoutError) as error:
                diagnostic_event(
                    "transmission.request.failed",
                    method=method,
                    request_id=request_id,
                    error=type(error).__name__,
                )
                raise TransmissionUnavailable("Could not reach Transmission RPC") from None
        raise TransmissionProtocolError("Transmission session negotiation exceeded its retry limit")

    async def _negotiate(self) -> TransmissionInfo:
        if self._info is None:
            # Read-only bootstrap understood by Transmission 3.x and 4.x, including 4.1.3.
            result = await self._rpc("session-get", {}, modern=False)
            version, rpc = result.get("version"), result.get("rpc-version")
            minimum = result.get("rpc-version-minimum")
            semver = result.get("rpc-version-semver", "")
            if (
                not isinstance(version, str)
                or not version
                or type(rpc) is not int
                or type(minimum) is not int
                or not 0 <= minimum <= rpc
            ):
                raise TransmissionProtocolError("Transmission version information is invalid")
            modern = (
                rpc >= 18
                and isinstance(semver, str)
                and bool(re.fullmatch(r"6\.[0-9]+\.[0-9]+", semver))
            )
            if not modern and not (14 <= rpc <= 17 and minimum <= 17):
                raise UnsupportedTransmission("Unsupported Transmission RPC version")
            self._info = TransmissionInfo(version, rpc, modern)
            diagnostic_event("transmission.connected", rpc_version=rpc, modern=modern)
        return self._info

    async def session_info(self) -> TransmissionInfo:
        async with self._lock:
            return await self._negotiate()

    def _torrent(self, value, expected: str) -> TransmissionTorrent:
        key = "hash_string" if self._info.modern else "hashString"
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or value["id"] < 0
            or not isinstance(value.get("name"), str)
            or not value["name"].strip()
            or not isinstance(value.get(key), str)
            or value[key].lower() != expected
        ):
            raise TransmissionProtocolError("Transmission torrent identity did not match")
        return TransmissionTorrent(value["id"], value["name"], expected)

    async def _find(self, info_hash: str) -> TransmissionTorrent | None:
        key = "hash_string" if self._info.modern else "hashString"
        result = await self._rpc("torrent-get", {"ids": [info_hash], "fields": ["id", "name", key]})
        torrents = result.get("torrents")
        if not isinstance(torrents, list) or len(torrents) > 1:
            raise TransmissionProtocolError(
                "Transmission torrent lookup returned an invalid result"
            )
        return self._torrent(torrents[0], info_hash) if torrents else None

    async def find_torrent(self, info_hash: str) -> TransmissionTorrent | None:
        info_hash = _hash(info_hash)
        async with self._lock:
            await self._negotiate()
            return await self._find(info_hash)

    async def _reconcile(self, info_hash: str) -> AddResult:
        try:
            torrent = await self._find(info_hash)
        except TransmissionError:
            torrent = None
        if torrent is None:
            # An empty immediate lookup does not prove the original add will never finish.
            diagnostic_event("transmission.uncertain", info_hash=info_hash)
            raise SubmissionUncertain(info_hash) from None
        self._uncertain.discard(info_hash)
        diagnostic_event("transmission.reconciled", info_hash=info_hash)
        return AddResult(AddStatus.FOUND, torrent)

    async def add_torrent(self, data: bytes, *, expected_info_hash: str) -> AddResult:
        """Validate and add bytes after the caller persists the confirmed selection/hash.

        Never automatically replay an ambiguous add. FOUND confirms presence only, without
        claiming this request created a new torrent. Cancellation propagates; the caller
        must retain submitting/uncertain state for restart recovery in step 6.
        """
        expected_info_hash = _hash(expected_info_hash)
        torrent = await asyncio.to_thread(validate_torrent, data)
        if torrent.info_hash != expected_info_hash:
            raise InvalidTorrent("Torrent identity differs from the persisted selection hash")
        async with self._lock:
            await self._negotiate()
            if expected_info_hash in self._uncertain:
                return await self._reconcile(expected_info_hash)
            modern = self._info.modern
            args = {"metainfo": base64.b64encode(torrent.data).decode("ascii")}
            if self._download_dir is not None:
                args["download_dir" if modern else "download-dir"] = self._download_dir
            self._uncertain.add(expected_info_hash)
            try:
                result = await self._rpc("torrent-add", args)
                added, duplicate = (
                    ("torrent_added", "torrent_duplicate")
                    if modern
                    else ("torrent-added", "torrent-duplicate")
                )
                if (added in result) == (duplicate in result):
                    raise TransmissionProtocolError("Transmission add result is invalid")
                status = AddStatus.ADDED if added in result else AddStatus.DUPLICATE
                accepted = self._torrent(
                    result[added if added in result else duplicate], expected_info_hash
                )
            except TransmissionRejected, TransmissionAuthenticationError:
                self._uncertain.discard(expected_info_hash)
                raise
            except TransmissionError:
                return await self._reconcile(expected_info_hash)
            self._uncertain.discard(expected_info_hash)
            diagnostic_event(
                "transmission.submitted",
                info_hash=expected_info_hash,
                status=status,
                transmission_id=accepted.id,
            )
            return AddResult(status, accepted)
