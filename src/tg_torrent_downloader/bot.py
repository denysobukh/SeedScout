"""Long-polling application lifecycle and aiogram routing."""

import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError

from tg_torrent_downloader.config import Settings
from tg_torrent_downloader.conversation import TelegramConversation
from tg_torrent_downloader.logging import diagnostic_event
from tg_torrent_downloader.rutracker import RuTrackerClient
from tg_torrent_downloader.storage import WorkflowStore
from tg_torrent_downloader.transmission import TransmissionClient
from tg_torrent_downloader.transmission_app import (
    TransmissionLaunchError,
    ensure_transmission_running,
)
from tg_torrent_downloader.workflow import DownloadWorkflow


def create_router(conversation: TelegramConversation) -> Router:
    router = Router(name="seed_scout")
    router.message.register(conversation.handle_message)
    router.callback_query.register(conversation.handle_callback)
    return router


async def run_bot(settings: Settings) -> None:
    store = WorkflowStore(settings.database_path)
    with store.runtime_lock():
        await asyncio.to_thread(store.initialize)
        workflow = DownloadWorkflow(store, lambda: TransmissionClient.from_settings(settings))
        await _poll(settings, store, workflow)


async def _poll(settings: Settings, store: WorkflowStore, workflow: DownloadWorkflow) -> None:
    bot = Bot(settings.telegram_bot_token)
    conversation = TelegramConversation(
        bot,
        store,
        settings.telegram_allowed_user_ids,
        lambda user_id, chat_id, username, password: RuTrackerClient(
            username,
            password,
            user_id=user_id,
            chat_id=chat_id,
        ),
        workflow=workflow,
    )
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(create_router(conversation))

    async def cleanup_idle():
        while True:
            await asyncio.sleep(60)
            await conversation.reap_idle()

    cleaner = asyncio.create_task(cleanup_idle())
    try:
        notice = None
        try:
            if await ensure_transmission_running(settings.transmission_app_path):
                notice = "Transmission was not running, so I started it."
        except TransmissionLaunchError as error:
            diagnostic_event("transmission.app.failed", error=type(error).__name__)
            notice = "Transmission could not be started automatically. " + str(error)
        if notice:
            for user_id in sorted(settings.telegram_allowed_user_ids):
                try:
                    await bot.send_message(chat_id=user_id, text=notice, parse_mode=None)
                except TelegramAPIError as error:
                    diagnostic_event(
                        "transmission.app.notification.failed",
                        user_id=user_id,
                        error=type(error).__name__,
                    )
        await workflow.recover_startup()
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
