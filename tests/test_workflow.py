import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from workflow_fakes import DATA, HASH, FakeTransmission

from tg_torrent_downloader.models import (
    RuTrackerCredentials,
    TorrentResult,
)
from tg_torrent_downloader.models import (
    SelectionStatus as Status,
)
from tg_torrent_downloader.rutracker import AuthenticationRequired, NetworkError
from tg_torrent_downloader.storage import (
    Expired,
    StateError,
    Unavailable,
    UnresolvedTorrent,
    WorkflowStore,
)
from tg_torrent_downloader.transmission import (
    AddStatus,
    SubmissionUncertain,
    TransmissionAuthenticationError,
    TransmissionClient,
    TransmissionRejected,
    TransmissionTorrent,
    TransmissionUnavailable,
)
from tg_torrent_downloader.workflow import DownloadAuthenticationRequired, DownloadWorkflow


class Harness:
    def __init__(self, tmp_path):
        self.now = [1000.0]
        self.store = WorkflowStore(tmp_path / "workflow.db", clock=lambda: self.now[0])
        self.store.initialize()
        self.rpc = FakeTransmission()
        self.workflow = DownloadWorkflow(self.store, lambda: self.rpc)
        self.fetches = []
        self.fetch_error = None
        self.fetch_wait = None
        self.fetch_entered = asyncio.Event()
        self.selected = self.selection()

    def selection(self, user=1, chat=10):
        search = self.store.save_search(user, chat, "test", [TorrentResult(123, "Release")])
        return self.store.select(search.id, 0, user, chat)

    async def fetch(self, topic):
        self.fetches.append(topic)
        self.fetch_entered.set()
        if self.fetch_wait:
            await self.fetch_wait.wait()
        if self.fetch_error:
            raise self.fetch_error
        return DATA

    async def download(self, **kwargs):
        return await self.workflow.download(self.selected.id, 1, 10, self.fetch, **kwargs)

    def current(self, selected=None):
        return self.store.get_selection((selected or self.selected).id, 1, 10)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


@pytest.mark.parametrize(
    "status,stored",
    [
        (AddStatus.ADDED, Status.ACCEPTED),
        (AddStatus.DUPLICATE, Status.DUPLICATE),
        (AddStatus.FOUND, Status.FOUND),
    ],
)
def test_confirmation_identity_and_known_outcomes(h, status, stored):
    async def run():
        h.rpc.status = status

        def before_add():
            current = h.current()
            assert current.status == Status.SUBMITTING and current.info_hash == HASH

        h.rpc.before_add = before_add
        outcome = await h.download()
        assert outcome.selection.status == stored
        assert outcome.selection.transmission_id == 7
        assert h.fetches == [123] and h.rpc.adds == [(DATA, HASH)]
        await h.download()
        assert len(h.rpc.adds) == 1 and h.fetches == [123]
        assert h.rpc.closed

    asyncio.run(run())


@pytest.mark.parametrize("case", ["expired", "cancelled", "wrong-owner"])
def test_unconfirmed_invalid_or_foreign_selection_never_fetches(h, case):
    async def run():
        if case == "expired":
            h.now[0] += 901
            with pytest.raises(Expired):
                await h.download()
        elif case == "wrong-owner":
            with pytest.raises(Unavailable):
                await h.workflow.download(h.selected.id, 2, 10, h.fetch)
        else:
            h.store.cancel(h.selected.id, 1, 10)
            assert (await h.download()).selection.status == Status.CANCELLED
        assert not h.fetches and not h.rpc.adds

    asyncio.run(run())


def test_concurrent_confirmations_using_separate_coordinators_claim_only_once(h):
    async def run():
        h.fetch_wait = asyncio.Event()
        first = asyncio.create_task(h.download())
        await h.fetch_entered.wait()
        other = DownloadWorkflow(h.store, lambda: h.rpc)
        second = await other.download(h.selected.id, 1, 10, h.fetch)
        assert second.selection.status == Status.CLAIMED
        h.fetch_wait.set()
        assert (await first).selection.status == Status.ACCEPTED
        assert len(h.rpc.adds) == 1 and h.fetches == [123]

    asyncio.run(run())


