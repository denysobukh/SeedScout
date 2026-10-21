import asyncio
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, User
from workflow_fakes import DATA, HASH, FakeTransmission

from tg_torrent_downloader.conversation import TelegramConversation, split_text
from tg_torrent_downloader.models import (
    RuTrackerCredentials,
    SearchBatch,
    SelectionStatus,
    TorrentResult,
)
from tg_torrent_downloader.rutracker import (
    AuthenticationRequired,
    CaptchaChallenge,
    CaptchaExpired,
    InvalidCredentials,
    NetworkError,
    TopicBlock,
    TopicDetails,
    TopicUnavailable,
    UnrecognizedSearchResults,
)
from tg_torrent_downloader.storage import WorkflowStore
from tg_torrent_downloader.transmission import (
    AddStatus,
    SubmissionUncertain,
    TransmissionRejected,
    TransmissionTorrent,
)
from tg_torrent_downloader.workflow import DownloadWorkflow


class FakeBot:
    def __init__(self):
        self.messages, self.photos, self.answers = [], [], []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)

    async def answer_callback_query(self, **kwargs):
        self.answers.append(kwargs)


class FakeTracker:
    def __init__(self):
        self.opened = self.closed = False
        self.ensure_calls = 0
        self.check_calls = 0
        self.check_outcomes = []
        self.queries, self.topics, self.answers = [], [], []
        self.batch_calls = []
        self.batch_outcomes = {}
        self.downloads, self.download_outcomes = [], []
        self.download_wait = None
        self.download_entered = asyncio.Event()
        self.auth_outcomes, self.search_outcomes, self.topic_outcomes, self.captcha_outcomes = (
            [],
            [],
            [],
            [],
        )
        self.results = tuple(
            TorrentResult(i, f"Release {i}", size="1 GB", seeds=2) for i in range(1, 11)
        )
        self.description = "Full description <b>literal text</b>"
        self.wait = None
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.opened = True
        return self

    async def close(self):
        self.closed = True

    def outcome(self, queue, default):
        result = queue.pop(0) if queue else default
        if isinstance(result, Exception):
            raise result
        return result

    async def check_authenticated(self):
        self.check_calls += 1
        return self.outcome(self.check_outcomes, True)

    async def ensure_authenticated(self):
        self.ensure_calls += 1
        self.entered.set()
        if self.wait:
            await self.wait.wait()
        return self.outcome(self.auth_outcomes, None)

    async def answer_captcha(self, challenge_id, answer, **owner):
        self.answers.append((challenge_id, answer, owner))
        return self.outcome(self.captcha_outcomes, None)

    async def search(self, query):
        self.queries.append(query)
        return self.outcome(self.search_outcomes, self.results)

    async def search_batch(self, query, *, offset=0):
        self.batch_calls.append((query, offset))
        if offset in self.batch_outcomes:
            self.queries.append(query)
            return self.outcome(self.batch_outcomes[offset], SearchBatch((), offset))
        return SearchBatch(await self.search(query), offset)

    async def get_topic(self, result):
        self.topics.append(result)
        return self.outcome(self.topic_outcomes, TopicDetails(result, self.description))

    async def download_torrent(self, topic_id):
        self.downloads.append(topic_id)
        self.download_entered.set()
        if self.download_wait:
            await self.download_wait.wait()
        return self.outcome(self.download_outcomes, DATA)


def message(text, user_id=1, chat_id=10):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Tester"),
        text=text,
    )


def callback(data, user_id=1, chat_id=10):
    return CallbackQuery(
        id=uuid4().hex,
        from_user=User(id=user_id, is_bot=False, first_name="Tester"),
        chat_instance="test",
        message=message("buttons", user_id, chat_id),
        data=data,
    )


class Harness:
    def __init__(self, tmp_path, *, credentials=True):
        self.now = [1000.0]
        self.store = WorkflowStore(tmp_path / "test.db", clock=lambda: self.now[0])
        self.store.initialize()
        if credentials:
            self.store.save_credentials(1, RuTrackerCredentials("saved-user", "saved-password"))
        self.bot = FakeBot()
        self.clients = []
        self.next_client = FakeTracker()

        def factory(user, chat, username, password):
            client = self.next_client
            self.next_client = FakeTracker()
            client.credentials = RuTrackerCredentials(username, password)
            self.clients.append(client)
            return client

        self.factory = factory
        self.rpc = FakeTransmission()
        self.workflow = DownloadWorkflow(self.store, lambda: self.rpc)
        self.flow = TelegramConversation(
            self.bot,
            self.store,
            frozenset({1, 2}),
            factory,
            workflow=self.workflow,
            clock=lambda: self.now[0],
        )

    @property
    def state(self):
        return self.flow.sessions[(1, 10)]

    @property
    def client(self):
        return self.clients[-1]

    def button(self, text):
        screens = self.bot.messages + self.bot.photos
        # Message/photo lists are separate; current state token identifies the active controls.
        for screen in reversed(screens):
            markup = screen.get("reply_markup")
            if markup:
                for row in markup.inline_keyboard:
                    for button in row:
                        if (
                            button.text == text
                            or (text.isdecimal() and button.callback_data.endswith(f":pick{text}"))
                        ) and self.state.token in button.callback_data:
                            return button.callback_data
        raise AssertionError(f"No active {text} button")

    async def send(self, text, **kwargs):
        await self.flow.handle_message(message(text, **kwargs))

    async def click(self, text, **kwargs):
        await self.flow.handle_callback(callback(self.button(text), **kwargs))

    async def search(self, query="ubuntu"):
        await self.send("/search")
        await self.send(query)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_search_authenticates_before_prompting_for_query(h):
    async def run():
        await h.send("/search")
        assert h.client.opened and h.client.ensure_calls == 1
        assert h.state.stage == "query"
        assert not h.client.queries
        assert "Send a search query" in h.bot.messages[-1]["text"]
        await h.flow.close()
        assert h.client.closed

    asyncio.run(run())


def test_help_and_unauthorized_inputs_never_open_upstream_session(h):
    async def run():
        await h.send("/help")
        await h.send("/start", user_id=99)
        await h.flow.handle_callback(callback("sc:fake:download", user_id=99))
        assert not h.clients
        assert "restricted" in h.bot.messages[-1]["text"]
        assert "denied" in h.bot.answers[-1]["text"]

    asyncio.run(run())


def test_query_is_unchanged_and_first_ten_results_are_numbered(h):
    async def run():
        await h.search("  Test & 2020  ")
        assert h.client.queries == ["  Test & 2020  "]
        assert h.state.stage == "results"
        assert len(h.state.search.results) == 10
        results = h.bot.messages[-11:-1]
        assert len(results) == 10
        for index, item in enumerate(results, 1):
            assert item["text"] == f"{index}. Release {index}\n1 GB · Seeds: 2"
            assert [b.text for b in item["reply_markup"].inline_keyboard[0]] == [
                "Details",
                "Download",
                "Sequential Download",
            ]
        assert h.bot.messages[-1]["text"] == "Page 1 / 1"
        assert h.bot.messages[-1]["reply_markup"] is None

    asyncio.run(run())


