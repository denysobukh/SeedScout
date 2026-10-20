"""Long-polling application lifecycle and aiogram routing."""

import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher, Router

from tg_torrent_downloader.config import Settings
from tg_torrent_downloader.conversation import TelegramConversation
from tg_torrent_downloader.rutracker import RuTrackerClient
from tg_torrent_downloader.storage import WorkflowStore


def create_router(conversation: TelegramConversation) -> Router:
    router = Router(name="seed_scout")
    router.message.register(conversation.handle_message)
    router.callback_query.register(conversation.handle_callback)
    return router


async def run_bot(settings: Settings) -> None:
    store = WorkflowStore(settings.database_path)
    await asyncio.to_thread(store.initialize)
    bot = Bot(settings.telegram_bot_token)
    conversation = TelegramConversation(
        bot,
        store,
        settings.telegram_allowed_user_ids,
        lambda user_id, chat_id: RuTrackerClient(
            settings.rutracker_username,
            settings.rutracker_password,
            user_id=user_id,
            chat_id=chat_id,
        ),
    )
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(create_router(conversation))

    async def cleanup_idle():
        while True:
            await asyncio.sleep(60)
            await conversation.reap_idle()

    cleaner = asyncio.create_task(cleanup_idle())
    try:
        await dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"], close_bot_session=False
        )
    finally:
        cleaner.cancel()
        with suppress(asyncio.CancelledError):
            await cleaner
        try:
            await conversation.close()
        finally:
            await bot.session.close()
