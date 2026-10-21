"""Telegram search, explicit download confirmation, and durable outcome reporting."""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageEntity,
)

from tg_torrent_downloader.logging import (
    diagnostic_event,
    log_state,
    log_user_input,
    register_secrets,
)
from tg_torrent_downloader.models import (
    RuTrackerCredentials,
    Search,
    SearchSession,
    Selection,
    SelectionStatus,
)
from tg_torrent_downloader.pagination import PaginationError, load_page
from tg_torrent_downloader.rutracker import (
    AuthenticationRequired,
    CaptchaChallenge,
    CaptchaExpired,
    CaptchaUnavailable,
    InvalidCredentials,
    RuTrackerClient,
    RuTrackerError,
    TopicBlock,
    TopicUnavailable,
    UnrecognizedSearchResults,
)
from tg_torrent_downloader.storage import Expired, StateError, WorkflowStore
from tg_torrent_downloader.transmission import TransmissionError
from tg_torrent_downloader.workflow import (
    DownloadAuthenticationRequired,
    DownloadOutcome,
    DownloadWorkflow,
    database_call,
)

_HELP = (
    "Seed Scout\n/start — menu: log in, search, downloads\n"
    "/search — new search\n/status — check recent downloads\n"
    "/cancel — stop the current bot operation\n/help — this guide\n\n"
    "Enter your RuTracker login and password when prompted, then solve any login CAPTCHA. "
    "Send your query, then use Details, Download, or Sequential Download below a result. "
    "Send other text to search again. "
    "Download confirms sending the selected torrent to Transmission. Back restores the same list."
)
_LABELS = {
    "next": "next",
    "prev": "previous",
    "results": "search results",
    "reload": "reload",
    "menu": "menu",
    "login": "log in",
    "search": "search",
    "downloads": "downloads",
    "new": "New search",
    "retry": "Retry",
    "back": "Back",
    "download": "Download",
    "sequential": "Sequential download",
}


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Preserve all text and split within Telegram's UTF-16 message length limit."""
    if limit < 2:
        raise ValueError("Message limit must be at least 2")
    chunks, current = [], []
    size = 0
    for character in text:
        units = 2 if ord(character) > 0xFFFF else 1
        if size + units > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += units
    if current:
        chunks.append("".join(current))
    return chunks or [" "]


@dataclass(repr=False)
class Conversation:
    user_id: int
    chat_id: int
    touched_at: float
    stage: str = "idle"
    token: str = field(default_factory=lambda: uuid4().hex)
    actions: tuple[str, ...] = ()
    credentials: RuTrackerCredentials | None = None
    pending_username: str | None = None
    credentials_unverified: bool = False
    authenticated: bool = False
    client: RuTrackerClient | None = None
    challenge: CaptchaChallenge | None = None
    query: str = ""
    search: Search | None = None
    search_session: SearchSession | None = None
    pending_page: int | None = None
    selected: Selection | None = None
    download_claim: Selection | None = None
    sequential_download: bool = False
    resume: str = "query"
    task: asyncio.Task | None = None


