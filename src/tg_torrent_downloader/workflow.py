"""Confirmed retrieval/submission and conservative recovery of durable workflow state."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from tg_torrent_downloader.logging import diagnostic_event
from tg_torrent_downloader.models import Selection, SelectionStatus
from tg_torrent_downloader.rutracker import AuthenticationRequired, RuTrackerError
from tg_torrent_downloader.storage import (
    InvalidTransition,
    StateError,
    Unavailable,
    UnresolvedTorrent,
    WorkflowStore,
)
from tg_torrent_downloader.torrent import InvalidTorrent, validate_torrent
from tg_torrent_downloader.transmission import (
    ActiveDownload,
    AddStatus,
    TransmissionAuthenticationError,
    TransmissionClient,
    TransmissionError,
    TransmissionRejected,
    TransmissionUnavailable,
    UnsupportedTransmission,
)


@dataclass(frozen=True, repr=False)
class DownloadOutcome:
    selection: Selection
    note: str = ""


class DownloadAuthenticationRequired(AuthenticationRequired):
    def __init__(self, selection: Selection) -> None:
        self.selection = selection
        super().__init__("RuTracker authentication is needed to retrieve the confirmed torrent")


async def database_call(method, *args, **kwargs):
    """Let an in-flight SQLite transaction settle before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _finish_cleanup(action):
    task = asyncio.create_task(action)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