def test_description_and_back_preserve_search_and_numbering(h):
    async def run():
        await h.search()
        original = h.state.search
        results_text = [item["text"] for item in h.bot.messages[-11:]]
        await h.send("2")
        assert h.state.stage == "description"
        assert h.client.topics[0].topic_id == 2
        assert "Full description <b>literal text</b>" in h.bot.messages[-1]["text"]
        assert h.bot.messages[-1]["parse_mode"] is None
        assert h.bot.messages[-1]["link_preview_options"].is_disabled
        await h.click("Back")
        assert h.state.search == original
        assert [item["text"] for item in h.bot.messages[-11:]] == results_text
        assert h.client.queries == ["ubuntu"]

    asyncio.run(run())


def test_download_confirms_once_and_reports_durable_acceptance(h):
    async def run():
        await h.search()
        await h.send("1")
        selected = h.state.selected
        old_download = h.button("Download")
        before = len(h.bot.messages)
        await h.click("Download")
        assert len(h.bot.messages) == before + 1
        assert h.bot.messages[-1]["text"] == "Accepted by Transmission."
        assert h.state.actions == ("results", "downloads")
        await h.flow.handle_callback(callback(old_download))
        assert "expired" in h.bot.answers[-1]["text"]
        assert len(h.bot.messages) == before + 1
        assert h.client.downloads == [1] and len(h.rpc.adds) == 1
        stored = h.store.get_selection(selected.id, 1, 10)
        assert stored.status == SelectionStatus.ACCEPTED
        assert stored.claim_token is not None

    asyncio.run(run())


@pytest.mark.parametrize("number", ["0", "11", "-1", "text", "1.5", "١", "9" * 100])
def test_other_result_inputs_become_unchanged_search_queries(h, number):
    async def run():
        await h.search()
        await h.send(number)
        assert not h.client.topics
        assert h.client.queries == ["ubuntu", number]
        assert h.state.query == number
        assert h.state.stage == "results"

    asyncio.run(run())


def test_no_results_and_unsupported_messages(h):
    async def run():
        h.next_client.results = ()
        await h.search()
        assert h.state.stage == "query"
        assert "No matches" in h.bot.messages[-1]["text"]
        await h.send(None)
        await h.send("  ")
        assert "nonempty" in h.bot.messages[-1]["text"]
        assert h.client.queries == ["ubuntu"]

    asyncio.run(run())


def test_captcha_image_answer_and_refreshed_challenge_are_scoped_and_redacted(h, caplog):
    async def run():
        first = CaptchaChallenge("first", 1, 10, b"image-1", "image/png", 1300)
        second = CaptchaChallenge("second", 1, 10, b"image-2", "image/png", 1300)
        h.next_client.auth_outcomes = [first]
        h.next_client.captcha_outcomes = [second, None]
        await h.send("/search")
        assert h.state.stage == "captcha"
        assert h.bot.photos[-1]["photo"].data == b"image-1"
        await h.send("wrong-secret")
        assert h.bot.photos[-1]["photo"].data == b"image-2"
        await h.send("answer-secret")
        assert h.state.stage == "query"
        assert h.client.answers[-1] == ("second", "answer-secret", {"user_id": 1, "chat_id": 10})
        assert not h.client.queries

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "wrong-secret" not in caplog.text and "answer-secret" not in caplog.text


def test_expired_captcha_restarts_authentication(h):
    async def run():
        h.next_client.auth_outcomes = [
            CaptchaChallenge("first", 1, 10, b"img", "image/png", 1001),
            None,
        ]
        h.next_client.captcha_outcomes = [CaptchaExpired("Expired")]
        await h.send("/search")
        await h.send("answer")
        assert h.client.ensure_calls == 2
        assert h.state.stage == "query"

    asyncio.run(run())


def test_session_expiry_during_search_resumes_original_query(h):
    async def run():
        h.next_client.search_outcomes = [AuthenticationRequired("Expired")]
        await h.search("  saved query  ")
        assert h.client.ensure_calls == 2
        assert h.client.queries == ["  saved query  "] * 2
        assert h.state.stage == "results"

    asyncio.run(run())


def test_session_expiry_during_topic_resumes_original_selection(h):
    async def run():
        await h.search()
        h.client.topic_outcomes = [AuthenticationRequired("Expired")]
        await h.send("3")
        assert [result.topic_id for result in h.client.topics] == [3, 3]
        assert h.state.stage == "description"

    asyncio.run(run())


def test_network_retry_preserves_query(h):
    async def run():
        h.next_client.search_outcomes = [NetworkError("Try again")]
        await h.search("saved")
        assert h.state.stage == "error"
        await h.click("Retry")
        assert h.client.queries == ["saved", "saved"]
        assert h.state.stage == "results"

    asyncio.run(run())


def test_invalid_credentials_return_to_login_without_search_or_retry(h):
    async def run():
        h.next_client.auth_outcomes = [InvalidCredentials("bad")]
        await h.send("/search")
        assert "retry" not in h.state.actions
        assert h.state.stage == "login"
        assert h.state.actions == ()
        assert "login failed" in h.bot.messages[-1]["text"]
        assert "Enter your RuTracker login" in h.bot.messages[-1]["text"]
        assert h.bot.messages[-1]["reply_markup"] is None
        assert h.store.get_credentials(1) is None
        assert h.client.closed

    asyncio.run(run())


def test_unavailable_topic_returns_to_original_list(h):
    async def run():
        await h.search()
        original = h.state.search
        h.client.topic_outcomes = [TopicUnavailable("gone")]
        await h.send("1")
        assert h.state.stage == "results"
        assert h.state.search == original

    asyncio.run(run())


def test_callbacks_reject_wrong_user_chat_and_old_screen(h):
    async def run():
        await h.search()
        old = h.button("1")
        await h.flow.handle_callback(callback(old, user_id=2))
        await h.flow.handle_callback(callback(old, chat_id=20))
        assert len(h.clients) == 1
        await h.send("1")
        before = len(h.client.topics)
        await h.flow.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]
        assert len(h.client.topics) == before
        assert h.state.stage == "description"

    asyncio.run(run())


def test_restart_rejects_old_controls_without_using_persisted_selection(h):
    async def run():
        await h.search()
        await h.send("1")
        old = h.button("Download")
        fresh = TelegramConversation(h.bot, h.store, frozenset({1}), h.factory, workflow=h.workflow)
        await fresh.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]
        assert len(h.clients) == 1
        await h.flow.close()

    asyncio.run(run())


