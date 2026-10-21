"""Lazy server-batch loading with immutable ten-result selection snapshots."""

from collections.abc import Awaitable, Callable

from tg_torrent_downloader.models import Search, SearchBatch
from tg_torrent_downloader.storage import WorkflowStore
from tg_torrent_downloader.workflow import database_call


class PaginationError(Exception):
    """A bounded navigation attempt could not finish; retain the current page."""


async def load_page(
    store: WorkflowStore,
    session_id: str,
    user_id: int,
    chat_id: int,
    page_number: int,
    fetch: Callable[[str, int], Awaitable[SearchBatch]],
) -> Search | None:
    if type(page_number) is not int or page_number < 1:
        raise ValueError("Invalid page number")
    session = await database_call(store.get_search_session, session_id, user_id, chat_id)
    for _ in range(5):
        if len(session.results) >= page_number * 10 or session.next_offset is None:
            break
        batch = await fetch(session.query, session.next_offset)
        session = await database_call(
            store.append_search_batch, session_id, user_id, chat_id, batch
        )
    if len(session.results) < page_number * 10 and session.next_offset is not None:
        raise PaginationError("Could not finish loading this page. Retry to continue.")
    if (page_number - 1) * 10 >= len(session.results):
        return None
    return await database_call(store.select_search_page, session_id, user_id, chat_id, page_number)
