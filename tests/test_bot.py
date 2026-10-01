import unittest

from app.domain import caption, validate_profile
from app.telegram import ProxySession

PROFILE = dict(name='Test', gender='male', age=25, photo_file_id='photo',
               description='О себе', goals=['friends', 'project'])


class UnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_header_survives_session_recreation(self):
        session = ProxySession('https://proxy.example', 'test-secret')
        for _ in range(2):
            client = await session.create_session()
            self.assertEqual(client.headers['Authorization'], 'Bearer test-secret')
            self.assertEqual(session.api.api_url('123:test', 'getMe'),
                             'https://proxy.example/bot123:test/getMe')
            self.assertEqual(session.api.file_url('123:test', 'photos/a.jpg'),
                             'https://proxy.example/file/bot123:test/photos/a.jpg')
            await session.close()

    def test_profile_validation(self):
        validate_profile(PROFILE)
        for changes in ({'age': 17}, {'age': True}, {'age': 101}, {'gender': 'unknown'},
                        {'photo_file_id': ''}, {'description': 'x'*501},
                        {'goals': []}, {'goals': ['unknown']}, {'goals': ['friends', 'friends']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_profile({**PROFILE, **changes})
        self.assertIn('людей для проекта', caption(PROFILE))