def test_search_expiry_blocks_number_and_download(h):
    async def run():
        await h.search()
        h.now[0] += 901
        await h.send("1")
        assert not h.client.topics
        assert "Use /start" in h.bot.messages[-1]["text"]

    asyncio.run(run())


def test_cancel_interrupts_pending_authentication_and_closes_client(h):
    async def run():
        client = h.next_client
        client.wait = asyncio.Event()
        task = asyncio.create_task(h.send("/search"))
        await asyncio.wait_for(client.entered.wait(), timeout=3)
        await h.send("query while busy")
        assert "operation is running" in h.bot.messages[-1]["text"]
        await h.send("/cancel")
        await asyncio.gather(task, return_exceptions=True)
        assert not h.flow.sessions and client.closed
        assert not client.queries

    asyncio.run(run())


def test_full_description_is_split_without_loss_or_telegram_markup(h):
    async def run():
        await h.search()
        h.client.description = "<b>literal</b>" + "🙂" * 5000 + "tail"
        start = len(h.bot.messages)
        await h.send("1")
        responses = h.bot.messages[start + 1 :]  # Skip loading message.
        assert h.client.description in "".join(item["text"] for item in responses)
        assert all(len(item["text"].encode("utf-16-le")) // 2 <= 4000 for item in responses)
        assert all(item["parse_mode"] is None for item in responses)
        assert all(item["reply_markup"] is None for item in responses[:-1])
        assert responses[-1]["reply_markup"] is not None

    asyncio.run(run())


def test_split_text_preserves_all_characters():
    text = "ab🙂\n" * 2000
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)


def test_captcha_from_another_owner_is_never_delivered(h):
    async def run():
        h.next_client.auth_outcomes = [
            CaptchaChallenge("private", 2, 20, b"private", "image/png", 1300)
        ]
        await h.send("/search")
        assert not h.bot.photos
        assert h.state.stage == "error"

    asyncio.run(run())


def test_reauthentication_can_pause_search_for_captcha_and_resume(h):
    async def run():
        challenge = CaptchaChallenge("query-login", 1, 10, b"image", "image/png", 1300)
        h.next_client.auth_outcomes = [None, challenge]
        h.next_client.search_outcomes = [AuthenticationRequired("expired")]
        await h.search("preserved query")
        assert h.state.stage == "captcha"
        await h.send("answer")
        assert h.client.queries == ["preserved query", "preserved query"]
        assert h.state.stage == "results"

    asyncio.run(run())


def test_expired_download_and_idle_cleanup(h):
    async def run():
        await h.search()
        await h.send("1")
        selected = h.state.selected
        h.now[0] += 900
        # Refresh conversation activity; stored search itself must still enforce its expiry.
        h.state.touched_at = h.now[0]
        await h.click("Download")
        assert "unavailable" in h.bot.messages[-1]["text"]
        assert h.store.get_selection(selected.id, 1, 10).claim_token is None
        h.now[0] += 901
        await h.flow.reap_idle()
        assert not h.flow.sessions and h.client.closed

    asyncio.run(run())


def test_callback_reserves_operation_before_acknowledgement_yields(h):
    async def run():
        await h.search()
        data = h.button("1")
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.bot.answer_callback_query

        async def slow_ack(**kwargs):
            if "text" not in kwargs:
                entered.set()
                await release.wait()
            await original(**kwargs)

        h.bot.answer_callback_query = slow_ack
        first = asyncio.create_task(h.flow.handle_callback(callback(data)))
        await asyncio.wait_for(entered.wait(), timeout=3)
        await h.flow.handle_callback(callback(data))
        assert "Working" in h.bot.answers[-1]["text"]
        release.set()
        await first
        assert len(h.client.topics) == 1  # Exactly one numeric-button selection.

    asyncio.run(run())


def test_old_controls_and_unknown_actions_cannot_start_work(h):
    async def run():
        await h.send("/search")
        forged = f"sc:{h.state.token}:download"
        await h.flow.handle_callback(callback(forged))
        assert "expired" in h.bot.answers[-1]["text"]
        assert not h.client.topics and not h.client.queries
        await h.send("ubuntu")
        old = h.button("1")
        await h.send("/search")
        assert not h.clients[0].closed
        await h.flow.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]

    asyncio.run(run())


def test_aiogram_router_dispatches_messages_without_network(h):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Update

    from tg_torrent_downloader.bot import create_router

    async def run():
        dispatcher = Dispatcher(disable_fsm=True)
        dispatcher.include_router(create_router(h.flow))
        bot = Bot("123456789:abcdefghijklmnopqrstuvwxyz_123456789")
        try:
            await dispatcher.feed_update(bot, Update(update_id=1, message=message("/help")))
            assert "Seed Scout" in h.bot.messages[-1]["text"]
            await dispatcher.feed_update(
                bot, Update(update_id=2, message=message("/start", user_id=99))
            )
            assert "restricted" in h.bot.messages[-1]["text"]
        finally:
            await bot.session.close()

    asyncio.run(run())


def test_cancel_marks_pending_selection_cancelled_and_closes_client(h):
    async def run():
        await h.search()
        await h.send("1")
        selected = h.state.selected
        await h.send("/cancel")
        assert not h.flow.sessions and h.client.closed
        assert h.store.get_selection(selected.id, 1, 10).status == SelectionStatus.CANCELLED

    asyncio.run(run())


def test_results_have_actions_under_each_displayed_torrent(h):
    async def run():
        h.next_client.results = h.next_client.results[:3]
        await h.search()
        for index, item in enumerate(h.bot.messages[-4:-1], 1):
            buttons = item["reply_markup"].inline_keyboard[0]
            assert [b.text for b in buttons] == ["Details", "Download", "Sequential Download"]
            assert [b.callback_data.split(":")[-1] for b in buttons] == [
                f"pick{index}",
                f"get{index}",
                f"seq{index}",
            ]
            assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
        assert h.bot.messages[-1]["reply_markup"] is None
        await h.click("3")
        assert h.client.topics[-1].topic_id == 3
        assert h.state.stage == "description"

    asyncio.run(run())


@pytest.mark.parametrize("prefix, sequential", [("get", False), ("seq", True)])
def test_result_row_download_submits_its_own_torrent(h, prefix, sequential):
    async def run():
        await h.search()
        data = f"sc:{h.state.token}:{prefix}3"
        await h.flow.handle_callback(callback(data))
        assert h.client.downloads == [3]
        assert h.client.topics == []
        assert h.rpc.sequential_adds == [sequential]
        assert h.bot.messages[-1]["text"] == "Accepted by Transmission."
        await h.flow.handle_callback(callback(data))
        assert "expired" in h.bot.answers[-1]["text"]
        assert len(h.rpc.adds) == 1

    asyncio.run(run())


