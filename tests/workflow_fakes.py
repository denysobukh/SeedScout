"""Synthetic metainfo and an in-memory RPC fake; never contact a real daemon."""

import asyncio
import hashlib

from tg_torrent_downloader.transmission import (
    AddResult,
    AddStatus,
    TransmissionInfo,
    TransmissionTorrent,
)

INFO = b"d6:lengthi1e4:name8:test.bin12:piece lengthi1e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"
DATA = b"d4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO, usedforsecurity=False).hexdigest()


class FakeTransmission:
    def __init__(self):
        self.adds, self.lookups = [], []
        self.sequential_adds = []
        self.closed = False
        self.status = AddStatus.ADDED
        self.add_error = self.connect_error = self.lookup_error = None
        self.found = None
        self.active = ()
        self.active_error = None
        self.active_calls = 0
        self.wait = None
        self.entered = asyncio.Event()
        self.before_add = None

    async def __aenter__(self):
        self.closed = False
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def session_info(self):
        if self.connect_error:
            raise self.connect_error
        return TransmissionInfo("4.1.3", 19, True)

    async def add_torrent(self, data, *, expected_info_hash, sequential=False):
        self.sequential_adds.append(sequential)
        self.adds.append((data, expected_info_hash))
        if self.before_add:
            self.before_add()
        self.entered.set()
        if self.wait:
            await self.wait.wait()
        if self.add_error:
            raise self.add_error
        return AddResult(self.status, TransmissionTorrent(7, "test.bin", expected_info_hash))

    async def find_torrent(self, info_hash):
        self.lookups.append(info_hash)
        if self.lookup_error:
            raise self.lookup_error
        return self.found

    async def active_downloads(self):
        self.active_calls += 1
        if self.active_error:
            raise self.active_error
        return self.active
