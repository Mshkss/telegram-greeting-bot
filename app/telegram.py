from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer


class ProxySession(AiohttpSession):
    def __init__(self, base_url: str, secret: str):
        super().__init__(api=TelegramAPIServer.from_base(base_url), timeout=60)
        self.proxy_secret = secret

    async def create_session(self):
        session = await super().create_session()
        session.headers['Authorization'] = f'Bearer {self.proxy_secret}'
        return session


def create_bot(settings):
    return Bot(settings.bot_token, session=ProxySession(settings.api_base, settings.proxy_secret))