@pytest.mark.parametrize("query", ["  a different release  ", "1", "10", "11", "/a-title"])
def test_new_query_from_description_invalidates_previous_controls(h, query):
    async def run():
        await h.search()
        await h.click("2")
        old = h.button("Download")
        assert h.state.actions == ("back", "download", "sequential")
        markup = h.bot.messages[-1]["reply_markup"]
        assert [button.text for row in markup.inline_keyboard for button in row] == [
            "Back",
            "Download",
            "Sequential download",
        ]
        await h.send(query)
        assert h.client.queries == ["ubuntu", query]
        assert len(h.client.topics) == 1
        assert h.state.stage == "results" and h.state.selected is None
        await h.flow.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]

    asyncio.run(run())


@pytest.mark.parametrize("query", ["a different title", "1", "11", "  query with spaces  "])
def test_unrecognized_search_results_have_no_buttons_and_next_input_is_query(h, query):
    async def run():
        h.next_client.search_outcomes = [
            UnrecognizedSearchResults("RuTracker search table contains no recognizable releases")
        ]
        await h.search("first query")
        assert h.state.stage == "query"
        assert h.bot.messages[-1]["reply_markup"] is None
        assert h.state.actions == ()
        assert "no recognizable releases" in h.bot.messages[-1]["text"]
        assert h.state.search is None and h.state.selected is None
        await h.send(query)
        assert h.client.queries == ["first query", query]
        assert not h.client.topics
        assert h.state.stage == "results"

    asyncio.run(run())


def test_new_user_login_password_steps_and_restart_reuse(tmp_path):
    h = Harness(tmp_path, credentials=False)

    async def run():
        await h.send("/search")
        assert h.state.stage == "login" and not h.clients
        await h.send("new-user")
        assert h.state.stage == "password" and not h.clients
        assert h.store.get_credentials(1) is None
        await h.send("/password-with-slash")
        pair = RuTrackerCredentials("new-user", "/password-with-slash")
        assert h.client.credentials == pair
        assert h.state.stage == "query"
        assert h.store.get_credentials(1) == pair
        await h.flow.close()
        h.flow = TelegramConversation(
            h.bot, h.store, frozenset({1, 2}), h.factory, workflow=h.workflow
        )
        await h.send("/search")
        assert h.state.stage == "query"
        assert h.client.credentials == pair
        assert h.store.get_credentials(2) is None
        await h.flow.close()

    asyncio.run(run())


def test_bad_credentials_loop_until_valid_and_never_persist_candidates(tmp_path):
    h = Harness(tmp_path, credentials=False)

    async def run():
        await h.send("/search")
        for attempt in range(2):
            client = h.next_client
            client.auth_outcomes = [InvalidCredentials("rejected")]
            await h.send(f"wrong-{attempt}")
            assert h.state.stage == "password"
            await h.send("bad-password")
            assert h.state.stage == "login" and h.state.actions == ()
            assert h.store.get_credentials(1) is None
            assert client.closed and h.state.credentials is None
        await h.send("valid-user")
        await h.send("valid-password")
        assert h.state.stage == "query"
        assert h.store.get_credentials(1) == RuTrackerCredentials("valid-user", "valid-password")
        await h.flow.close()

    asyncio.run(run())


def test_captcha_credentials_saved_only_after_success(tmp_path):
    h = Harness(tmp_path, credentials=False)

    async def run():
        h.next_client.auth_outcomes = [
            CaptchaChallenge("login", 1, 10, b"image", "image/png", 1300)
        ]
        await h.send("/search")
        await h.send("candidate-user")
        await h.send("candidate-password")
        assert h.state.stage == "captcha"
        assert h.store.get_credentials(1) is None
        await h.send("answer")
        assert h.state.stage == "query"
        assert h.store.get_credentials(1) == RuTrackerCredentials(
            "candidate-user", "candidate-password"
        )
        await h.flow.close()

    asyncio.run(run())


def test_rejected_captcha_login_returns_to_credentials(tmp_path):
    h = Harness(tmp_path, credentials=False)

    async def run():
        h.next_client.auth_outcomes = [
            CaptchaChallenge("login", 1, 10, b"image", "image/png", 1300)
        ]
        h.next_client.captcha_outcomes = [InvalidCredentials("bad")]
        await h.send("/search")
        await h.send("bad-user")
        await h.send("bad-password")
        await h.send("answer")
        assert h.state.stage == "login"
        assert h.store.get_credentials(1) is None
        assert h.state.challenge is None
        await h.flow.close()

    asyncio.run(run())


def test_credentials_inputs_are_redacted_and_cancel_clears_pending(tmp_path, caplog):
    h = Harness(tmp_path, credentials=False)

    async def run():
        with caplog.at_level(logging.INFO):
            await h.send("/search")
            await h.send("private-login")
            await h.send("private-password")
        assert "private-login" not in caplog.text
        assert "private-password" not in caplog.text
        await h.flow.close()
        # A different allowed user must enter their own credentials.
        await h.send("/search", user_id=2)
        await h.send("pending-login", user_id=2)
        state = h.flow.sessions[(2, 10)]
        await h.send("/cancel", user_id=2)
        assert state.credentials is None and state.pending_username is None
        assert h.store.get_credentials(2) is None

    asyncio.run(run())


def test_failed_reauthentication_preserves_search_until_new_credentials(h):
    async def run():
        await h.send("/search")
        h.client.search_outcomes = [AuthenticationRequired("expired")]
        h.client.auth_outcomes = [InvalidCredentials("bad")]
        await h.send("saved query")
        assert h.state.stage == "login" and h.state.query == "saved query"
        await h.send("replacement-user")
        await h.send("replacement-password")
        assert h.state.stage == "results"
        assert h.client.queries == ["saved query"]
        assert h.store.get_credentials(1).username == "replacement-user"
        await h.flow.close()

    asyncio.run(run())


def test_group_chat_does_not_collect_credentials(h):
    async def run():
        group = message("/start").model_copy(update={"chat": Chat(id=-10, type="group")})
        await h.flow.handle_message(group)
        assert not h.flow.sessions and not h.clients
        assert "private chat" in h.bot.messages[-1]["text"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,phrase",
    [
        (AddStatus.DUPLICATE, "duplicate"),
        (AddStatus.FOUND, "Confirmed present"),
    ],
)
def test_download_reports_duplicate_and_reconciled_presence_honestly(h, status, phrase):
    async def run():
        await h.search()
        await h.click("2")
        h.rpc.status = status
        await h.click("Download")
        assert phrase in h.bot.messages[-1]["text"]
        assert h.state.actions == ("results", "downloads")
        assert h.client.downloads == [2]

    asyncio.run(run())