def test_fresh_selection_cannot_bypass_inflight_hash_guard(h):
    async def run():
        h.rpc.wait = asyncio.Event()
        first = asyncio.create_task(h.download())
        await h.rpc.entered.wait()
        newer = h.selection()
        other_rpc = FakeTransmission()
        other = DownloadWorkflow(h.store, lambda: other_rpc)
        result = await other.download(newer.id, 1, 10, h.fetch)
        assert result.selection.status == Status.FAILED
        assert "earlier submission" in result.note
        assert not other_rpc.adds
        h.rpc.wait.set()
        assert (await first).selection.status == Status.ACCEPTED

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["fetch", "connect", "reject", "auth", "invalid-file"])
def test_known_failures_do_not_claim_acceptance(h, phase):
    async def run():
        fetch = h.fetch
        if phase == "fetch":
            h.fetch_error = NetworkError("safe error")
        elif phase == "connect":
            h.rpc.connect_error = TransmissionUnavailable("safe error")
        elif phase == "reject":
            h.rpc.add_error = TransmissionRejected("safe error")
        elif phase == "auth":
            h.rpc.add_error = TransmissionAuthenticationError("safe error")
        else:

            async def invalid(topic):
                return b"<html>private-passkey</html>"

            fetch = invalid
        outcome = await h.workflow.download(h.selected.id, 1, 10, fetch)
        assert outcome.selection.status == Status.FAILED
        assert "private-passkey" not in outcome.note
        assert h.rpc.closed or phase in {"fetch", "invalid-file"}

    asyncio.run(run())


def test_authentication_pause_keeps_claim_and_resumes_same_confirmation(h):
    async def run():
        h.fetch_error = AuthenticationRequired("expired")
        with pytest.raises(DownloadAuthenticationRequired) as caught:
            await h.download()
        claim = caught.value.selection
        assert h.current().status == Status.CLAIMED and not h.rpc.adds
        h.now[0] += 901  # Already-confirmed work can finish after search expiry.
        h.fetch_error = None
        outcome = await h.download(resume_claim=claim)
        assert outcome.selection.status == Status.ACCEPTED
        assert outcome.selection.claim_token == claim.claim_token
        assert h.fetches == [123, 123]

    asyncio.run(run())


def test_paused_confirmation_can_be_abandoned_without_submission(h):
    async def run():
        h.fetch_error = AuthenticationRequired("expired")
        with pytest.raises(DownloadAuthenticationRequired) as caught:
            await h.download()
        selected = await h.workflow.cancel(h.selected, claim=caught.value.selection)
        assert selected.status == Status.FAILED and not h.rpc.adds

    asyncio.run(run())


def test_uncertainty_survives_restart_and_resolves_only_on_presence(h):
    async def run():
        h.rpc.add_error = SubmissionUncertain(HASH)
        assert (await h.download()).selection.status == Status.UNCERTAIN
        assert (await h.download()).selection.status == Status.UNCERTAIN
        assert len(h.rpc.adds) == 1 and h.fetches == [123]
        restored = WorkflowStore(h.store.path)
        restored.initialize()
        rpc = FakeTransmission()
        resumed = DownloadWorkflow(restored, lambda: rpc)
        with restored.runtime_lock():
            outcomes = await resumed.recover_startup()
        assert outcomes[0].selection.status == Status.UNCERTAIN
        assert not rpc.adds and rpc.lookups == [HASH]
        rpc.found = TransmissionTorrent(7, "test.bin", HASH)
        outcomes = await resumed.recent(1, 10)
        assert outcomes[0].selection.status == Status.FOUND
        assert restored.get_selection(h.selected.id, 1, 10).status == Status.FOUND
        assert not await resumed.recent(2, 10)

    asyncio.run(run())


def test_startup_fails_unsubmitted_claims_and_never_replays_submitting_rows(h):
    async def run():
        claimed = h.store.claim_confirmation(h.selected.id, 1, 10)
        other = h.selection()
        submitting = h.store.claim_confirmation(other.id, 1, 10)
        h.store.begin_submission(submitting.id, submitting.claim_token, HASH)
        h.rpc.lookup_error = TransmissionUnavailable("offline")
        with h.store.runtime_lock():
            await h.workflow.recover_startup()
        assert h.current(claimed).status == Status.FAILED
        assert h.current(other).status == Status.UNCERTAIN
        assert not h.rpc.adds and not h.fetches

    asyncio.run(run())


@pytest.mark.parametrize("phase,expected", [("fetch", Status.FAILED), ("add", Status.UNCERTAIN)])
def test_cancellation_during_io_is_settled_durably(h, phase, expected):
    async def run():
        if phase == "fetch":
            h.fetch_wait = asyncio.Event()
            entered = h.fetch_entered
        else:
            h.rpc.wait = asyncio.Event()
            entered = h.rpc.entered
        task = asyncio.create_task(h.download())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.current().status == expected
        assert len(h.rpc.adds) == (1 if phase == "add" else 0)
        if phase == "add":
            assert h.rpc.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,expected",
    [
        ("claim_confirmation", Status.FAILED),
        ("begin_submission", Status.UNCERTAIN),
        ("finish", Status.ACCEPTED),
    ],
)
def test_cancellation_waits_for_inflight_database_commit(h, method, expected, monkeypatch):
    entered, release = Event(), Event()
    original = getattr(h.store, method)

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(h.store, method, delayed)

    async def run():
        task = asyncio.create_task(h.download())
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.current().status == expected
        assert len(h.rpc.adds) == (1 if method == "finish" else 0)
        if method == "claim_confirmation":
            assert not h.fetches

    asyncio.run(run())


