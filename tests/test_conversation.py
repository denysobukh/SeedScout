import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from tg_torrent_downloader.conversation import TelegramConversation, split_text
from tg_torrent_downloader.models import SelectionStatus, TorrentResult
from tg_torrent_downloader.rutracker import (
    AuthenticationRequired,
    CaptchaChallenge,
    CaptchaExpired,
    InvalidCredentials,
    NetworkError,
    TopicDetails,
    TopicUnavailable,
)
from tg_torrent_downloader.storage import WorkflowStore


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
        self.queries, self.topics, self.answers = [], [], []
        self.auth_outcomes, self.search_outcomes, self.topic_outcomes, self.captcha_outcomes = (
            [],
            [],
            [],
            [],
        )
        self.results = tuple(
            TorrentResult(i, f"Release {i}", size="1 GB", seeds=2) for i in range(1, 13)
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

    async def get_topic(self, result):
        self.topics.append(result)
        return self.outcome(self.topic_outcomes, TopicDetails(result, self.description))

    async def download_torrent(self, topic_id):
        raise AssertionError("Step 4 must never retrieve torrent bytes")


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
    def __init__(self, tmp_path):
        self.now = [1000.0]
        self.store = WorkflowStore(tmp_path / "test.db", clock=lambda: self.now[0])
        self.store.initialize()
        self.bot = FakeBot()
        self.clients = []
        self.next_client = FakeTracker()

        def factory(user, chat):
            client = self.next_client
            self.next_client = FakeTracker()
            self.clients.append(client)
            return client

        self.factory = factory
        self.flow = TelegramConversation(
            self.bot, self.store, frozenset({1, 2}), factory, clock=lambda: self.now[0]
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
                        if button.text == text and self.state.token in button.callback_data:
                            return button.callback_data
        raise AssertionError(f"No active {text} button")

    async def send(self, text, **kwargs):
        await self.flow.handle_message(message(text, **kwargs))

    async def click(self, text, **kwargs):
        await self.flow.handle_callback(callback(self.button(text), **kwargs))

    async def search(self, query="ubuntu"):
        await self.send("/start")
        await self.send(query)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_start_authenticates_before_prompting_for_query(h):
    async def run():
        await h.send("/start")
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
        text = h.bot.messages[-1]["text"]
        assert "1. Release 1" in text and "10. Release 10" in text
        assert "11. Release" not in text
        assert "1 GB" in text and "Seeds: 2" in text

    asyncio.run(run())


def test_description_and_back_preserve_search_and_numbering(h):
    async def run():
        await h.search()
        original = h.state.search
        results_text = h.bot.messages[-1]["text"]
        await h.send("2")
        assert h.state.stage == "description"
        assert h.client.topics[0].topic_id == 2
        assert "Full description <b>literal text</b>" in h.bot.messages[-1]["text"]
        assert h.bot.messages[-1]["parse_mode"] is None
        assert h.bot.messages[-1]["link_preview_options"].is_disabled
        await h.click("Back")
        assert h.state.search == original
        assert h.bot.messages[-1]["text"] == results_text
        assert h.client.queries == ["ubuntu"]

    asyncio.run(run())


def test_download_placeholder_does_not_claim_or_fetch_torrent(h):
    async def run():
        await h.search()
        await h.send("1")
        selected = h.state.selected
        await h.click("Download")
        await h.click("Download")
        assert "not enabled" in h.bot.messages[-1]["text"]
        stored = h.store.get_selection(selected.id, 1, 10)
        assert stored.status == SelectionStatus.PENDING
        assert stored.claim_token is None

    asyncio.run(run())


@pytest.mark.parametrize("number", ["0", "11", "-1", "text", "1.5", "١", "9" * 100])
def test_invalid_result_numbers_do_not_open_topic(h, number):
    async def run():
        await h.search()
        await h.send(number)
        assert not h.client.topics
        assert "number from 1 to 10" in h.bot.messages[-1]["text"]

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
        await h.send("/start")
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
        await h.send("/start")
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


def test_invalid_credentials_do_not_offer_automatic_retry(h):
    async def run():
        h.next_client.auth_outcomes = [InvalidCredentials("bad")]
        await h.send("/start")
        assert "retry" not in h.state.actions
        assert h.state.stage == "error"
        assert "configured credentials" in h.bot.messages[-1]["text"]

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
        old = h.button("New search")
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
        fresh = TelegramConversation(h.bot, h.store, frozenset({1}), h.factory)
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
        task = asyncio.create_task(h.send("/start"))
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
        await h.send("/start")
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
        data = h.button("New search")
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
        assert h.client.ensure_calls == 2  # Initial auth plus exactly one new search.

    asyncio.run(run())


def test_old_controls_and_unknown_actions_cannot_start_work(h):
    async def run():
        await h.send("/start")
        forged = f"sc:{h.state.token}:download"
        await h.flow.handle_callback(callback(forged))
        assert "expired" in h.bot.answers[-1]["text"]
        assert not h.client.topics and not h.client.queries
        old = h.button("Cancel")
        await h.send("/start")
        assert h.clients[0].closed
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
        await h.click("Cancel")
        assert not h.flow.sessions and h.client.closed
        assert h.store.get_selection(selected.id, 1, 10).status == SelectionStatus.CANCELLED

    asyncio.run(run())