def test_simultaneous_download_clicks_do_not_repeat_retrieval_or_add(h):
    async def run():
        await h.search()
        await h.click("1")
        h.rpc.wait = asyncio.Event()
        data = h.button("Download")
        first = asyncio.create_task(h.flow.handle_callback(callback(data)))
        await h.rpc.entered.wait()
        await h.flow.handle_callback(callback(data))
        assert "Working" in h.bot.answers[-1]["text"]
        h.rpc.wait.set()
        await first
        assert h.client.downloads == [1] and len(h.rpc.adds) == 1

    asyncio.run(run())


@pytest.mark.parametrize("captcha", [False, True])
def test_confirmed_download_reauthenticates_and_resumes_same_selection(h, captcha):
    async def run():
        await h.search()
        await h.click("3")
        selected = h.state.selected
        h.client.download_outcomes = [AuthenticationRequired("expired"), DATA]
        if captcha:
            h.client.auth_outcomes = [
                CaptchaChallenge("download", 1, 10, b"image", "image/png", 1300)
            ]
        await h.click("Download")
        if captcha:
            assert h.state.stage == "captcha" and not h.rpc.adds
            assert h.store.get_selection(selected.id, 1, 10).status == SelectionStatus.CLAIMED
            await h.send("answer")
        assert h.state.selected.id == selected.id
        assert h.state.selected.status == SelectionStatus.ACCEPTED
        assert h.client.downloads == [3, 3] and len(h.rpc.adds) == 1
        assert h.state.download_claim is None

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/cancel", "/search", "/start"])
def test_abandoning_download_captcha_never_submits(h, command):
    async def run():
        await h.search()
        await h.click("1")
        selected = h.state.selected
        h.client.download_outcomes = [AuthenticationRequired("expired")]
        h.client.auth_outcomes = [CaptchaChallenge("download", 1, 10, b"image", "image/png", 1300)]
        await h.click("Download")
        await h.send(command)
        assert h.store.get_selection(selected.id, 1, 10).status == SelectionStatus.FAILED
        assert not h.rpc.adds

    asyncio.run(run())


@pytest.mark.parametrize(
    "phase,expected", [("fetch", SelectionStatus.FAILED), ("add", SelectionStatus.UNCERTAIN)]
)
def test_cancel_download_updates_durable_state_and_reports_status(h, phase, expected):
    async def run():
        await h.search()
        await h.click("1")
        selected = h.state.selected
        if phase == "fetch":
            h.client.download_wait = asyncio.Event()
            entered = h.client.download_entered
        else:
            h.rpc.wait = asyncio.Event()
            entered = h.rpc.entered
        task = asyncio.create_task(h.click("Download"))
        await entered.wait()
        await h.send("/cancel")
        await asyncio.gather(task, return_exceptions=True)
        assert not h.flow.sessions
        assert h.store.get_selection(selected.id, 1, 10).status == expected
        assert len(h.rpc.adds) == (1 if phase == "add" else 0)
        await h.send("/status")
        assert ("uncertain" if phase == "add" else "failed") in h.bot.messages[-1]["text"]

    asyncio.run(run())


def test_status_after_restart_is_scoped_and_read_only(h):
    async def run():
        await h.search()
        await h.click("1")
        h.rpc.add_error = SubmissionUncertain(HASH)
        await h.click("Download")
        assert "uncertain" in h.bot.messages[-1]["text"]
        await h.flow.close()
        h.rpc.found = TransmissionTorrent(7, "test.bin", HASH)
        await h.send("/status", user_id=2)
        assert "No recent" in h.bot.messages[-1]["text"]
        await h.send("/status")
        assert "Confirmed present" in h.bot.messages[-1]["text"]
        assert not h.flow.sessions and len(h.rpc.adds) == 1

    asyncio.run(run())


def test_download_rejection_is_safe_and_avoids_secret_logging(h, caplog):
    async def run():
        await h.search()
        await h.click("1")
        h.rpc.add_error = TransmissionRejected("private-response")
        with caplog.at_level(logging.INFO):
            await h.click("Download")
        assert h.state.selected.status == SelectionStatus.FAILED
        assert "private-response" not in caplog.text
        assert "private-response" not in h.bot.messages[-1]["text"]
        assert DATA.decode() not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("label", ["Download", "Sequential download"])
def test_confirmed_download_survives_replacing_rejected_login(h, label):
    async def run():
        await h.search()
        await h.click("1")
        selected = h.state.selected
        h.client.download_outcomes = [AuthenticationRequired("expired")]
        h.client.auth_outcomes = [InvalidCredentials("rejected")]
        await h.click(label)
        assert h.state.stage == "login" and h.state.download_claim is not None
        assert not h.rpc.adds
        await h.send("replacement-user")
        await h.send("replacement-password")
        assert h.state.selected.id == selected.id
        assert h.state.selected.status == SelectionStatus.ACCEPTED
        assert h.client.downloads == [1] and len(h.rpc.adds) == 1
        assert h.rpc.sequential_adds == [label == "Sequential download"]

    asyncio.run(run())


def test_foreign_and_restart_download_controls_never_fetch_torrent(h):
    async def run():
        await h.search()
        await h.click("1")
        data = h.button("Download")
        await h.flow.handle_callback(callback(data, user_id=2))
        await h.flow.handle_callback(callback(data, chat_id=20))
        fresh = TelegramConversation(h.bot, h.store, frozenset({1}), h.factory, workflow=h.workflow)
        await fresh.handle_callback(callback(data))
        assert not h.client.downloads and not h.rpc.adds

    asyncio.run(run())


@pytest.mark.parametrize("active", [True, False])
def test_expired_telegram_callback_is_quiet_and_never_starts_download(h, active, caplog):
    async def run():
        if active:
            await h.search()
            await h.click("1")
            data = h.button("Download")
        else:
            data = "sc:old:download"

        async def expired(**kwargs):
            raise TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="expired"),
                message=(
                    "Bad Request: query is too old and response timeout expired "
                    "or query ID is invalid"
                ),
            )

        h.bot.answer_callback_query = expired
        with caplog.at_level(logging.INFO):
            await h.flow.handle_callback(callback(data))
        assert "telegram.callback.expired" in caplog.text
        assert not h.rpc.adds
        if active:
            assert not h.client.downloads
            assert h.state.task is None
            assert (
                h.store.get_selection(h.state.selected.id, 1, 10).status == SelectionStatus.PENDING
            )

    asyncio.run(run())


def test_start_checks_login_before_menu_without_transmission_requests(h):
    async def run():
        await h.send("/start")
        assert h.state.stage == "menu"
        assert h.bot.messages[0]["text"] == "Checking RuTracker login."
        assert h.state.actions == ("search", "downloads")
        assert h.bot.messages[-1]["text"] == "Menu"
        assert h.client.ensure_calls == 1 and not h.rpc.adds and not h.rpc.lookups
        await h.click("search")
        assert h.state.stage == "query" and h.client.ensure_calls == 2
        await h.send("  exact query  ")
        assert h.client.queries == ["  exact query  "]
        await h.flow.close()

    asyncio.run(run())