def test_runtime_lock_rejects_second_worker_and_releases(h):
    other = WorkflowStore(h.store.path)
    with h.store.runtime_lock():
        with pytest.raises(StateError, match="Another bot"):
            with other.runtime_lock():
                pytest.fail("Second worker entered")
    with other.runtime_lock():
        assert (
            other.path.with_name(other.path.name + ".runtime.lock").stat().st_mode & 0o777 == 0o600
        )


def test_hash_guard_is_atomic_across_database_connections(h):
    other = h.selection()
    first = h.store.claim_confirmation(h.selected.id, 1, 10)
    second = h.store.claim_confirmation(other.id, 1, 10)
    barrier = Barrier(2)

    def submit(selected):
        barrier.wait()
        try:
            return (
                WorkflowStore(h.store.path)
                .begin_submission(selected.id, selected.claim_token, HASH)
                .status
            )
        except UnresolvedTorrent:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, [first, second]))
    assert statuses.count(Status.SUBMITTING) == 1 and statuses.count("blocked") == 1


def test_v2_migration_preserves_credentials_and_workflow_and_accepts_found(h):
    pair = RuTrackerCredentials("private-user", "private-password")
    h.store.save_credentials(1, pair)
    selected = h.store.claim_confirmation(h.selected.id, 1, 10)
    h.store.begin_submission(selected.id, selected.claim_token, HASH)
    with sqlite3.connect(h.store.path) as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='selections'").fetchone()[0]
        db.execute(sql.replace('"selections"', "selections_old", 1).replace(",'found'", ""))
        db.execute("INSERT INTO selections_old SELECT * FROM selections")
        db.execute("DROP TABLE selections")
        db.execute("ALTER TABLE selections_old RENAME TO selections")
        db.execute("CREATE INDEX selections_status ON selections(status)")
        db.execute("DROP TABLE search_pages")
        db.execute("DROP TABLE search_sessions")
        db.execute("PRAGMA user_version=2")
    h.store.initialize()
    assert h.store.get_credentials(1) == pair
    assert h.current().info_hash == HASH
    result = h.store.finish(
        selected.id,
        selected.claim_token,
        duplicate=False,
        found=True,
        transmission_id=7,
        transmission_name="test.bin",
    )
    assert result.status == Status.FOUND


@pytest.mark.parametrize("modern,duplicate", [(True, False), (True, True), (False, False)])
def test_full_workflow_with_real_http_adapter_and_fake_daemon(h, modern, duplicate):
    from test_transmission import Daemon, server

    async def run():
        daemon = Daemon(modern=modern)
        daemon.duplicate = duplicate
        async with server(daemon) as url:
            workflow = DownloadWorkflow(h.store, lambda: TransmissionClient(url))
            outcome = await workflow.download(h.selected.id, 1, 10, h.fetch)
            assert outcome.selection.status == (Status.DUPLICATE if duplicate else Status.ACCEPTED)
            assert outcome.selection.info_hash == HASH
        assert daemon.adds == 1

    asyncio.run(run())


def test_status_keeps_old_uncertainty_visible_beyond_recent_terminal_limit(h):
    async def run():
        claim = h.store.claim_confirmation(h.selected.id, 1, 10)
        h.store.begin_submission(claim.id, claim.claim_token, HASH)
        h.store.mark_uncertain(claim.id, claim.claim_token)
        for _ in range(12):
            other = h.selection()
            item = h.store.claim_confirmation(other.id, 1, 10)
            h.store.fail(item.id, item.claim_token)
        outcomes = await h.workflow.recent(1, 10)
        assert len(outcomes) == 11
        assert any(o.selection.id == claim.id for o in outcomes)
        assert h.rpc.lookups == [HASH]

    asyncio.run(run())


def test_offline_status_stops_after_first_failed_rpc_lookup(h):
    async def run():
        for digest in ("a" * 40, "b" * 40):
            item = h.selection()
            claim = h.store.claim_confirmation(item.id, 1, 10)
            h.store.begin_submission(claim.id, claim.claim_token, digest)
            h.store.mark_uncertain(claim.id, claim.claim_token)
        h.rpc.lookup_error = TransmissionUnavailable("offline")
        outcomes = await h.workflow.recent(1, 10)
        assert len(outcomes) == 2 and len(h.rpc.lookups) == 1
        assert all(o.selection.status == Status.UNCERTAIN for o in outcomes)

    asyncio.run(run())
