import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_torrent_downloader.config import Settings


def test_polling_lifecycle_initializes_storage_and_closes_bot(tmp_path, monkeypatch):
    import tg_torrent_downloader.bot as module

    settings = Settings.load(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "DATABASE_PATH": str(tmp_path / "state.db"),
        },
        env_file=None,
    )
    fake_bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    poll = AsyncMock()
    dispatcher = SimpleNamespace(include_router=lambda router: None, start_polling=poll)
    monkeypatch.setattr(module, "Bot", lambda token: fake_bot)
    monkeypatch.setattr(module, "Dispatcher", lambda **kwargs: dispatcher)
    asyncio.run(module.run_bot(settings))
    assert settings.database_path.exists()
    poll.assert_awaited_once()
    assert poll.call_args.kwargs["allowed_updates"] == ["message", "callback_query"]
    fake_bot.session.close.assert_awaited_once()


@pytest.mark.parametrize("state", ["running", "started", "failed"])
def test_transmission_checked_before_recovery_and_start_is_reported(tmp_path, monkeypatch, state):
    import tg_torrent_downloader.bot as module
    from tg_torrent_downloader.transmission_app import TransmissionLaunchError

    settings = Settings.load(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
            "TELEGRAM_ALLOWED_USER_IDS": "1,2",
            "DATABASE_PATH": str(tmp_path / "bot.db"),
            "TRANSMISSION_APP_PATH": "/Applications/Transmission.app",
        },
        env_file=None,
    )
    events = []

    async def ensure(path):
        assert str(path) == "/Applications/Transmission.app"
        events.append("app")
        if state == "failed":
            raise TransmissionLaunchError("The configured app could not be read.")
        return state == "started"

    async def recover(self):
        events.append("recovery")

    async def poll(*args, **kwargs):
        events.append("poll")

    fake_bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), send_message=AsyncMock())
    monkeypatch.setattr(module, "ensure_transmission_running", ensure)
    monkeypatch.setattr(module.DownloadWorkflow, "recover_startup", recover)
    monkeypatch.setattr(module, "Bot", lambda token: fake_bot)
    monkeypatch.setattr(
        module,
        "Dispatcher",
        lambda **kwargs: SimpleNamespace(include_router=lambda router: None, start_polling=poll),
    )
    asyncio.run(module.run_bot(settings))
    assert events == ["app", "recovery", "poll"]
    assert fake_bot.send_message.await_count == (0 if state == "running" else 2)
    if state != "running":
        assert [call.kwargs["chat_id"] for call in fake_bot.send_message.await_args_list] == [1, 2]
        expected = "I started it" if state == "started" else "could not be started"
        assert expected in fake_bot.send_message.await_args.kwargs["text"]
    fake_bot.session.close.assert_awaited_once()


def test_blocked_startup_notification_does_not_prevent_polling(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    import tg_torrent_downloader.bot as module

    settings = Settings.load(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
            "TELEGRAM_ALLOWED_USER_IDS": "1,2",
            "DATABASE_PATH": str(tmp_path / "bot.db"),
        },
        env_file=None,
    )
    fake_bot = SimpleNamespace(
        session=SimpleNamespace(close=AsyncMock()),
        send_message=AsyncMock(
            side_effect=[
                TelegramForbiddenError(
                    method=SendMessage(chat_id=1, text="notice"), message="Forbidden"
                ),
                None,
            ]
        ),
    )
    polling = AsyncMock()
    monkeypatch.setattr(module, "ensure_transmission_running", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "Bot", lambda token: fake_bot)
    monkeypatch.setattr(
        module,
        "Dispatcher",
        lambda **kwargs: SimpleNamespace(include_router=lambda router: None, start_polling=polling),
    )
    asyncio.run(module.run_bot(settings))
    assert fake_bot.send_message.await_count == 2
    polling.assert_awaited_once()
    fake_bot.session.close.assert_awaited_once()
