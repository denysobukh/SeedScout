import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier

import pytest

from tg_torrent_downloader.models import SelectionStatus as Status
from tg_torrent_downloader.models import TorrentResult
from tg_torrent_downloader.storage import (
    Expired,
    InvalidTransition,
    StateError,
    Unavailable,
    WorkflowStore,
)


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = WorkflowStore(tmp_path / "runtime" / "state.db", clock=lambda: now[0])
    store.initialize()
    return store, now


def selection(store):
    search = store.save_search(1, -2, "film", [TorrentResult(123, "Film — 日本語", seeds=7)])
    return store.select(search.id, 0, 1, -2)


def claimed(store):
    item = selection(store)
    return store.claim_confirmation(item.id, 1, -2)


def test_roundtrip_preserves_results_and_selection(state):
    store, _ = state
    original = [
        TorrentResult(1, "Release", size="2 GB", quality="1080p", language="ru", format="MKV")
    ]
    search = store.save_search(1, -2, "query", original)
    original.clear()
    restored = store.get_search(search.id, 1, -2)
    assert restored == search
    item = store.select(search.id, 0, 1, -2)
    assert item.result == search.results[0]
    assert item.result.topic_url == "https://rutracker.org/forum/viewtopic.php?t=1"
    assert item.status == Status.PENDING
    assert store.select(search.id, 0, 1, -2) == item
    with pytest.raises(FrozenInstanceError):
        item.status = Status.ACCEPTED


@pytest.mark.parametrize("user,chat", [(2, -2), (1, -3)])
def test_ownership_on_every_user_operation(state, user, chat):
    store, _ = state
    item = selection(store)
    operations = [
        lambda: store.get_search(item.search_id, user, chat),
        lambda: store.select(item.search_id, 0, user, chat),
        lambda: store.get_selection(item.id, user, chat),
        lambda: store.claim_confirmation(item.id, user, chat),
        lambda: store.cancel(item.id, user, chat),
    ]
    for operation in operations:
        with pytest.raises(Unavailable):
            operation()
    assert store.get_selection(item.id, 1, -2).status == Status.PENDING


def test_unknown_ids_and_invalid_positions(state):
    store, _ = state
    with pytest.raises(Unavailable):
        store.get_search("missing", 1, -2)
    with pytest.raises(Unavailable):
        store.claim_confirmation("missing", 1, -2)
    item = selection(store)
    for position in (-1, 1, True):
        with pytest.raises(Unavailable):
            store.select(item.search_id, position, 1, -2)


def test_expiry_at_boundary_blocks_search_selection_and_confirmation(state):
    store, now = state
    item = selection(store)
    now[0] = item.expires_at
    for operation in (
        lambda: store.get_search(item.search_id, 1, -2),
        lambda: store.select(item.search_id, 0, 1, -2),
        lambda: store.claim_confirmation(item.id, 1, -2),
        lambda: store.cancel(item.id, 1, -2),
    ):
        with pytest.raises(Expired):
            operation()
    assert store.get_selection(item.id, 1, -2).status == Status.PENDING


def test_cancel_is_terminal(state):
    store, _ = state
    item = selection(store)
    assert store.cancel(item.id, 1, -2).status == Status.CANCELLED
    with pytest.raises(InvalidTransition):
        store.claim_confirmation(item.id, 1, -2)
    assert store.list_incomplete() == ()


@pytest.mark.parametrize("duplicate", [False, True])
def test_submission_and_completion_after_expiry(state, duplicate):
    store, now = state
    item = claimed(store)
    now[0] = item.expires_at + 1
    submitting = store.begin_submission(item.id, item.claim_token, "A" * 40)
    assert submitting.info_hash == "a" * 40
    assert submitting.status == Status.SUBMITTING
    done = store.finish(
        item.id,
        item.claim_token,
        duplicate=duplicate,
        transmission_id=42,
        transmission_name="Release",
    )
    assert done.status == (Status.DUPLICATE if duplicate else Status.ACCEPTED)
    assert done.transmission_id == 42
    assert done.transmission_name == "Release"
    assert done.info_hash == "a" * 40
    assert store.list_incomplete() == ()
    with pytest.raises(InvalidTransition):
        store.fail(item.id, item.claim_token)


def test_worker_claim_and_order_are_enforced(state):
    store, _ = state
    item = claimed(store)
    with pytest.raises(InvalidTransition):
        store.begin_submission(item.id, "wrong-token", "a" * 40)
    with pytest.raises(InvalidTransition):
        store.finish(
            item.id,
            item.claim_token,
            duplicate=False,
            transmission_id=1,
            transmission_name="release",
        )
    with pytest.raises(ValueError):
        store.begin_submission(item.id, item.claim_token, "not-a-hash")
    assert store.get_selection(item.id, 1, -2).status == Status.CLAIMED
    store.begin_submission(item.id, item.claim_token, "b" * 64)
    with pytest.raises(InvalidTransition):
        store.begin_submission(item.id, item.claim_token, "c" * 40)


def test_concurrent_confirmations_have_exactly_one_winner(state):
    store, _ = state
    item = selection(store)
    barrier = Barrier(8)

    def confirm(_):
        independent_store = WorkflowStore(store.path, clock=store.clock)
        barrier.wait(timeout=10)
        try:
            return independent_store.claim_confirmation(item.id, 1, -2)
        except InvalidTransition:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(confirm, range(8)))
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    assert winners[0].claim_token
    assert store.get_selection(item.id, 1, -2) == winners[0]


def test_concurrent_cancel_and_confirmation_only_one_succeeds(state):
    store, _ = state
    item = selection(store)
    barrier = Barrier(2)

    def action(cancel):
        independent = WorkflowStore(store.path, clock=store.clock)
        barrier.wait(timeout=10)
        try:
            method = independent.cancel if cancel else independent.claim_confirmation
            return method(item.id, 1, -2)
        except InvalidTransition:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(action, [False, True]))
    assert sum(outcome is not None for outcome in outcomes) == 1