def test_menu_login_branch_returns_to_menu_and_hides_login(tmp_path):
    h = Harness(tmp_path, credentials=False)

    async def run():
        await h.send("/start")
        await h.click("log in")
        assert h.state.stage == "login"
        await h.send("new-login")
        await h.send("new-password")
        assert h.state.stage == "menu" and h.state.authenticated
        assert h.state.actions == ("search", "downloads")
        assert h.store.get_credentials(1) == RuTrackerCredentials("new-login", "new-password")
        assert h.client.check_calls == 1
        await h.send("/start")
        assert not h.client.closed and h.client.check_calls == 2
        assert "login" not in h.state.actions
        await h.click("search")
        assert h.state.stage == "query"
        await h.flow.close()

    asyncio.run(run())


def test_menu_login_captcha_and_invalid_credentials_resume_menu(h):
    async def run():
        h.next_client.auth_outcomes = [InvalidCredentials("bad")]
        await h.send("/start")
        await h.click("log in")
        assert h.state.stage == "login"
        h.next_client.auth_outcomes = [CaptchaChallenge("login", 1, 10, b"img", "image/png", 1300)]
        await h.send("replacement-user")
        await h.send("replacement-password")
        assert h.state.stage == "captcha"
        await h.send("answer")
        assert h.state.stage == "menu" and "login" not in h.state.actions
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", [False, NetworkError("offline")])
def test_menu_checks_expired_or_unavailable_rutracker_session(h, outcome):
    async def run():
        await h.search()
        old = h.button("1")
        client = h.client
        client.check_outcomes = [outcome]
        await h.send("/start")
        assert h.state.stage == "menu" and "login" in h.state.actions
        assert client.closed and not h.state.authenticated
        await h.flow.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["populated", "empty", "offline"])
def test_downloads_branch_works_without_rutracker_login_and_returns_to_menu(tmp_path, state):
    h = Harness(tmp_path, credentials=False)
    from tg_torrent_downloader.transmission import ActiveDownload, TransmissionUnavailable

    async def run():
        h.rpc.active = (
            (ActiveDownload(7, "Ubuntu <literal>", 4, 0.25, 2048, 120),)
            if state == "populated"
            else ()
        )
        if state == "offline":
            h.rpc.active_error = TransmissionUnavailable("offline")
        await h.send("/start")
        await h.click("downloads")
        assert h.state.stage == "downloads" and h.state.actions == (
            "reload",
            "menu",
        )
        text = h.bot.messages[-1]["text"]
        if state == "populated":
            assert "Ubuntu <literal> G" in text and "25.0%" in text and "2.0 KiB/s" in text
            assert "(ID " not in text
            entity = h.bot.messages[-1]["entities"][0]
            assert parse_qs(urlsplit(entity.url).query) == {"q": ["Ubuntu <literal>"]}
            encoded = text.encode("utf-16-le")
            assert (
                encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
                == "G"
            )
        elif state == "empty":
            assert "No active downloads" in text
        else:
            assert "Could not load" in text
        assert h.rpc.closed and not h.rpc.adds and not h.rpc.lookups and not h.clients
        await h.click("menu")
        assert h.state.stage == "menu" and "login" in h.state.actions
        await h.flow.close()

    asyncio.run(run())


def test_downloads_return_preserves_authenticated_session_and_invalidates_old_menu(h):
    async def run():
        await h.send("/start")
        old_downloads = h.button("downloads")
        client = h.client
        await h.click("downloads")
        await h.click("menu")
        assert h.state.actions == ("search", "downloads") and not client.closed
        await h.flow.handle_callback(callback(old_downloads))
        assert "expired" in h.bot.answers[-1]["text"]
        assert client.ensure_calls == 1
        await h.flow.close()

    asyncio.run(run())


def test_another_user_cannot_read_downloads_with_foreign_menu(h):
    async def run():
        await h.send("/start")
        await h.flow.handle_callback(callback(h.button("downloads"), user_id=2))
        assert "expired" in h.bot.answers[-1]["text"]
        assert h.rpc.active_calls == 0
        await h.flow.close()

    asyncio.run(run())