class DownloadWorkflow:
    def __init__(
        self, store: WorkflowStore, transmission_factory: Callable[[], TransmissionClient]
    ) -> None:
        self.store = store
        self.transmission_factory = transmission_factory
        # Serializes metainfo workflows, not the Telegram event loop or unrelated searches.
        self._lock = asyncio.Lock()

    async def active_downloads(self) -> tuple[ActiveDownload, ...]:
        async with self.transmission_factory() as client:
            return await client.active_downloads()

    async def _settle(
        self,
        selection_id: str,
        user_id: int,
        chat_id: int,
        token: str,
        *,
        definite_failure: bool = False,
    ) -> Selection:
        selected = await database_call(self.store.get_selection, selection_id, user_id, chat_id)
        if selected.claim_token != token:
            return selected
        if selected.status == SelectionStatus.CLAIMED or (
            selected.status == SelectionStatus.SUBMITTING and definite_failure
        ):
            return await database_call(self.store.fail, selection_id, token)
        if selected.status == SelectionStatus.SUBMITTING:
            return await database_call(self.store.mark_uncertain, selection_id, token)
        return selected

    async def cancel(self, selected: Selection, *, claim: Selection | None = None) -> Selection:
        if claim is not None:
            return await self._settle(
                selected.id, selected.user_id, selected.chat_id, claim.claim_token
            )
        current = await database_call(
            self.store.get_selection, selected.id, selected.user_id, selected.chat_id
        )
        if current.status == SelectionStatus.PENDING:
            try:
                return await database_call(
                    self.store.cancel, current.id, current.user_id, current.chat_id
                )
            except StateError:
                return await database_call(
                    self.store.get_selection, current.id, current.user_id, current.chat_id
                )
        return current

    async def download(
        self,
        selection_id: str,
        user_id: int,
        chat_id: int,
        fetch: Callable[[int], Awaitable[bytes]],
        *,
        resume_claim: Selection | None = None,
        sequential: bool = False,
    ) -> DownloadOutcome:
        """Only an owned, live confirmation or its in-memory authentication resume may fetch."""
        token = resume_claim.claim_token if resume_claim else uuid4().hex
        data = torrent = None
        try:
            async with self._lock:
                selected = await database_call(
                    self.store.get_selection, selection_id, user_id, chat_id
                )
                if resume_claim is not None:
                    if (resume_claim.id, resume_claim.user_id, resume_claim.chat_id, token) != (
                        selected.id,
                        selected.user_id,
                        selected.chat_id,
                        selected.claim_token,
                    ) or not token:
                        raise Unavailable("Confirmed download is unavailable")
                    if selected.status != SelectionStatus.CLAIMED:
                        return await self._reconcile(selected)
                else:
                    if selected.status != SelectionStatus.PENDING:
                        return await self._reconcile(selected)
                    try:
                        selected = await database_call(
                            self.store.claim_confirmation,
                            selection_id,
                            user_id,
                            chat_id,
                            claim_token=token,
                        )
                    except InvalidTransition:
                        selected = await database_call(
                            self.store.get_selection, selection_id, user_id, chat_id
                        )
                        return DownloadOutcome(selected)
                data = await fetch(selected.result.topic_id)
                torrent = await asyncio.to_thread(validate_torrent, data)
                async with self.transmission_factory() as rpc:
                    await rpc.session_info()  # Read-only failures here cannot have added anything.
                    selected = await database_call(
                        self.store.begin_submission, selection_id, token, torrent.info_hash
                    )
                    result = await rpc.add_torrent(
                        data, expected_info_hash=torrent.info_hash, sequential=sequential
                    )
                    selected = await database_call(
                        self.store.finish,
                        selection_id,
                        token,
                        duplicate=result.status == AddStatus.DUPLICATE,
                        found=result.status == AddStatus.FOUND,
                        transmission_id=result.torrent.id,
                        transmission_name=result.torrent.name,
                    )
                return DownloadOutcome(selected)
        except AuthenticationRequired:
            raise DownloadAuthenticationRequired(selected) from None
        except asyncio.CancelledError:
            await _finish_cleanup(self._settle(selection_id, user_id, chat_id, token))
            raise
        except UnresolvedTorrent:
            selected = await _finish_cleanup(self._settle(selection_id, user_id, chat_id, token))
            return DownloadOutcome(
                selected, "An earlier submission of this torrent is unresolved. Use /status."
            )
        except StateError, sqlite3.Error:
            await _finish_cleanup(self._settle(selection_id, user_id, chat_id, token))
            raise
        except Exception as error:
            selected = await _finish_cleanup(
                self._settle(
                    selection_id,
                    user_id,
                    chat_id,
                    token,
                    definite_failure=isinstance(
                        error,
                        (
                            TransmissionRejected,
                            TransmissionAuthenticationError,
                            InvalidTorrent,
                        ),
                    ),
                )
            )
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from None
            diagnostic_event(
                "download.error",
                selection_id=selection_id,
                user_id=user_id,
                chat_id=chat_id,
                error=type(error).__name__,
                status=selected.status,
            )
            note = "Could not complete the download request."
            if isinstance(error, InvalidTorrent):
                note = str(error)  # Validator errors contain no supplied values.
            elif isinstance(error, TransmissionAuthenticationError):
                note = "Transmission RPC authentication failed. Check its configured credentials."
            elif isinstance(error, TransmissionRejected):
                note = "Transmission rejected the torrent or its download settings."
            elif isinstance(error, UnsupportedTransmission):
                note = "The configured Transmission RPC version is unsupported."
            elif isinstance(error, TransmissionUnavailable):
                note = "Transmission RPC is unavailable. Check its address and service."
            elif isinstance(error, TransmissionError):
                note = "Transmission could not confirm this request."
            elif isinstance(error, RuTrackerError):
                note = "RuTracker could not return the selected torrent. Try a fresh search later."
            return DownloadOutcome(selected, note)
        finally:
            data = torrent = None  # No metainfo files or raw payloads are retained.

    async def _reconcile(self, selected: Selection, rpc=None) -> DownloadOutcome:
        if selected.status not in {SelectionStatus.SUBMITTING, SelectionStatus.UNCERTAIN}:
            return DownloadOutcome(selected)
        if selected.status == SelectionStatus.SUBMITTING:
            selected = await database_call(
                self.store.mark_uncertain, selected.id, selected.claim_token
            )
        try:
            if rpc is None:
                async with self.transmission_factory() as client:
                    found = await client.find_torrent(selected.info_hash)
            else:
                found = await rpc.find_torrent(selected.info_hash)
        except TransmissionError:
            return DownloadOutcome(selected, "Transmission status could not be checked.")
        if found is not None:
            selected = await database_call(
                self.store.finish,
                selected.id,
                selected.claim_token,
                duplicate=False,
                found=True,
                transmission_id=found.id,
                transmission_name=found.name,
            )
        return DownloadOutcome(selected)

    async def recent(self, user_id: int, chat_id: int) -> tuple[DownloadOutcome, ...]:
        async with self._lock:
            rows = await database_call(self.store.history, user_id, chat_id)
            active = {SelectionStatus.SUBMITTING, SelectionStatus.UNCERTAIN}
            if not any(selected.status in active for selected in rows):
                return tuple(DownloadOutcome(selected) for selected in rows)
            outcomes, offline = [], False
            async with self.transmission_factory() as rpc:
                for selected in rows:
                    if selected.status not in active:
                        outcome = DownloadOutcome(selected)
                    elif offline:
                        outcome = DownloadOutcome(
                            selected, "Status was not checked: Transmission is unavailable."
                        )
                    else:
                        outcome = await self._reconcile(selected, rpc)
                        offline = bool(outcome.note)
                    outcomes.append(outcome)
            return tuple(outcomes)

    async def recover_startup(self) -> tuple[DownloadOutcome, ...]:
        """Call only while holding the runtime lock, after all previous workers stopped."""
        async with self._lock:
            rows = await database_call(self.store.list_incomplete)
            outcomes, uncertain = [], []
            for selected in rows:
                selected = await self._settle(
                    selected.id, selected.user_id, selected.chat_id, selected.claim_token
                )
                if selected.status == SelectionStatus.UNCERTAIN:
                    uncertain.append(selected)
                else:
                    outcomes.append(DownloadOutcome(selected, "Interrupted before submission."))
            if uncertain:
                async with self.transmission_factory() as rpc:
                    for selected in uncertain:
                        outcome = await self._reconcile(selected, rpc)
                        outcomes.append(outcome)
                        if outcome.note:
                            # Retain remaining uncertainty without repeated offline waits.
                            break
            diagnostic_event("download.recovery", interrupted=len(rows), checked=len(outcomes))
            return tuple(outcomes)
