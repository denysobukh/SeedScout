import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_torrent_downloader.config import Settings


def test_polling_lifecycle_initializes_storage_and_closes_bot(tmp_path, monkeypatch):
    import tg_torrent_downloader.bot as module

    settings = Settings.load(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "RUTRACKER_USERNAME": "user",
            "RUTRACKER_PASSWORD": "password",
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
