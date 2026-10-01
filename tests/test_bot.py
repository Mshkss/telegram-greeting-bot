import unittest
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, F
from aiogram.methods import SendMessage
from aiogram.types import Update

from bot import ProxySession, greet


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_header_survives_session_recreation(self):
        session = ProxySession('https://proxy.example', 'test-secret')
        for _ in range(2):
            client = await session.create_session()
            self.assertEqual(client.headers['Authorization'], 'Bearer test-secret')
            self.assertEqual(session.api.api_url('123:test', 'getMe'),
                             'https://proxy.example/bot123:test/getMe')
            await session.close()

    async def test_messages_and_ignored_updates(self):
        session = ProxySession('https://proxy.example', 'test-secret')
        session.make_request = AsyncMock(return_value=True)
        bot = Bot('123456:TEST_TOKEN', session=session)
        dp = Dispatcher()
        dp.message.register(greet, F.from_user, ~F.from_user.is_bot)
        base = {'message_id': 1, 'date': 0,
                'chat': {'id': 42, 'type': 'private'},
                'from': {'id': 42, 'is_bot': False, 'first_name': 'Test'}}
        for content in ({'text': '/start'}, {'text': 'hi'},
                        {'photo': [{'file_id': 'a', 'file_unique_id': 'b',
                                    'width': 1, 'height': 1}]}):
            session.make_request.reset_mock()
            await dp.feed_update(bot, Update.model_validate(
                {'update_id': 1, 'message': {**base, **content}}))
            method = session.make_request.call_args.args[1]
            self.assertIsInstance(method, SendMessage)
            self.assertEqual(method.chat_id, 42)
            self.assertEqual(method.text, 'Привет! 👋')
        for update in (
            {'edited_message': {**base, 'text': 'edit'}},
            {'message': {**base, 'text': 'hi', 'from':
                         {'id': 43, 'is_bot': True, 'first_name': 'Bot'}}},
        ):
            session.make_request.reset_mock()
            await dp.feed_update(bot, Update.model_validate({'update_id': 2, **update}))
            session.make_request.assert_not_called()
        await bot.session.close()


if __name__ == '__main__':
    unittest.main()