def test_downloads_long_list_retains_menu_on_final_chunk(h):
    from tg_torrent_downloader.transmission import ActiveDownload

    async def run():
        h.rpc.active = tuple(
            ActiveDownload(i, "long name 🙂" * 70, 3, 0.1, 0, -1) for i in range(12)
        )
        await h.send("/start")
        before = len(h.bot.messages)
        await h.click("downloads")
        chunks = h.bot.messages[before + 1 :]
        assert len(chunks) > 1
        assert all(item["reply_markup"] is None for item in chunks[:-1])
        assert chunks[-1]["reply_markup"] is not None
        links = 0
        for chunk in chunks:
            encoded = chunk["text"].encode("utf-16-le")
            for entity in chunk.get("entities") or []:
                assert (
                    encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
                        "utf-16-le"
                    )
                    == "G"
                )
                assert parse_qs(urlsplit(entity.url).query)["q"] == ["long name 🙂" * 70]
                links += 1
        assert links == 12
        await h.click("menu")
        assert h.state.stage == "menu"
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_start_abandons_pending_selection_and_expired_selection_does_not_block_menu(h, expired):
    async def run():
        await h.search()
        await h.click("1")
        selected, client = h.state.selected, h.client
        if expired:
            h.now[0] += 901
            h.state.touched_at = h.now[0]  # UI is active, but its search snapshot has expired.
        await h.send("/start")
        assert h.state.stage == "menu" and h.state.selected is None
        assert not h.rpc.adds and not client.closed
        if not expired:
            assert h.store.get_selection(selected.id, 1, 10).status == SelectionStatus.CANCELLED
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("initial", ["empty", "offline", "populated"])
def test_reload_fetches_fresh_torrents_and_shows_seeding(h, initial):
    from tg_torrent_downloader.transmission import ActiveDownload, TransmissionUnavailable

    async def run():
        if initial == "offline":
            h.rpc.active_error = TransmissionUnavailable("offline")
        elif initial == "populated":
            h.rpc.active = (ActiveDownload(1, "old name", 4, 0.5, 10, -1),)
        await h.send("/start")
        await h.click("downloads")
        old_reload = h.button("reload")
        h.rpc.active_error = None
        h.rpc.active = (ActiveDownload(9, "Now seeding", 6, 1.0, 0, -1, 4096),)
        await h.click("reload")
        assert h.rpc.active_calls == 2
        assert "Now seeding" in h.bot.messages[-1]["text"]
        assert "Seeding" in h.bot.messages[-1]["text"]
        assert "100.0%" in h.bot.messages[-1]["text"]
        assert "↑ 4.0 KiB/s" in h.bot.messages[-1]["text"]
        assert h.state.actions == ("reload", "menu")
        await h.flow.handle_callback(callback(old_reload))
        assert "expired" in h.bot.answers[-1]["text"]
        assert h.rpc.active_calls == 2 and not h.rpc.adds
        await h.click("menu")
        assert h.state.stage == "menu"
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["missing", "invalid", "offline", "captcha"])
def test_start_login_check_handles_missing_invalid_offline_and_captcha(tmp_path, outcome):
    h = Harness(tmp_path, credentials=outcome != "missing")

    async def run():
        if outcome == "invalid":
            h.next_client.auth_outcomes = [InvalidCredentials("bad")]
        elif outcome == "offline":
            h.next_client.auth_outcomes = [NetworkError("offline")]
        elif outcome == "captcha":
            h.next_client.auth_outcomes = [
                CaptchaChallenge("start", 1, 10, b"image", "image/png", 1300)
            ]
        await h.send("/start")
        assert h.bot.messages[0]["text"] == "Checking RuTracker login."
        if outcome == "captcha":
            assert h.state.stage == "captcha" and h.state.resume == "menu"
            await h.send("answer")
            assert h.state.stage == "menu" and "login" not in h.state.actions
        else:
            assert h.state.stage == "menu" and "login" in h.state.actions
            assert h.state.task is None
            if outcome == "invalid":
                assert h.store.get_credentials(1) is None
            await h.click("downloads")
            assert h.state.stage == "downloads"
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("photo_fails", [False, True])
def test_detail_poster_precedes_description_and_preserves_controls(h, monkeypatch, photo_fails):
    async def run():
        await h.search()
        result = h.state.search.results[0]
        h.client.topic_outcomes = [
            TopicDetails(result, "Main description", "https://example.com/poster.jpg")
        ]
        order = []
        send_photo, send_message = h.bot.send_photo, h.bot.send_message

        async def photo(**kwargs):
            order.append("photo")
            if photo_fails:
                raise TelegramBadRequest(
                    method=AnswerCallbackQuery(callback_query_id="photo"), message="wrong photo"
                )
            await send_photo(**kwargs)

        async def text(**kwargs):
            order.append("text")
            await send_message(**kwargs)

        monkeypatch.setattr(h.bot, "send_photo", photo)
        monkeypatch.setattr(h.bot, "send_message", text)
        await h.click("1")
        assert order == ["text", "photo", "text"]
        assert h.state.stage == "description" and h.state.actions == (
            "back",
            "download",
            "sequential",
        )
        assert "Main description" in h.bot.messages[-1]["text"]
        if photo_fails:
            assert "Poster: https://example.com/poster.jpg" in h.bot.messages[-1]["text"]
        else:
            assert h.bot.photos[-1]["photo"] == "https://example.com/poster.jpg"
        assert not h.rpc.adds and not h.client.downloads
        await h.click("Back")
        assert h.state.stage == "results"
        await h.flow.close()

    asyncio.run(run())


def test_long_expandable_detail_sections_keep_full_text_and_valid_entities(h):
    async def run():
        await h.search()
        result = h.state.search.results[0]
        blocks = (
            TopicBlock("Intro 🙂 <literal>"),
            TopicBlock("Media info\n" + "Technical 🙂 data\n" * 500, True),
            TopicBlock("Скриншоты\nhttps://example.com/shot.jpg", True),
            TopicBlock("After sections"),
        )
        description = "\n\n".join(block.text for block in blocks)
        h.client.topic_outcomes = [TopicDetails(result, description, blocks=blocks)]
        before = len(h.bot.messages)
        await h.click("1")
        responses = h.bot.messages[before + 1 :]
        assert description in "".join(item["text"] for item in responses)
        quoted = []
        for item in responses:
            assert len(item["text"].encode("utf-16-le")) // 2 <= 4000
            encoded = item["text"].encode("utf-16-le")
            for entity in item.get("entities") or []:
                assert entity.type == "expandable_blockquote"
                assert 0 <= entity.offset < entity.offset + entity.length <= len(encoded) // 2
                quoted.append(
                    encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
                        "utf-16-le"
                    )
                )
        assert "".join(quoted) == blocks[1].text + blocks[2].text
        assert all(item["reply_markup"] is None for item in responses[:-1])
        assert responses[-1]["reply_markup"] is not None
        await h.send("another query")
        assert h.state.stage == "results" and h.client.queries[-1] == "another query"
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "uncertain"])
def test_submission_has_one_short_message_and_results_downloads_navigation(h, outcome):
    async def run():
        await h.search()
        original = h.state.search
        await h.click("1")
        if outcome == "rejected":
            h.rpc.add_error = TransmissionRejected("rejected")
        elif outcome == "uncertain":
            h.rpc.add_error = SubmissionUncertain(HASH)
        before = len(h.bot.messages)
        await h.click("Download")
        messages = h.bot.messages[before:]
        assert len(messages) == 1
        expected = {
            "accepted": "Accepted by Transmission.",
            "rejected": "Not accepted by Transmission.",
            "uncertain": "Transmission acceptance is uncertain. Use /status to check.",
        }[outcome]
        assert messages[0]["text"] == expected
        assert [
            button.text for row in messages[0]["reply_markup"].inline_keyboard for button in row
        ] == ["search results", "downloads"]
        await h.click("search results")
        assert h.state.stage == "results" and h.state.search == original
        assert h.client.queries == ["ubuntu"]
        # A newly selected topic still requires its own explicit Download confirmation.
        await h.click("2")
        h.rpc.add_error = None
        await h.click("Download")
        await h.click("downloads")
        assert h.state.stage == "downloads" and h.state.actions == ("reload", "menu")
        await h.flow.close()

    asyncio.run(run())


