"""Greeting bot using the authenticated Cloudflare Telegram API proxy."""

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message
from dotenv import load_dotenv


class ProxySession(AiohttpSession):
    def __init__(self, base_url: str, secret: str):
        super().__init__(api=TelegramAPIServer.from_base(base_url), timeout=60)
        self.proxy_secret = secret

    async def create_session(self):
        session = await super().create_session()
        session.headers["Authorization"] = f"Bearer {self.proxy_secret}"
        return session


async def greet(message: Message) -> None:
    await message.answer("Привет! 👋")


async def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    required = ("BOT_TOKEN", "TELEGRAM_API_BASE", "TELEGRAM_PROXY_SECRET")
    config = {key: os.getenv(key, "").strip() for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise ValueError("Заполните переменные в .env: " + ", ".join(missing))

    base_url = config["TELEGRAM_API_BASE"].rstrip("/")
    url = urlsplit(base_url)
    if (url.scheme != "https" or not url.hostname or url.username
            or url.password or url.path or url.query or url.fragment):
        raise ValueError("TELEGRAM_API_BASE должен быть HTTPS-адресом Worker без пути")

    session = ProxySession(base_url, config["TELEGRAM_PROXY_SECRET"])
    async with Bot(token=config["BOT_TOKEN"], session=session) as bot:
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("У бота установлен webhook. Удалите его перед запуском polling.")
        dp = Dispatcher()
        dp.message.register(greet, F.from_user, ~F.from_user.is_bot)
        await dp.start_polling(bot, allowed_updates=["message"], polling_timeout=30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
