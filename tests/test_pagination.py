import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from tg_torrent_downloader.models import RuTrackerCredentials, SearchBatch, TorrentResult
from tg_torrent_downloader.pagination import PaginationError, load_page
from tg_torrent_downloader.storage import Expired, InvalidTransition, Unavailable, WorkflowStore


def rows(first, last):
    return tuple(TorrentResult(i, f"Release {i}", seeds=1000 - i) for i in range(first, last + 1))


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = WorkflowStore(tmp_path / "pages.db", clock=lambda: now[0])
    store.initialize()
    return store, now


def test_fifty_cached_results_cover_five_pages_and_previous_without_network(state):
    store, _ = state
    session = store.save_search_session(
        1, 10, " unchanged ", SearchBatch(rows(1, 50), next_offset=50)
    )
    fetch = AsyncMock(return_value=SearchBatch(rows(51, 62), 50))

    async def run():
        snapshots = []
        for number in range(1, 6):
            page = await load_page(store, session.id, 1, 10, number, fetch)
            snapshots.append(page)
            assert [row.topic_id for row in page.results] == list(
                range((number - 1) * 10 + 1, number * 10 + 1)
            )
        fetch.assert_not_awaited()
        page = await load_page(store, session.id, 1, 10, 6, fetch)
        fetch.assert_awaited_once_with(" unchanged ", 50)
        assert page.results[0].topic_id == 51
        partial = await load_page(store, session.id, 1, 10, 7, fetch)
        assert [row.topic_id for row in partial.results] == [61, 62]
        assert not store.get_search_session(session.id, 1, 10).has_next
        previous = await load_page(store, session.id, 1, 10, 2, fetch)
        assert previous == snapshots[1] and fetch.await_count == 1
        selection = store.select(previous.id, 1, 1, 10)
        assert selection.result.topic_id == 12

    asyncio.run(run())


def test_duplicates_are_removed_and_short_batches_fill_one_logical_page(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 8), next_offset=50))
    fetch = AsyncMock(return_value=SearchBatch(rows(6, 14), 50))
    page = asyncio.run(load_page(store, session.id, 1, 10, 1, fetch))
    assert [result.topic_id for result in page.results] == list(range(1, 11))
    assert [
        result.topic_id for result in store.get_search_session(session.id, 1, 10).results
    ] == list(range(1, 15))


def test_empty_continuation_keeps_current_page_and_marks_end(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10), next_offset=50))
    original = store.select_search_page(session.id, 1, 10, 1)
    fetch = AsyncMock(return_value=SearchBatch((), 50, 100))
    assert asyncio.run(load_page(store, session.id, 1, 10, 2, fetch)) is None
    saved = store.get_search_session(session.id, 1, 10)
    assert saved.current_page == 1 and saved.next_offset is None and not saved.has_next
    assert store.select_search_page(session.id, 1, 10, 1) == original


def test_failed_fetch_does_not_advance_page_or_cursor(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10), next_offset=50))
    store.select_search_page(session.id, 1, 10, 1)
    fetch = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        asyncio.run(load_page(store, session.id, 1, 10, 2, fetch))
    assert store.get_search_session(session.id, 1, 10) == session


def test_batch_cache_is_idempotent_and_rejects_unexpected_offsets(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10), next_offset=50))
    batch = SearchBatch(rows(11, 20), 50, 100)
    appended = store.append_search_batch(session.id, 1, 10, batch)
    assert store.append_search_batch(session.id, 1, 10, batch) == appended
    with pytest.raises(InvalidTransition):
        store.append_search_batch(session.id, 1, 10, SearchBatch(rows(21, 30), 150))


@pytest.mark.parametrize("user,chat", [(2, 10), (1, 11)])
def test_session_and_page_are_owned(state, user, chat):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10)))
    for call in [
        lambda: store.get_search_session(session.id, user, chat),
        lambda: store.select_search_page(session.id, user, chat, 1),
        lambda: store.append_search_batch(session.id, user, chat, SearchBatch((), 50)),
    ]:
        with pytest.raises(Unavailable):
            call()


def test_expiration_and_reopened_store_keep_the_same_page_snapshot(state):
    store, now = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 20)))
    snapshot = store.select_search_page(session.id, 1, 10, 2)
    reopened = WorkflowStore(store.path, clock=lambda: now[0])
    reopened.initialize()
    assert reopened.get_search_session(session.id, 1, 10).current_page == 2
    assert reopened.select_search_page(session.id, 1, 10, 2) == snapshot
    now[0] += 901
    with pytest.raises(Expired):
        reopened.select_search_page(session.id, 1, 10, 1)


def test_duplicate_only_continuations_have_bounded_requests(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10), next_offset=50))
    store.select_search_page(session.id, 1, 10, 1)

    async def fetch(query, offset):
        return SearchBatch(rows(1, 10), offset, offset + 50)

    fetcher = AsyncMock(side_effect=fetch)
    with pytest.raises(PaginationError):
        asyncio.run(load_page(store, session.id, 1, 10, 2, fetcher))
    assert fetcher.await_count == 5
    assert store.get_search_session(session.id, 1, 10).current_page == 1


def test_cancelled_fetch_does_not_advance_page(state):
    store, _ = state
    session = store.save_search_session(1, 10, "query", SearchBatch(rows(1, 10), next_offset=50))

    async def run():
        entered = asyncio.Event()

        async def fetch(query, offset):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(load_page(store, session.id, 1, 10, 2, fetch))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.get_search_session(session.id, 1, 10) == session

    asyncio.run(run())


def test_v3_migration_preserves_existing_credentials_and_selection(state):
    store, _ = state
    pair = RuTrackerCredentials("saved", "password")
    store.save_credentials(1, pair)
    search = store.save_search(1, 10, "old", rows(1, 1))
    selection = store.select(search.id, 0, 1, 10)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE ui_controls")
        db.execute("DROP TABLE search_pages")
        db.execute("DROP TABLE search_sessions")
        db.execute("PRAGMA user_version=3")
    store.initialize()
    assert store.get_credentials(1) == pair
    assert store.get_selection(selection.id, 1, 10) == selection
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_total_pages_rounds_partial_page_and_persists_across_batches(state):
    store, _ = state
    session = store.save_search_session(
        1, 10, "query", SearchBatch(rows(1, 50), next_offset=50, total_results=123)
    )
    assert session.total_pages == 13
    restored = WorkflowStore(store.path, clock=store.clock)
    restored.initialize()
    assert restored.get_search_session(session.id, 1, 10).total_pages == 13
    continued = restored.append_search_batch(session.id, 1, 10, SearchBatch(rows(51, 100), 50, 100))
    assert continued.total_results == 123 and continued.total_pages == 13
    final = restored.append_search_batch(session.id, 1, 10, SearchBatch(rows(101, 123), 100))
    assert final.total_pages == 13
