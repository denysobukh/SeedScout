"""Bounded BitTorrent v1 metainfo validation and identity from the original info bytes."""

import hashlib
import re
from dataclasses import dataclass

MAX_TORRENT_BYTES = 16 * 1024 * 1024
_MAX_LENGTH = 2**63 - 1


class InvalidTorrent(ValueError):
    """Safe metainfo validation error; never includes input bytes or tracker URLs."""


@dataclass(frozen=True, repr=False)
class ValidatedTorrent:
    data: bytes
    info_hash: str
    name: str
    total_size: int
    file_count: int


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        self.nodes = 0
        self.info_span: tuple[int, int] | None = None

    def value(self, depth: int = 0):
        self.nodes += 1
        if depth > 64 or self.nodes > 200_000 or self.position >= len(self.data):
            raise InvalidTorrent("Torrent structure is truncated or exceeds parsing limits")
        marker = self.data[self.position : self.position + 1]
        if marker == b"i":
            end = self.data.find(b"e", self.position + 1)
            raw = self.data[self.position + 1 : end]
            if (
                end < 0
                or len(raw) > 64
                or not re.fullmatch(rb"-?(0|[1-9][0-9]*)", raw)
                or raw == b"-0"
            ):
                raise InvalidTorrent("Torrent contains an invalid bencoded integer")
            self.position = end + 1
            return int(raw)
        if marker in (b"l", b"d"):
            self.position += 1
            result = [] if marker == b"l" else {}
            previous = None
            while self.data[self.position : self.position + 1] != b"e":
                if marker == b"l":
                    result.append(self.value(depth + 1))
                else:
                    key = self.value(depth + 1)
                    if not isinstance(key, bytes) or (previous is not None and key <= previous):
                        raise InvalidTorrent("Torrent dictionary keys must be unique and sorted")
                    previous = key
                    start = self.position
                    result[key] = self.value(depth + 1)
                    if depth == 0 and key == b"info":
                        self.info_span = (start, self.position)
            self.position += 1
            return result
        if b"0" <= marker <= b"9":
            colon = self.data.find(b":", self.position, self.position + 21)
            raw = self.data[self.position : colon]
            if colon < 0 or not re.fullmatch(rb"0|[1-9][0-9]*", raw):
                raise InvalidTorrent("Torrent contains an invalid byte-string length")
            length = int(raw)
            start = colon + 1
            if length > len(self.data) - start:
                raise InvalidTorrent("Torrent byte string is truncated")
            self.position = start + length
            return self.data[start : self.position]
        raise InvalidTorrent("Torrent contains invalid bencoded data")


def _component(value) -> bytes:
    if (
        not isinstance(value, bytes)
        or not value
        or value in {b".", b".."}
        or any(c in value for c in (b"/", b"\\", b"\x00"))
        or re.match(rb"^[A-Za-z]:", value)
    ):
        raise InvalidTorrent("Torrent contains an invalid file name or path component")
    return value


def _length(value) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_LENGTH:
        raise InvalidTorrent("Torrent file length must be a nonnegative 64-bit integer")
    return value


def _path(value) -> tuple[bytes, ...]:
    if not isinstance(value, list) or not value:
        raise InvalidTorrent("Torrent file paths must be nonempty lists")
    return tuple(_component(part) for part in value)


def validate_torrent(data: bytes) -> ValidatedTorrent:
    """Validate v1 torrents; reject v2/hybrid metainfo rather than deriving a wrong identity.

    No filesystem writes, URL fetches, or decode/encode round trips are performed.
    Run via asyncio.to_thread when handling downloaded files in an async workflow.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_TORRENT_BYTES:
        raise InvalidTorrent("Torrent must be nonempty bytes within the 16 MiB limit")
    decoder = _Decoder(data)
    root = decoder.value()
    if decoder.position != len(data) or not isinstance(root, dict):
        raise InvalidTorrent("Torrent must contain exactly one bencoded dictionary")
    info = root.get(b"info")
    if not isinstance(info, dict) or decoder.info_span is None:
        raise InvalidTorrent("Torrent info dictionary is missing")
    if b"meta version" in info:
        raise InvalidTorrent(
            "Only BitTorrent v1 torrents are currently supported; v2/hybrid rejected"
        )
    name = _component(info.get(b"name"))
    if b"name.utf-8" in info:
        name = _component(info[b"name.utf-8"])
    piece_length = info.get(b"piece length")
    pieces = info.get(b"pieces")
    if type(piece_length) is not int or not 0 < piece_length <= 2**31:
        raise InvalidTorrent("Torrent piece length must be a positive supported integer")
    if not isinstance(pieces, bytes) or len(pieces) % 20:
        raise InvalidTorrent("Torrent piece hashes must be a multiple of 20 bytes")
    if (b"length" in info) == (b"files" in info):
        raise InvalidTorrent("Torrent must specify exactly one of length or files")
    if b"length" in info:
        total_size = _length(info[b"length"])
        file_count = 1
    else:
        files = info[b"files"]
        if not isinstance(files, list) or not files:
            raise InvalidTorrent("Torrent files must be a nonempty list")
        paths = []
        total_size = 0
        for entry in files:
            if not isinstance(entry, dict):
                raise InvalidTorrent("Torrent file entries must be dictionaries")
            if b"symlink path" in entry:
                raise InvalidTorrent("Torrent symlink entries are not supported")
            total_size += _length(entry.get(b"length"))
            path = _path(entry.get(b"path"))
            if b"path.utf-8" in entry:
                path = _path(entry[b"path.utf-8"])
            paths.append(path)
        if total_size > _MAX_LENGTH:
            raise InvalidTorrent("Torrent total size exceeds the supported range")
        paths.sort()
        if any(right[: len(left)] == left for left, right in zip(paths, paths[1:], strict=False)):
            raise InvalidTorrent("Torrent file paths overlap or are duplicated")
        file_count = len(files)
    expected_pieces = (total_size + piece_length - 1) // piece_length
    if len(pieces) // 20 != expected_pieces:
        raise InvalidTorrent("Torrent piece count does not match the declared content size")
    start, end = decoder.info_span
    info_hash = hashlib.sha1(data[start:end], usedforsecurity=False).hexdigest()
    try:
        decoded_name = name.decode("utf-8")
    except UnicodeError:
        decoded_name = name.decode("cp1251", errors="replace")
    return ValidatedTorrent(data, info_hash, decoded_name, total_size, file_count)