def test_restart_keeps_pending_and_interrupted_work_without_reclaiming(state):
    store, _ = state
    pending = selection(store)
    worker = claimed(store)
    uncertain = claimed(store)
    store.begin_submission(uncertain.id, uncertain.claim_token, "c" * 40)
    store.mark_uncertain(uncertain.id, uncertain.claim_token)
    submitting = claimed(store)
    store.begin_submission(submitting.id, submitting.claim_token, "d" * 40)
    reopened = WorkflowStore(store.path, clock=lambda: 1001)
    reopened.initialize()
    assert reopened.get_selection(pending.id, 1, -2) == pending
    assert reopened.get_search(pending.search_id, 1, -2).results == (pending.result,)
    interrupted = {item.id: item for item in reopened.list_incomplete()}
    assert set(interrupted) == {worker.id, uncertain.id, submitting.id}
    assert interrupted[uncertain.id].info_hash == "c" * 40
    assert interrupted[submitting.id].status == Status.SUBMITTING
    for item in interrupted.values():
        with pytest.raises(InvalidTransition):
            reopened.claim_confirmation(item.id, 1, -2)
    done = reopened.finish(
        uncertain.id,
        uncertain.claim_token,
        duplicate=False,
        transmission_id=7,
        transmission_name="Recovered",
    )
    assert done.status == Status.ACCEPTED
    assert reopened.fail(worker.id, worker.claim_token).status == Status.FAILED


def test_uncertain_outcome_remains_blocked_until_explicit_resolution(state):
    store, _ = state
    item = claimed(store)
    store.begin_submission(item.id, item.claim_token, "a" * 40)
    store.mark_uncertain(item.id, item.claim_token)
    assert store.list_incomplete()[0].status == Status.UNCERTAIN
    with pytest.raises(InvalidTransition):
        store.claim_confirmation(item.id, 1, -2)
    store.fail(item.id, item.claim_token)
    with pytest.raises(InvalidTransition):
        store.claim_confirmation(item.id, 1, -2)


def test_schema_newer_than_supported_is_not_modified(state):
    store, _ = state
    item = selection(store)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(StateError, match="schema version"):
        store.initialize()
    assert store.get_selection(item.id, 1, -2) == item


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_bad_ttl_rejected(state, ttl):
    store, _ = state
    with pytest.raises(ValueError):
        store.save_search(1, -2, "query", [], ttl_seconds=ttl)


def test_empty_results_and_off_event_loop_usage(state):
    store, _ = state

    async def use_store():
        search = await asyncio.to_thread(store.save_search, 1, -2, "no matches", [])
        return await asyncio.to_thread(store.get_search, search.id, 1, -2)

    assert asyncio.run(use_store()).results == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"topic_id": -1, "title": "release"},
        {"topic_id": 1, "title": " "},
        {"topic_id": 1, "title": "release", "seeds": -1},
    ],
)
def test_invalid_result_metadata(kwargs):
    with pytest.raises(ValueError):
        TorrentResult(**kwargs)


def test_committed_selection_state_is_logged_without_worker_claim(state, caplog):
    import json
    import logging

    store, _ = state
    with caplog.at_level(logging.INFO):
        item = claimed(store)
        store.begin_submission(item.id, item.claim_token, "a" * 40)
    events = [json.loads(record.message) for record in caplog.records]
    assert [event["submission_status"] for event in events] == ["pending", "claimed", "submitting"]
    assert all(event["user_id"] == 1 and event["chat_id"] == -2 for event in events)
    assert all(event["topic_id"] == 123 for event in events)
    assert item.claim_token not in caplog.text


def test_credentials_are_private_per_user_and_survive_restart(state):
    from tg_torrent_downloader.models import RuTrackerCredentials

    store, _ = state
    pair = RuTrackerCredentials("private-user", "private-password")
    store.save_credentials(1, pair)
    assert store.get_credentials(1) == pair
    assert store.get_credentials(2) is None
    assert "private-password" not in repr(pair)
    assert store.path.stat().st_mode & 0o777 == 0o600
    restored = WorkflowStore(store.path)
    restored.initialize()
    assert restored.get_credentials(1) == pair
    newer = RuTrackerCredentials("new-user", "new-password")
    store.save_credentials(1, newer)
    store.forget_credentials(1, pair)
    assert store.get_credentials(1) == newer
    store.forget_credentials(1, newer)
    assert store.get_credentials(1) is None


def test_version_one_migration_preserves_workflow(state):
    store, _ = state
    item = selection(store)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE ui_controls")
        db.execute("DROP TABLE search_pages")
        db.execute("DROP TABLE search_sessions")
        db.execute("DROP TABLE rutracker_credentials")
        db.execute("PRAGMA user_version = 1")
    store.initialize()
    assert store.get_selection(item.id, 1, -2) == item
    assert store.get_credentials(1) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_durable_ui_context_is_owned_and_action_bound(state):
    store, _ = state
    context = {"query": "movie", "selected_position": 2}
    store.save_ui_control("screen", 1, 10, ("get3",), context)
    restored = WorkflowStore(store.path, clock=store.clock)
    restored.initialize()
    assert restored.get_ui_control("screen", 1, 10, "get3") == context
    for token, user, chat, action in [
        ("screen", 2, 10, "get3"),
        ("screen", 1, 20, "get3"),
        ("screen", 1, 10, "get2"),
        ("unknown", 1, 10, "get3"),
    ]:
        with pytest.raises(Unavailable):
            restored.get_ui_control(token, user, chat, action)
