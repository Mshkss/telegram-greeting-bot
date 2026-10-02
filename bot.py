import asyncio
import logging

from aiogram import Dispatcher

from app.config import Settings
from app.analytics import ActivityMiddleware
from app.db import Database
from app.handlers import build_router
from app.telegram import ProxySession, create_bot


async def main():
    settings = Settings.load()
    db = await Database.connect(settings)
    try:
        await db.initialize()
        async with create_bot(settings, db) as bot:
            if (await bot.get_webhook_info()).url:
                raise RuntimeError('Удалите webhook перед запуском polling.')
            dp = Dispatcher()
            dp.update.outer_middleware(ActivityMiddleware(db))
            dp.include_router(build_router(db))
            # One polling process; sequential updates prevent onboarding races.
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'],
                                   polling_timeout=30, handle_as_tasks=False)
    finally:
        await db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