def test_pagination_uses_cached_pages_and_fetches_only_at_server_boundary(h):
    async def run():
        first = tuple(TorrentResult(i, f"Release {i}", seeds=100 - i) for i in range(1, 51))
        last = tuple(TorrentResult(i, f"Release {i}", seeds=100 - i) for i in range(51, 64))
        h.next_client.batch_outcomes = {
            0: [SearchBatch(first, next_offset=50)],
            50: [SearchBatch(last, 50)],
        }
        await h.search(" original ")
        assert h.state.search_session.current_page == 1 and "prev" not in h.state.actions
        assert h.state.actions[-1] == "next"
        rows = h.bot.messages[-1]["reply_markup"].inline_keyboard
        assert [button.text for button in rows[-1]] == ["next"]
        for page in range(2, 6):
            await h.click("next")
            assert h.state.search.results[0].topic_id == (page - 1) * 10 + 1
        assert h.client.batch_calls == [(" original ", 0)]
        await h.click("next")
        assert h.state.search_session.current_page == 6
        assert h.client.batch_calls == [(" original ", 0), (" original ", 50)]
        sixth = h.state.search
        await h.send("2")
        assert h.state.selected.result.topic_id == 52
        await h.click("Back")
        assert h.state.search == sixth and h.state.search_session.current_page == 6
        await h.click("next")
        assert len(h.state.search.results) == 3 and "next" not in h.state.actions
        assert h.state.search_session.current_page == 7
        await h.click("previous")
        assert h.state.search == sixth and len(h.client.batch_calls) == 2
        await h.click("1")
        await h.click("Download")
        await h.click("search results")
        assert h.state.search == sixth and h.state.search_session.current_page == 6
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("text", ["next", "previous", "11", "  another query  "])
def test_arbitrary_text_on_later_page_starts_a_fresh_search(h, text):
    async def run():
        h.next_client.results += tuple(TorrentResult(i, f"Release {i}") for i in range(11, 21))
        await h.search()
        await h.click("next")
        previous = h.state.search_session.id
        await h.send(text)
        assert h.client.queries[-1] == text
        assert h.state.search_session.id != previous
        assert h.state.search_session.current_page == 1
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("captcha", [False, True])
def test_page_fetch_reauthenticates_and_resumes_the_requested_page(h, captcha):
    async def run():
        h.next_client.batch_outcomes = {
            0: [SearchBatch(h.next_client.results, next_offset=50)],
            50: [AuthenticationRequired("expired"), SearchBatch((TorrentResult(11, "Next"),), 50)],
        }
        await h.search()
        if captcha:
            h.client.auth_outcomes = [CaptchaChallenge("page", 1, 10, b"image", "image/png", 1300)]
        await h.click("next")
        if captcha:
            assert h.state.stage == "captcha" and h.state.pending_page == 2
            await h.send("answer")
        assert h.state.stage == "results" and h.state.search_session.current_page == 2
        assert h.state.search.results[0].topic_id == 11
        assert h.client.batch_calls == [("ubuntu", 0), ("ubuntu", 50), ("ubuntu", 50)]
        await h.flow.close()

    asyncio.run(run())


def test_page_network_error_keeps_current_page_and_retry_target(h):
    async def run():
        h.next_client.batch_outcomes = {
            0: [SearchBatch(h.next_client.results, next_offset=50)],
            50: [NetworkError("offline"), SearchBatch((TorrentResult(11, "Next"),), 50)],
        }
        await h.search()
        original = h.state.search
        await h.click("next")
        assert h.state.search == original and h.state.search_session.current_page == 1
        assert "Could not load" in h.bot.messages[-1]["text"]
        assert h.state.pending_page == 2 and "retry" in h.state.actions
        await h.click("Retry")
        assert h.state.search_session.current_page == 2
        await h.flow.close()

    asyncio.run(run())


def test_empty_server_continuation_hides_next_without_advancing(h):
    async def run():
        h.next_client.batch_outcomes = {
            0: [SearchBatch(h.next_client.results, next_offset=50)],
            50: [SearchBatch((), 50)],
        }
        await h.search()
        original = h.state.search
        await h.click("next")
        assert h.state.search == original and h.state.search_session.current_page == 1
        assert "next" not in h.state.actions and "No more results" in h.bot.messages[-1]["text"]
        await h.flow.close()

    asyncio.run(run())


def test_old_and_foreign_pagination_controls_cannot_navigate(h):
    async def run():
        h.next_client.results += tuple(TorrentResult(i, f"Release {i}") for i in range(11, 21))
        await h.search()
        old = h.button("next")
        await h.flow.handle_callback(callback(old, user_id=2))
        assert h.state.search_session.current_page == 1
        await h.click("next")
        await h.flow.handle_callback(callback(old))
        assert "expired" in h.bot.answers[-1]["text"]
        assert h.state.search_session.current_page == 2
        await h.flow.close()

    asyncio.run(run())


def test_double_next_clicks_fetch_the_continuation_once(h, monkeypatch):
    async def run():
        h.next_client.batch_outcomes = {0: [SearchBatch(h.next_client.results, next_offset=50)]}
        await h.search()
        data = h.button("next")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(query, *, offset=0):
            calls.append(offset)
            entered.set()
            await release.wait()
            return SearchBatch((TorrentResult(11, "Next"),), offset)

        monkeypatch.setattr(h.client, "search_batch", fetch)
        task = asyncio.create_task(h.flow.handle_callback(callback(data)))
        await entered.wait()
        await h.flow.handle_callback(callback(data))
        assert "Working" in h.bot.answers[-1]["text"]
        release.set()
        await task
        assert calls == [50] and h.state.search_session.current_page == 2
        await h.flow.close()

    asyncio.run(run())


def test_rejected_login_during_paging_resumes_same_cursor_with_new_credentials(h):
    async def run():
        h.next_client.batch_outcomes = {
            0: [SearchBatch(h.next_client.results, next_offset=50)],
            50: [AuthenticationRequired("expired")],
        }
        await h.search(" saved query ")
        original = h.state.search
        h.client.auth_outcomes = [InvalidCredentials("bad")]
        h.next_client.batch_outcomes = {50: [SearchBatch((TorrentResult(11, "Next"),), 50)]}
        await h.click("next")
        assert h.state.stage == "login" and h.state.search == original
        await h.send("replacement-user")
        await h.send("replacement-password")
        assert h.state.search_session.current_page == 2
        assert h.client.batch_calls == [(" saved query ", 50)]
        assert h.state.search.results[0].topic_id == 11
        assert h.store.get_credentials(1).username == "replacement-user"
        await h.flow.close()

    asyncio.run(run())


@pytest.mark.parametrize("label, sequential", [("Download", False), ("Sequential download", True)])
def test_download_mode_from_details(h, label, sequential):
    async def run():
        await h.search()
        await h.send("1")
        assert h.state.actions == ("back", "download", "sequential")
        await h.click(label)
        assert h.rpc.sequential_adds == [sequential]
        assert h.bot.messages[-1]["text"] == "Accepted by Transmission."

    asyncio.run(run())


def test_result_page_total_uses_reported_count_and_survives_back(h):
    async def run():
        first = tuple(TorrentResult(i, f"Release {i}") for i in range(1, 51))
        h.next_client.batch_outcomes = {0: [SearchBatch(first, next_offset=50, total_results=123)]}
        await h.search()
        assert h.bot.messages[-1]["text"] == "Page 1 / 13"
        await h.click("next")
        assert h.bot.messages[-1]["text"] == "Page 2 / 13"
        await h.click("1")
        await h.click("Back")
        assert h.bot.messages[-1]["text"] == "Page 2 / 13"
        assert h.client.batch_calls == [("ubuntu", 0)]

    asyncio.run(run())