class TelegramConversation:
    def __init__(
        self,
        bot: Bot,
        store: WorkflowStore,
        allowed_users: frozenset[int],
        client_factory: Callable[[int, int, str, str], RuTrackerClient],
        *,
        workflow: DownloadWorkflow,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bot = bot
        self.store = store
        self.allowed_users = allowed_users
        self.client_factory = client_factory
        self.workflow = workflow
        self.clock = clock
        self.sessions: dict[tuple[int, int], Conversation] = {}

    async def _call(self, method, **kwargs):
        for attempt in range(3):
            try:
                return await method(**kwargs)
            except TelegramRetryAfter as error:
                if attempt == 2 or error.retry_after > 30:
                    raise
                await asyncio.sleep(error.retry_after)

    async def _ack(self, callback_id: str, *, text: str | None = None) -> bool:
        values = {"callback_query_id": callback_id}
        if text is not None:
            values["text"] = text
        try:
            await self._call(self.bot.answer_callback_query, **values)
        except TelegramBadRequest as error:
            if any(
                marker in error.message.casefold()
                for marker in (
                    "query is too old",
                    "response timeout expired",
                    "query id is invalid",
                )
            ):
                diagnostic_event("telegram.callback.expired")
                return False
            raise
        return True

    async def _text(
        self, chat_id: int, text: str, markup=None, *, entities: list[MessageEntity] | None = None
    ) -> None:
        chunks = split_text(text)
        offset = 0
        for index, chunk in enumerate(chunks):
            end = offset + len(chunk.encode("utf-16-le")) // 2
            chunk_entities = [
                entity.model_copy(
                    update={
                        "offset": max(entity.offset, offset) - offset,
                        "length": min(entity.offset + entity.length, end)
                        - max(entity.offset, offset),
                    }
                )
                for entity in entities or []
                if entity.offset < end and entity.offset + entity.length > offset
            ]
            await self._call(
                self.bot.send_message,
                chat_id=chat_id,
                text=chunk,
                parse_mode=None,
                entities=chunk_entities or None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=markup if index == len(chunks) - 1 else None,
            )
            offset = end

    def _keyboard(
        self, conversation: Conversation, actions: tuple[str, ...], *, refresh: bool = True
    ) -> InlineKeyboardMarkup | None:
        if refresh:
            conversation.token = uuid4().hex
            conversation.actions = actions
        buttons = [
            InlineKeyboardButton(
                text=(
                    "Details"
                    if action.startswith("pick")
                    else "Download"
                    if action.startswith("get")
                    else "Sequential Download"
                    if action.startswith("seq") and action != "sequential"
                    else _LABELS.get(action, action)
                ),
                callback_data=f"sc:{conversation.token}:{action}",
            )
            for action in actions
        ]
        if not buttons:
            return None
        rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _stage(self, conversation: Conversation, stage: str) -> None:
        conversation.stage = stage
        log_state(
            stage,
            user_id=conversation.user_id,
            chat_id=conversation.chat_id,
            topic_id=conversation.selected.result.topic_id if conversation.selected else None,
            selection_id=conversation.selected.id if conversation.selected else None,
        )

    async def _screen(
        self,
        conversation: Conversation,
        text: str,
        actions: tuple[str, ...],
        *,
        entities: list[MessageEntity] | None = None,
    ) -> None:
        await self._text(
            conversation.chat_id, text, self._keyboard(conversation, actions), entities=entities
        )

    async def _discard(self, conversation: Conversation) -> None:
        task = conversation.task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        conversation.task = asyncio.current_task()
        if conversation.selected is not None:
            try:
                await self.workflow.cancel(conversation.selected, claim=conversation.download_claim)
            except StateError, sqlite3.Error:
                # Preserve the durable record if storage cleanup is unavailable.
                pass
        if conversation.client:
            await conversation.client.close()
        conversation.download_claim = None
        conversation.credentials = None
        conversation.pending_username = None
        conversation.challenge = None
        key = (conversation.user_id, conversation.chat_id)
        if self.sessions.get(key) is conversation:
            self.sessions.pop(key, None)
        self._stage(conversation, "cancelled")

    async def close(self) -> None:
        for conversation in list(self.sessions.values()):
            await self._discard(conversation)

    async def reap_idle(self) -> None:
        for conversation in list(self.sessions.values()):
            if not conversation.task and self.clock() - conversation.touched_at >= 900:
                self.sessions.pop((conversation.user_id, conversation.chat_id), None)
                await self._discard(conversation)

    async def _client(self, conversation: Conversation) -> RuTrackerClient:
        if conversation.client is None:
            if conversation.credentials is None:
                raise StateError("RuTracker credentials are unavailable")
            conversation.client = self.client_factory(
                conversation.user_id,
                conversation.chat_id,
                conversation.credentials.username,
                conversation.credentials.password,
            )
            await conversation.client.__aenter__()
        return conversation.client

    async def handle_message(self, message: Message) -> None:
        if not message.from_user:
            return
        user_id, chat_id = message.from_user.id, message.chat.id
        if user_id not in self.allowed_users:
            await self._text(chat_id, "This bot is restricted to allowed users.")
            return
        if message.chat.type != "private":
            await self._text(chat_id, "Use a private chat with this bot to sign in and search.")
            return
        conversation = self.sessions.get((user_id, chat_id))
        text = message.text
        command = (
            text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if text and text.startswith("/")
            else ""
        )
        log_user_input(
            user_id,
            chat_id,
            text if text is not None else "[non-text input]",
            sensitive=bool(
                conversation
                and conversation.stage in {"login", "password", "captcha"}
                and command not in {"/start", "/search", "/cancel", "/help"}
            ),
        )
        if command == "/help":
            await self._text(chat_id, _HELP)
            return
        if command == "/cancel":
            if conversation:
                await self._discard(conversation)
            await self._text(
                chat_id,
                "Stopped the bot operation. Use /status to check confirmed downloads, "
                "or /start to search.",
            )
            return
        if conversation and conversation.task:
            await self._text(chat_id, "An operation is running. You can /cancel it.")
            return
        if command == "/status":
            if conversation:
                await self._execute(conversation, self._status(user_id, chat_id))
            else:
                await self._status(user_id, chat_id)
            return
        if conversation and self.clock() - conversation.touched_at >= 900:
            conversation.task = asyncio.current_task()
            await self._discard(conversation)
            conversation = None
        if conversation is None:
            if command not in {"/start", "/search"}:
                await self._text(
                    chat_id,
                    "Use /start to begin a new search. Earlier interactions may have expired.",
                )
                return
            conversation = Conversation(user_id, chat_id, self.clock())
            self.sessions[(user_id, chat_id)] = conversation
        conversation.touched_at = self.clock()
        await self._execute(conversation, self._message_action(conversation, text, command))

    async def _message_action(self, c: Conversation, text: str | None, command: str) -> None:
        if command == "/start":
            await self._menu(c, startup=True)
        elif command == "/search":
            await self._abandon_download(c)
            c.query, c.search, c.selected = "", None, None
            await self._authenticate(c, "query")
        elif command and c.stage not in {"login", "password", "description"}:
            await self._text(c.chat_id, "Unknown command. Use /help for available commands.")
        elif text is None or not text.strip():
            await self._text(c.chat_id, "Send a nonempty text message.")
        elif c.stage == "login":
            c.pending_username = text
            register_secrets(text)
            self._stage(c, "password")
            await self._screen(c, "Enter your RuTracker password, or /cancel.", ())
        elif c.stage == "password":
            register_secrets(text)
            c.credentials = RuTrackerCredentials(c.pending_username, text)
            c.pending_username = None
            c.credentials_unverified = True
            await self._authenticate(c, c.resume)
        elif c.stage == "captcha":
            client = await self._client(c)
            try:
                challenge = await client.answer_captcha(
                    c.challenge.id, text, user_id=c.user_id, chat_id=c.chat_id
                )
            except CaptchaExpired:
                c.challenge = None
                await self._authenticate(c, c.resume)
                return
            c.challenge = challenge
            if challenge:
                await self._captcha(c)
            else:
                await self._resume(c)
        elif c.stage in {"query", "description"}:
            if c.stage == "description":
                c.search, c.selected = None, None
            c.query = text  # Preserve whitespace and punctuation exactly for RuTracker search.
            await self._search(c)
        elif c.stage == "results":
            number = text.strip()
            if (
                number.isascii()
                and number.isdigit()
                and len(number) <= 2
                and 1 <= int(number) <= len(c.search.results)
            ):
                await self._select_result(c, int(number) - 1)
            else:
                c.query, c.search, c.selected = text, None, None
                await self._search(c)

        else:
            await self._text(
                c.chat_id, "Use the buttons below the last response, or /search for a new search."
            )

    async def _select_result(self, c: Conversation, position: int) -> None:
        c.selected = await asyncio.to_thread(
            self.store.select,
            c.search.id,
            position,
            c.user_id,
            c.chat_id,
        )
        await self._topic(c)

    async def handle_callback(self, callback: CallbackQuery) -> None:
        if callback.from_user.id not in self.allowed_users:
            await self._ack(callback.id, text="Access denied.")
            return
        if not isinstance(callback.message, Message):
            await self._ack(callback.id, text="Use /start in the bot chat.")
            return
        c = self.sessions.get((callback.from_user.id, callback.message.chat.id))
        pieces = (callback.data or "").split(":")
        valid = (
            c is not None
            and len(pieces) == 3
            and pieces[0] == "sc"
            and pieces[1] == c.token
            and pieces[2] in c.actions
        )
        if not valid or self.clock() - c.touched_at >= 900:
            await self._ack(
                callback.id, text="This control expired. Use /start or the latest buttons."
            )
            return
        action = pieces[2]
        log_user_input(c.user_id, c.chat_id, action)
        if c.task:
            await self._ack(callback.id, text="Working; you can /cancel.")
            return
        c.task = asyncio.current_task()
        try:
            if not await self._ack(callback.id):
                return
            c.touched_at = self.clock()
            await self._execute(c, self._callback_action(c, action))
        finally:
            if c.task is asyncio.current_task():
                c.task = None

    async def _callback_action(self, c: Conversation, action: str) -> None:
        if action == "menu":
            await self._menu(c)
        elif action == "login":
            await self._authenticate(c, "menu")
        elif action == "search":
            await self._abandon_download(c)
            c.query, c.search, c.selected = "", None, None
            await self._authenticate(c, "query")
        elif action in {"downloads", "reload"}:
            await self._downloads(c)
        elif action in {"next", "prev"}:
            target = c.search_session.current_page + (1 if action == "next" else -1)
            await self._navigate(c, target)
        elif action.startswith(("pick", "get", "seq")) and action != "sequential":
            if action.startswith("pick"):
                await self._select_result(c, int(action[4:]) - 1)
            else:
                c.selected = await database_call(
                    self.store.select, c.search.id, int(action[3:]) - 1, c.user_id, c.chat_id
                )
                c.sequential_download = action.startswith("seq")
                await self._download(c)
        elif action == "new":
            await self._abandon_download(c)
            c.query, c.search, c.selected = "", None, None
            await self._authenticate(c, "query")
        elif action in {"back", "results"}:
            await self._abandon_download(c)
            await self._results(c)
        elif action == "retry":
            if c.resume == "search":
                await self._search(c)
            elif c.resume == "page":
                await self._navigate(c, c.pending_page)
            elif c.resume == "topic":
                await self._topic(c)
            elif c.resume == "download":
                await self._download(c)
            else:
                await self._authenticate(c, c.resume if c.resume == "menu" else "query")
        elif action in {"download", "sequential"}:
            c.sequential_download = action == "sequential"
            await self._download(c)

    async def _menu(self, c: Conversation, *, startup: bool = False) -> None:
        await self._abandon_download(c)
        if c.selected is not None:
            try:
                await self.workflow.cancel(c.selected)
            except StateError:
                # An expired selection must not prevent navigation back to menu.
                pass
        c.query, c.search, c.selected = "", None, None
        c.search_session, c.pending_page = None, None
        c.challenge, c.pending_username = None, None
        if c.credentials_unverified:
            c.credentials = None
            c.credentials_unverified = False
        c.resume = "menu"
        note = ""
        if startup:
            self._stage(c, "authenticating")
            await self._screen(c, "Checking RuTracker login.", ())
        if startup and not c.authenticated:
            if c.client is not None:
                await c.client.close()
                c.client = None
            if c.credentials is None:
                c.credentials = await asyncio.to_thread(self.store.get_credentials, c.user_id)
            if c.credentials is not None:
                register_secrets(c.credentials.username, c.credentials.password)
                try:
                    client = await self._client(c)
                    c.challenge = await client.ensure_authenticated()
                    if c.challenge:
                        await self._captcha(c)
                        return
                    c.authenticated = True
                except InvalidCredentials:
                    await asyncio.to_thread(self.store.forget_credentials, c.user_id, c.credentials)
                    c.credentials = None
                    note = "\nRuTracker login failed. Use log in to enter your credentials."
                except RuTrackerError:
                    note = "\nCould not verify RuTracker login. Use log in to retry."
        elif c.client is not None and c.authenticated:
            try:
                c.authenticated = await c.client.check_authenticated()
            except RuTrackerError:
                c.authenticated = False
                note = "\nCould not verify RuTracker login. Use log in to retry."
        if not c.authenticated and c.client is not None:
            await c.client.close()
            c.client = None
        c.resume = "menu"
        self._stage(c, "menu")
        actions = ("search", "downloads") if c.authenticated else ("login", "search", "downloads")
        await self._screen(c, "Menu" + note, actions)

    async def _downloads(self, c: Conversation) -> None:
        self._stage(c, "downloads")
        await self._screen(c, "Loading active downloads…", ())
        try:
            downloads = await self.workflow.active_downloads()
        except TransmissionError as error:
            diagnostic_event(
                "transmission.downloads.failed",
                error=type(error).__name__,
                user_id=c.user_id,
                chat_id=c.chat_id,
            )
            await self._screen(
                c, "Could not load Transmission downloads. Try reload.", ("reload", "menu")
            )
            return
        statuses = {
            1: "Queued for verification",
            2: "Verifying",
            3: "Queued",
            4: "Downloading",
            5: "Queued to seed",
            6: "Seeding",
        }
        rows = ["Active Transmission downloads"]
        entities = []
        offset = len(rows[0].encode("utf-16-le")) // 2 + 2
        for index, download in enumerate(downloads, 1):
            speed = download.rate_download / 1024
            eta = f"{download.eta // 60} min" if download.eta >= 0 else "unknown"
            prefix = f"{index}. {download.name} "
            entities.append(
                MessageEntity(
                    type="text_link",
                    offset=offset + len(prefix.encode("utf-16-le")) // 2,
                    length=1,
                    url="https://www.google.com/search?" + urlencode({"q": download.name}),
                )
            )
            rows.append(
                prefix + "G\n"
                f"{statuses[download.status]} · {download.progress:.1%} · "
                f"↓ {speed:.1f} KiB/s · ↑ {download.rate_upload / 1024:.1f} KiB/s · ETA {eta}"
            )
            offset += len(rows[-1].encode("utf-16-le")) // 2 + 2
        if not downloads:
            rows.append("No active downloads.")
        await self._screen(c, "\n\n".join(rows), ("reload", "menu"), entities=entities)

    async def _abandon_download(self, c: Conversation) -> None:
        if c.download_claim is not None:
            await self.workflow.cancel(c.download_claim, claim=c.download_claim)
            c.download_claim = None

    @staticmethod
    def _outcome_text(outcome: DownloadOutcome) -> str:
        selected = outcome.selection
        descriptions = {
            SelectionStatus.ACCEPTED: (
                "Accepted by Transmission. Queued; completion is not confirmed."
            ),
            SelectionStatus.DUPLICATE: "Already in Transmission (duplicate).",
            SelectionStatus.FOUND: (
                "Confirmed present in Transmission. New versus duplicate is unknown."
            ),
            SelectionStatus.CLAIMED: "This confirmed download request is in progress.",
            SelectionStatus.SUBMITTING: "Submission is in progress. Use /status to check it.",
            SelectionStatus.UNCERTAIN: (
                "Submission outcome is uncertain. Use /status to check. "
                "No add will be retried while unresolved."
            ),
            SelectionStatus.FAILED: (
                "Download request failed. Start a new search and confirm again to retry."
            ),
            SelectionStatus.CANCELLED: "This selection was cancelled.",
        }
        description = descriptions.get(selected.status, "Awaiting confirmation.")
        text = f"{selected.result.title}\n{description}"
        if selected.transmission_id is not None:
            text += (
                f"\nName: {selected.transmission_name}\nTransmission ID: {selected.transmission_id}"
            )
        if outcome.note:
            text += "\n" + outcome.note
        return text

    async def _status(self, user_id: int, chat_id: int) -> None:
        try:
            outcomes = await self.workflow.recent(user_id, chat_id)
        except sqlite3.Error:
            await self._text(chat_id, "Local download storage is unavailable. Try /status again.")
            return
        if not outcomes:
            await self._text(chat_id, "No recent confirmed downloads in this chat.")
        for outcome in outcomes:
            await self._text(chat_id, self._outcome_text(outcome))

    @staticmethod
    def _submission_text(outcome: DownloadOutcome) -> str:
        return {
            SelectionStatus.ACCEPTED: "Accepted by Transmission.",
            SelectionStatus.DUPLICATE: "Already in Transmission (duplicate).",
            SelectionStatus.FOUND: "Confirmed present in Transmission.",
            SelectionStatus.CLAIMED: "Transmission submission is in progress.",
            SelectionStatus.SUBMITTING: "Transmission submission is in progress.",
            SelectionStatus.UNCERTAIN: (
                "Transmission acceptance is uncertain. Use /status to check."
            ),
        }.get(outcome.selection.status, "Not accepted by Transmission.")

    async def _download(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "download"
        self._stage(c, "downloading")
        client = await self._client(c)
        try:
            outcome = await self.workflow.download(
                c.selected.id,
                c.user_id,
                c.chat_id,
                client.download_torrent,
                resume_claim=c.download_claim,
                sequential=c.sequential_download,
            )
        except DownloadAuthenticationRequired as error:
            c.download_claim = error.selection
            if not reauthenticate:
                raise
            await self._authenticate(c, "download")
            return
        c.selected = outcome.selection
        c.download_claim = None
        self._stage(c, "description")
        await self._screen(c, self._submission_text(outcome), ("results", "downloads"))

    async def _execute(self, c: Conversation, action) -> None:
        task = asyncio.current_task()
        c.task = task
        try:
            await action
        except (RuTrackerError, StateError, ValueError, PaginationError) as error:
            c.challenge = None
            self._stage(c, "error")
            diagnostic_event(
                "conversation.error",
                user_id=c.user_id,
                chat_id=c.chat_id,
                error=type(error).__name__,
                resume=c.resume,
            )
            if isinstance(error, UnrecognizedSearchResults):
                c.query, c.search, c.selected = "", None, None
                c.token, c.actions = uuid4().hex, ()
                self._stage(c, "query")
                await self._text(c.chat_id, f"{error}. Send a new search query.")
            elif isinstance(error, (Expired, StateError)):
                await self._screen(
                    c,
                    "This search or selection is unavailable. Start a new search.",
                    ("new",),
                )
            elif isinstance(error, InvalidCredentials):
                if c.credentials:
                    await asyncio.to_thread(self.store.forget_credentials, c.user_id, c.credentials)
                await self._prompt_login(c, failed=True)
            else:
                await self._screen(c, str(error), ("retry", "new"))
        except (TelegramAPIError, sqlite3.Error) as error:
            self._stage(c, "error")
            diagnostic_event(
                "conversation.error",
                user_id=c.user_id,
                chat_id=c.chat_id,
                error=type(error).__name__,
            )
            if isinstance(error, sqlite3.Error):
                await self._text(
                    c.chat_id, "Local search storage is unavailable. Try /start again."
                )
        finally:
            if c.task is task:
                c.task = None

    async def _authenticate(self, c: Conversation, resume: str) -> None:
        c.resume = resume
        c.authenticated = False
        if c.credentials is None:
            c.credentials = await asyncio.to_thread(self.store.get_credentials, c.user_id)
        if c.credentials is None:
            await self._prompt_login(c)
            return
        register_secrets(c.credentials.username, c.credentials.password)
        self._stage(c, "authenticating")
        await self._screen(
            c,
            "Checking RuTracker login.",
            (),
        )
        client = await self._client(c)
        c.challenge = await client.ensure_authenticated()
        if c.challenge:
            await self._captcha(c)
        else:
            await self._resume(c)

    async def _prompt_login(self, c: Conversation, *, failed: bool = False) -> None:
        if c.client:
            await c.client.close()
            c.client = None
        c.credentials = None
        c.authenticated = False
        c.pending_username = None
        c.credentials_unverified = False
        c.challenge = None
        self._stage(c, "login")
        prefix = "RuTracker login failed. Please try again.\n" if failed else ""
        await self._screen(c, prefix + "Enter your RuTracker login, or /cancel.", ())

    async def _captcha(self, c: Conversation) -> None:
        if c.challenge.user_id != c.user_id or c.challenge.chat_id != c.chat_id:
            raise CaptchaUnavailable("This CAPTCHA does not belong to the current user and chat")
        self._stage(c, "captcha")
        extension = {"image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(
            c.challenge.content_type, "png"
        )
        await self._call(
            self.bot.send_photo,
            chat_id=c.chat_id,
            photo=BufferedInputFile(c.challenge.image, filename=f"captcha.{extension}"),
            caption="Type the characters in this CAPTCHA, or /cancel.",
            parse_mode=None,
            reply_markup=self._keyboard(c, ()),
        )

    async def _resume(self, c: Conversation) -> None:
        c.authenticated = True
        if c.credentials_unverified:
            await asyncio.to_thread(self.store.save_credentials, c.user_id, c.credentials)
            c.credentials_unverified = False
        c.challenge = None
        if c.resume == "menu":
            await self._menu(c)
        elif c.resume == "search":
            await self._search(c, reauthenticate=False)
        elif c.resume == "page":
            await self._navigate(c, c.pending_page, reauthenticate=False)
        elif c.resume == "topic":
            await self._topic(c, reauthenticate=False)
        elif c.resume == "download":
            await self._download(c, reauthenticate=False)
        else:
            c.query, c.search, c.selected = "", None, None
            self._stage(c, "query")
            await self._screen(c, "Send a search query", ())

    async def _search(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "search"
        c.search_session, c.pending_page = None, None
        self._stage(c, "searching")
        await self._screen(c, "Searching RuTracker…", ())
        client = await self._client(c)
        try:
            batch = await client.search_batch(c.query)
        except AuthenticationRequired:
            if not reauthenticate:
                raise
            await self._authenticate(c, "search")
            return
        if not batch.results:
            self._stage(c, "query")
            await self._screen(c, "No matches found. Send another query.", ("new",))
            return
        c.search_session = await database_call(
            self.store.save_search_session, c.user_id, c.chat_id, c.query, batch
        )
        c.selected = None
        await self._navigate(c, 1)

    async def _navigate(
        self,
        c: Conversation,
        target: int,
        *,
        reauthenticate: bool = True,
    ) -> None:
        c.resume, c.pending_page = "page", target
        try:
            client = await self._client(c)
            search = await load_page(
                self.store,
                c.search_session.id,
                c.user_id,
                c.chat_id,
                target,
                lambda query, offset: client.search_batch(query, offset=offset),
            )
        except AuthenticationRequired:
            if not reauthenticate:
                raise
            await self._authenticate(c, "page")
            return
        except (RuTrackerError, PaginationError, ValueError) as error:
            if c.search is None:
                raise
            diagnostic_event(
                "search.page.failed",
                user_id=c.user_id,
                chat_id=c.chat_id,
                error=type(error).__name__,
                page=target,
            )
            await self._results(
                c, note="Could not load that page. Retry or send a new query.", retry=True
            )
            return
        if search is not None:
            c.search = search
        c.pending_page = None
        await self._results(c, note="No more results." if search is None else "")

    async def _results(self, c: Conversation, *, note: str = "", retry: bool = False) -> None:
        c.search = await asyncio.to_thread(self.store.get_search, c.search.id, c.user_id, c.chat_id)
        c.selected = None
        if c.search_session is not None:
            c.search_session = await asyncio.to_thread(
                self.store.get_search_session, c.search_session.id, c.user_id, c.chat_id
            )
        self._stage(c, "results")
        row_actions = tuple(
            (f"pick{index}", f"get{index}", f"seq{index}")
            for index in range(1, len(c.search.results) + 1)
        )
        navigation = ()
        page = c.search_session.current_page if c.search_session else 1
        if page > 1:
            navigation += ("prev",)
        if c.search_session and c.search_session.has_next:
            navigation += ("next",)
        if retry:
            navigation += ("retry",)
        self._keyboard(c, tuple(action for row in row_actions for action in row) + navigation)
        for index, result in enumerate(c.search.results, 1):
            metadata = []
            if result.size:
                metadata.append(result.size)
            if result.seeds is not None:
                metadata.append(f"Seeds: {result.seeds}")
            buttons = self._keyboard(c, row_actions[index - 1], refresh=False)
            await self._text(
                c.chat_id,
                f"{index}. {result.title}" + ("\n" + " · ".join(metadata) if metadata else ""),
                buttons,
            )
        total_pages = c.search_session.total_pages if c.search_session else 1
        await self._text(
            c.chat_id,
            f"Page {page} / {total_pages if total_pages is not None else '?'}"
            + ("\n" + note if note else ""),
            self._keyboard(c, navigation, refresh=False),
        )

    async def _topic(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "topic"
        await asyncio.to_thread(self.store.get_search, c.search.id, c.user_id, c.chat_id)
        self._stage(c, "loading_topic")
        await self._screen(c, "Loading the selected release…", ())
        client = await self._client(c)
        try:
            details = await client.get_topic(c.selected.result)
        except AuthenticationRequired:
            if not reauthenticate:
                raise
            await self._authenticate(c, "topic")
            return
        except TopicUnavailable:
            await self._text(c.chat_id, "That topic is unavailable. Choose another result.")
            await self._results(c)
            return
        self._stage(c, "description")
        header = f"{details.result.title}\n{details.result.topic_url}\n\n"
        if details.poster_url:
            try:
                await self._call(
                    self.bot.send_photo,
                    chat_id=c.chat_id,
                    photo=details.poster_url,
                    parse_mode=None,
                )
            except TelegramAPIError as error:
                diagnostic_event(
                    "telegram.poster.unavailable",
                    user_id=c.user_id,
                    chat_id=c.chat_id,
                    topic_id=details.result.topic_id,
                    error=type(error).__name__,
                )
                header += f"Poster: {details.poster_url}\n\n"
        blocks = details.blocks or (TopicBlock(details.description),)
        offset, entities = len(header.encode("utf-16-le")) // 2, []
        for block in blocks:
            length = len(block.text.encode("utf-16-le")) // 2
            if block.expandable and length:
                entities.append(
                    MessageEntity(type="expandable_blockquote", offset=offset, length=length)
                )
            offset += length + 2
        await self._screen(
            c,
            header + "\n\n".join(block.text for block in blocks),
            ("back", "download", "sequential"),
            entities=entities,
        )
