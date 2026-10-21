"""Telegram search, explicit download confirmation, and durable outcome reporting."""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
)

from tg_torrent_downloader.logging import (
    diagnostic_event,
    log_state,
    log_user_input,
    register_secrets,
)
from tg_torrent_downloader.models import RuTrackerCredentials, Search, Selection, SelectionStatus
from tg_torrent_downloader.rutracker import (
    AuthenticationRequired,
    CaptchaChallenge,
    CaptchaExpired,
    CaptchaUnavailable,
    InvalidCredentials,
    RuTrackerClient,
    RuTrackerError,
    TopicUnavailable,
    UnrecognizedSearchResults,
)
from tg_torrent_downloader.storage import Expired, StateError, WorkflowStore
from tg_torrent_downloader.workflow import (
    DownloadAuthenticationRequired,
    DownloadOutcome,
    DownloadWorkflow,
)

_HELP = (
    "Seed Scout\n/start — connect to RuTracker and begin a search\n"
    "/search — new search\n/status — check recent downloads\n"
    "/cancel — stop the current bot operation\n/help — this guide\n\n"
    "Enter your RuTracker login and password when prompted, then solve any login CAPTCHA. "
    "Send your query, then choose a numbered button or type its number. "
    "Send other text to search again. "
    "Download confirms sending the selected torrent to Transmission. Back restores the same list."
)
_LABELS = {
    "new": "New search",
    "cancel": "Cancel",
    "retry": "Retry",
    "back": "Back",
    "download": "Download",
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
    client: RuTrackerClient | None = None
    challenge: CaptchaChallenge | None = None
    query: str = ""
    search: Search | None = None
    selected: Selection | None = None
    download_claim: Selection | None = None
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

    async def _text(self, chat_id: int, text: str, markup=None) -> None:
        chunks = split_text(text)
        for index, chunk in enumerate(chunks):
            await self._call(
                self.bot.send_message,
                chat_id=chat_id,
                text=chunk,
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=markup if index == len(chunks) - 1 else None,
            )

    def _keyboard(
        self, conversation: Conversation, actions: tuple[str, ...]
    ) -> InlineKeyboardMarkup:
        conversation.token = uuid4().hex
        conversation.actions = actions
        buttons = [
            InlineKeyboardButton(
                text=_LABELS.get(action, action.removeprefix("pick")),
                callback_data=f"sc:{conversation.token}:{action}",
            )
            for action in actions
        ]
        return InlineKeyboardMarkup(
            inline_keyboard=[buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        )

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
        self, conversation: Conversation, text: str, actions: tuple[str, ...]
    ) -> None:
        await self._text(conversation.chat_id, text, self._keyboard(conversation, actions))

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
        if command == "/start":
            if conversation:
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
        if command in {"/start", "/search"}:
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
            await self._screen(c, "Enter your RuTracker password, or /cancel.", ("cancel",))
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
        if c.task and action != "cancel":
            await self._ack(callback.id, text="Working; you can cancel.")
            return
        if action == "cancel":
            if not await self._ack(callback.id):
                return
            await self._discard(c)
            await self._text(
                c.chat_id,
                "Stopped the bot operation. Use /status to check confirmed downloads, "
                "or /start to search.",
            )
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
        if action.startswith("pick"):
            await self._select_result(c, int(action[4:]) - 1)
        elif action == "new":
            await self._abandon_download(c)
            c.query, c.search, c.selected = "", None, None
            await self._authenticate(c, "query")
        elif action == "back":
            await self._abandon_download(c)
            await self._results(c)
        elif action == "retry":
            if c.resume == "search":
                await self._search(c)
            elif c.resume == "topic":
                await self._topic(c)
            elif c.resume == "download":
                await self._download(c)
            else:
                await self._authenticate(c, "query")
        elif action == "download":
            await self._download(c)

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

    async def _download(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "download"
        self._stage(c, "downloading")
        await self._text(
            c.chat_id,
            "Processing the confirmed torrent. You can /cancel waiting; "
            "/status checks its outcome.",
        )
        client = await self._client(c)
        try:
            outcome = await self.workflow.download(
                c.selected.id,
                c.user_id,
                c.chat_id,
                client.download_torrent,
                resume_claim=c.download_claim,
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
        await self._screen(c, self._outcome_text(outcome), ("download", "back"))

    async def _execute(self, c: Conversation, action) -> None:
        task = asyncio.current_task()
        c.task = task
        try:
            await action
        except (RuTrackerError, StateError, ValueError) as error:
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
                    ("new", "cancel"),
                )
            elif isinstance(error, InvalidCredentials):
                if c.credentials:
                    await asyncio.to_thread(self.store.forget_credentials, c.user_id, c.credentials)
                await self._prompt_login(c, failed=True)
            else:
                await self._screen(c, str(error), ("retry", "new", "cancel"))
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
        if c.credentials is None:
            c.credentials = await asyncio.to_thread(self.store.get_credentials, c.user_id)
        if c.credentials is None:
            await self._prompt_login(c)
            return
        register_secrets(c.credentials.username, c.credentials.password)
        self._stage(c, "authenticating")
        await self._screen(
            c,
            "Checking RuTracker login. Chrome may open for browser verification. You can /cancel.",
            ("cancel",),
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
        c.pending_username = None
        c.credentials_unverified = False
        c.challenge = None
        self._stage(c, "login")
        prefix = "RuTracker login failed. Please try again.\n" if failed else ""
        await self._screen(c, prefix + "Enter your RuTracker login, or /cancel.", ("cancel",))

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
            reply_markup=self._keyboard(c, ("cancel",)),
        )

    async def _resume(self, c: Conversation) -> None:
        if c.credentials_unverified:
            await asyncio.to_thread(self.store.save_credentials, c.user_id, c.credentials)
            c.credentials_unverified = False
        c.challenge = None
        if c.resume == "search":
            await self._search(c, reauthenticate=False)
        elif c.resume == "topic":
            await self._topic(c, reauthenticate=False)
        elif c.resume == "download":
            await self._download(c, reauthenticate=False)
        else:
            c.query, c.search, c.selected = "", None, None
            self._stage(c, "query")
            await self._screen(c, "What would you like to find? Send a search query.", ("cancel",))

    async def _search(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "search"
        self._stage(c, "searching")
        await self._screen(c, "Searching RuTracker…", ("cancel",))
        client = await self._client(c)
        try:
            results = await client.search(c.query)
        except AuthenticationRequired:
            if not reauthenticate:
                raise
            await self._authenticate(c, "search")
            return
        if not results:
            self._stage(c, "query")
            await self._screen(c, "No matches found. Send another query.", ("new", "cancel"))
            return
        c.search = await asyncio.to_thread(
            self.store.save_search, c.user_id, c.chat_id, c.query, results[:10]
        )
        c.selected = None
        await self._results(c)

    async def _results(self, c: Conversation) -> None:
        c.search = await asyncio.to_thread(self.store.get_search, c.search.id, c.user_id, c.chat_id)
        c.selected = None
        self._stage(c, "results")
        lines = []
        for index, result in enumerate(c.search.results, 1):
            metadata = []
            if result.size:
                metadata.append(result.size)
            if result.seeds is not None:
                metadata.append(f"Seeds: {result.seeds}")
            lines.append(
                f"{index}. {result.title}" + ("\n" + " · ".join(metadata) if metadata else "")
            )
        await self._screen(
            c,
            "\n\n".join(lines) + "\n\nChoose a numbered button or send a new search query.",
            tuple(f"pick{index}" for index in range(1, len(lines) + 1)),
        )

    async def _topic(self, c: Conversation, *, reauthenticate: bool = True) -> None:
        c.resume = "topic"
        await asyncio.to_thread(self.store.get_search, c.search.id, c.user_id, c.chat_id)
        self._stage(c, "loading_topic")
        await self._screen(c, "Loading the selected release…", ("cancel",))
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
        await self._screen(
            c,
            f"{details.result.title}\n{details.result.topic_url}\n\n{details.description}",
            ("download", "back"),
        )
