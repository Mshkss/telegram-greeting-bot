"""Uses an isolated temporary PostgreSQL cluster, never the application's database."""
import asyncio
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientConnectionError
from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot, Dispatcher
from aiogram.methods import SendPhoto
from aiogram.types import Update

from app.admin import create_app
from app.db import Database
from app.handlers import build_router
from app.telegram import ProxySession

PROFILE = dict(name='Test', username='example', gender='male', age=25,
               photo_file_id='photo', description='О себе', goals=['friends', 'project'])


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        folder = os.environ.get('TEST_POSTGRES_BIN', '/opt/homebrew/opt/postgresql@15/bin')
        cls.initdb = shutil.which('initdb') or str(Path(folder)/'initdb')
        cls.pg_ctl = shutil.which('pg_ctl') or str(Path(folder)/'pg_ctl')
        if not Path(cls.initdb).exists() or not Path(cls.pg_ctl).exists():
            raise unittest.SkipTest('Set TEST_POSTGRES_BIN to run real PostgreSQL tests')
        cls.temp = tempfile.TemporaryDirectory(prefix='dating-test-', dir='/tmp')
        cls.data = Path(cls.temp.name)/'data'
        subprocess.run([cls.initdb, '-D', str(cls.data), '-A', 'trust', '-U', 'test_admin',
                        '--no-locale', '--encoding=UTF8'], check=True, capture_output=True)
        subprocess.run([cls.pg_ctl, '-D', str(cls.data), '-l', str(Path(cls.temp.name)/'postgres.log'),
                        '-o', f"-c listen_addresses='' -k {cls.temp.name}", '-w', 'start'],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([cls.pg_ctl, '-D', str(cls.data), '-m', 'fast', '-w', 'stop'],
                       check=True, capture_output=True)
        cls.temp.cleanup()

    async def asyncSetUp(self):
        self.settings = SimpleNamespace(db_host=self.temp.name, db_port=5432,
            db_name='postgres', db_user='test_admin', db_password='',
            admin_user='admin', admin_password='correct-long-password')
        self.db = await Database.connect(self.settings)
        await self.db.initialize()
        await self.db.pool.execute('TRUNCATE profiles,drafts,reactions,profile_archives,user_moderation,moderation_events,analytics_users,analytics_sessions,analytics_updates,analytics_activity_days,analytics_events,analytics_attempts CASCADE')
        await self.db.pool.execute('UPDATE app_settings SET registrations_open=true')
        self.session = ProxySession('https://proxy.example', 'test-secret')
        self.session.make_request = AsyncMock(return_value=True)
        self.bot = Bot('123456:TEST_TOKEN', session=self.session)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.db))
        self.seq = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.db.close()

    async def publish(self, uid, **changes):
        draft = await self.db.save_draft(uid, 'preview', {**PROFILE, **changes})
        self.assertTrue(await self.db.publish(uid, draft['version']))

    async def send(self, text=None, callback=None, photo=False, uid=42):
        self.seq += 1
        user = {'id': uid, 'is_bot': False, 'first_name': 'Test', 'username': 'example'}
        message = {'message_id': self.seq, 'date': 0, 'chat': {'id': uid, 'type': 'private'}, 'from': user}
        if callback:
            update = {'callback_query': {'id': str(self.seq), 'from': user, 'chat_instance': 'test',
                                         'message': message, 'data': callback}}
        else:
            if text is not None:
                message['text'] = text
            if photo:
                message['photo'] = [{'file_id': 'photo', 'file_unique_id': 'unique', 'width': 100, 'height': 100}]
            update = {'message': message}
        await self.dp.feed_update(self.bot, Update.model_validate({'update_id': self.seq, **update}))

    async def click_draft(self, value):
        draft = await self.db.draft(42)
        await self.send(callback=f"draft:{draft['version']}:{value}")

    async def test_onboarding_restart_stale_buttons_edit_cancel_and_delete(self):
        await self.send('/start')
        await self.click_draft('agree')
        old = await self.db.draft(42)
        await self.click_draft('female')
        await self.send(callback=f"draft:{old['version']}:male")
        self.assertEqual((await self.db.draft(42))['data']['gender'], 'female')
        await self.send('17')
        self.assertEqual((await self.db.draft(42))['step'], 'age')
        await self.send('23')
        await self.send('not a photo')
        self.assertEqual((await self.db.draft(42))['step'], 'photo')
        await self.send(photo=True)
        await self.send('x'*501)
        self.assertEqual((await self.db.draft(42))['step'], 'description')
        await self.send('Люблю прогулки')
        await self.click_draft('done')
        self.assertEqual((await self.db.draft(42))['step'], 'goals')
        await self.click_draft('goal_friends')
        await self.click_draft('goal_project')
        await self.click_draft('goal_project')
        await self.click_draft('goal_dating')
        # Reopen all database connections, emulating a process restart.
        await self.db.close()
        self.db = await Database.connect(self.settings)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.db))
        await self.send('/start')
        self.assertEqual((await self.db.draft(42))['data']['goals'], ['friends', 'dating'])
        await self.click_draft('done')
        preview = await self.db.draft(42)
        await self.click_draft('publish')
        self.assertEqual((await self.db.profile(42))['age'], 23)
        self.assertIsNone(await self.db.draft(42))
        await self.send(callback=f"draft:{preview['version']}:publish")
        await self.send('/edit')
        await self.send('/cancel')
        self.assertEqual((await self.db.profile(42))['description'], 'Люблю прогулки')
        await self.send('/hide')
        self.assertFalse((await self.db.profile(42))['active'])
        await self.send('/show')
        self.assertTrue((await self.db.profile(42))['active'])
        await self.send('/delete')
        self.assertIsNotNone(await self.db.profile(42))
        await self.send(callback='delete:confirm')
        self.assertIsNone(await self.db.profile(42))

    async def test_publish_transaction_validation_and_double_click(self):
        draft = await self.db.save_draft(1, 'preview', {**PROFILE, 'age': 17})
        with self.assertRaises(ValueError):
            await self.db.publish(1, draft['version'])
        self.assertIsNone(await self.db.profile(1))
        draft = await self.db.save_draft(1, 'preview', PROFILE)
        self.assertFalse(await self.db.publish(1, 'stale'))
        results = await asyncio.gather(self.db.publish(1, draft['version']), self.db.publish(1, draft['version']))
        self.assertEqual(sorted(results), [False, True])

    async def test_search_matching_visibility_and_cascade_delete(self):
        await self.publish(1)
        await self.publish(2, goals=['dating'])
        await self.publish(3, goals=['friends'])
        await self.publish(4, goals=['project'])
        await self.db.visibility(4, False)
        self.assertEqual((await self.db.candidate(1))['user_id'], 3)
        self.assertEqual(await self.db.react(1, 2, True), (False, False))
        self.assertEqual(await self.db.react(1, 4, True), (False, False))
        self.assertEqual(await self.db.react(1, 3, True), (True, False))
        self.assertIsNone(await self.db.candidate(1))
        self.assertEqual(await self.db.react(3, 1, True), (True, True))
        self.assertEqual(await self.db.react(3, 1, True), (False, False))
        self.assertEqual(len(await self.db.matches(1)), 1)
        await self.db.visibility(3, False)
        self.assertEqual(len(await self.db.matches(1)), 0)
        await self.db.delete(3)
        self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM reactions'), 0)

    async def test_concurrent_mutual_likes(self):
        await self.publish(1)
        await self.publish(2)
        results = await asyncio.gather(self.db.react(1, 2, True), self.db.react(2, 1, True))
        self.assertEqual(sum(mutual for _, mutual in results), 1)
        self.assertEqual(len(await self.db.matches(1)), 1)

    async def test_admin_auth_search_escape_and_photo_proxy(self):
        await self.publish(42, name='<script>alert(1)</script>', description='<img src=x onerror=bad()>')
        fake_bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='photo.jpg', file_size=10)))
        async def download(path, destination):
            destination.write(b'\xff\xd8\xfftest')
        fake_bot.download_file = AsyncMock(side_effect=download)
        async with TestClient(TestServer(create_app(self.settings, self.db, fake_bot))) as client:
            for path in ('/', '/profiles/42', '/profiles/42/photo'):
                response = await client.get(path)
                self.assertEqual(response.status, 401)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            fake_bot.get_file.assert_not_called()
            auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:correct-long-password').decode()}
            response = await client.get('/profiles?q=42', headers=auth)
            body = await response.text()
            self.assertIn('&lt;script&gt;', body)
            self.assertNotIn('<script>alert', body)
            response = await client.get('/profiles/42', headers=auth)
            self.assertIn('&lt;img src=x', await response.text())
            response = await client.get('/profiles/42/photo', headers=auth)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'\xff\xd8\xfftest')
            self.assertNotIn('TEST_TOKEN', str(response.headers))
            fake_bot.download_file.side_effect = ClientConnectionError('upstream URL contains TEST_TOKEN')
            response = await client.get('/profiles/42/photo', headers=auth)
            self.assertEqual(response.status, 502)
            self.assertNotIn('TEST_TOKEN', await response.text())
            self.assertEqual((await client.get('/profiles/999', headers=auth)).status, 404)
            self.assertEqual((await client.get('/profiles/99999999999999999999999999', headers=auth)).status, 404)
            response = await client.get('/profiles?q=%27%20OR%201=1--', headers=auth)
            self.assertIn('Анкет пока нет', await response.text())

    async def test_admin_login_throttle(self):
        async with TestClient(TestServer(create_app(self.settings, self.db, self.bot))) as client:
            for _ in range(10):
                self.assertEqual((await client.get('/')).status, 401)
            self.assertEqual((await client.get('/')).status, 429)

    async def test_archive_every_deletion_and_preserve_draft_and_photo(self):
        await self.publish(42, description='Первая версия')
        await self.db.save_draft(42, 'description', {**PROFILE, 'photo_file_id': 'draft-photo'})
        results = await asyncio.gather(self.db.delete(42), self.db.delete(42))
        self.assertEqual(sum(value is not None for value in results), 1)
        total, rows = await self.db.list_archives('42')
        self.assertEqual(total, 1)
        first = rows[0]
        self.assertEqual(first['profile_snapshot']['description'], 'Первая версия')
        self.assertEqual(first['profile_snapshot']['photo_file_id'], 'photo')
        self.assertEqual(first['draft_snapshot']['data']['photo_file_id'], 'draft-photo')
        self.assertIsNone(await self.db.profile(42))
        self.assertIsNone(await self.db.draft(42))
        await self.publish(42, description='Вторая версия')
        second_id = await self.db.delete(42)
        self.assertNotEqual(first['id'], second_id)
        self.assertEqual((await self.db.archive(first['id']))['profile_snapshot']['description'], 'Первая версия')
        self.assertEqual((await self.db.list_archives())[0], 2)
        await self.db.save_draft(42, 'gender', {'name': 'Draft', 'goals': []})
        draft_id = await self.db.delete(42)
        record = await self.db.archive(draft_id)
        self.assertIsNone(record['profile_snapshot'])
        self.assertEqual(record['draft_snapshot']['step'], 'gender')
        self.assertEqual((await self.db.list_archives('Draft'))[0], 1)

    async def test_archive_failure_rolls_back_deletion(self):
        import asyncpg
        await self.publish(42)
        await self.db.pool.execute('ALTER TABLE profile_archives ADD CONSTRAINT test_failure CHECK(false) NOT VALID')
        try:
            with self.assertRaises(asyncpg.CheckViolationError):
                await self.db.delete(42)
            self.assertIsNotNone(await self.db.profile(42))
        finally:
            await self.db.pool.execute('ALTER TABLE profile_archives DROP CONSTRAINT test_failure')

    async def test_moderation_survives_edit_show_delete_and_registration(self):
        await self.publish(1)
        await self.publish(2)
        await self.db.react(1, 2, True)
        await self.db.react(2, 1, True)
        await self.db.moderate(2, True, 'Спам', 'admin')
        self.assertIsNone(await self.db.candidate(2))
        self.assertEqual(await self.db.matches(1), [])
        self.assertEqual(await self.db.matches(2), [])
        self.assertEqual(await self.db.react(2, 1, True), (False, False))
        await self.publish(2, description='Исправил')
        await self.db.visibility(2, True)
        self.assertTrue((await self.db.profile(2))['blocked'])
        self.assertEqual((await self.db.list_profiles(status='blocked'))[0], 1)
        self.assertEqual((await self.db.list_profiles(status='active'))[0], 1)
        await self.db.delete(2)
        await self.db.moderate(2, True, 'Спам', 'admin')
        self.assertEqual(len(await self.db.moderation_history(2)), 1)
        await self.publish(2)
        self.assertIsNone(await self.db.candidate(1))
        await self.db.moderate(2, False, 'Проверено', 'admin')
        self.assertEqual((await self.db.candidate(1))['user_id'], 2)
        self.assertEqual(len(await self.db.moderation_history(2)), 2)

    async def test_existing_database_migration_is_idempotent_and_concurrent(self):
        await self.publish(42)
        # Simulate the previous production schema, retaining the actual profile.
        await self.db.pool.execute('DROP TABLE profile_archives,user_moderation,moderation_events,schema_migrations,analytics_users,analytics_sessions,analytics_updates,analytics_activity_days,analytics_events,analytics_attempts,app_settings; ALTER TABLE drafts DROP COLUMN attempt_id')
        await asyncio.gather(self.db.initialize(), self.db.initialize())
        self.assertEqual((await self.db.profile(42))['name'], PROFILE['name'])
        self.assertFalse((await self.db.profile(42))['blocked'])
        self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM schema_migrations'), 3)
        self.assertIsNotNone(await self.db.delete(42))

    async def test_admin_moderation_csrf_archive_access_and_unblock(self):
        import re
        await self.publish(42)
        await self.publish(43)
        fake_bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='photo.jpg', file_size=10)))
        async def download(path, destination):
            destination.write(b'\xff\xd8\xfftest')
        fake_bot.download_file = AsyncMock(side_effect=download)
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:correct-long-password').decode()}
        async with TestClient(TestServer(create_app(self.settings, self.db, fake_bot))) as client:
            response = await client.get('/profiles/42', headers=auth)
            token = re.search(r'name="csrf" value="([^"]+)"', await response.text()).group(1)
            form = {'action': 'block', 'reason': '<script>спам</script>', 'csrf': token}
            self.assertEqual((await client.post('/profiles/42/moderation', data=form)).status, 401)
            self.assertEqual((await client.get('/profiles/42/moderation', headers=auth)).status, 405)
            self.assertEqual((await client.post('/profiles/42/moderation', data={**form, 'csrf': ''}, headers=auth)).status, 403)
            self.assertEqual((await client.post('/profiles/43/moderation', data=form, headers=auth)).status, 403)
            self.assertEqual((await client.post('/profiles/42/moderation', data={**form, 'reason': ''}, headers=auth)).status, 400)
            response = await client.post('/profiles/42/moderation', data=form, headers=auth, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertTrue((await self.db.profile(42))['blocked'])
            detail = await (await client.get('/profiles/42', headers=auth)).text()
            self.assertIn('&lt;script&gt;спам&lt;/script&gt;', detail)
            self.assertNotIn('<script>', detail)
            aid = await self.db.delete(42)
            for path in ('/archive', f'/archive/{aid}', f'/archive/{aid}/photo'):
                self.assertEqual((await client.get(path)).status, 401)
            response = await client.get('/archive?q=42', headers=auth)
            self.assertIn('Найдено записей: 1', await response.text())
            response = await client.get(f'/archive/{aid}/photo', headers=auth)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'\xff\xd8\xfftest')
            response = await client.get(f'/archive/{aid}', headers=auth)
            body = await response.text()
            self.assertIn('Разблокировать пользователя', body)
            token = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
            response = await client.post(f'/archive/{aid}/moderation', headers=auth,
                data={'action': 'unblock', 'reason': 'Проверено', 'csrf': token}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertFalse((await self.db.moderation(42))['blocked'])
            self.assertIsNone(await self.db.profile(42))
            self.assertEqual((await client.get('/archive/999999999999999999999999', headers=auth)).status, 404)
            await self.db.save_draft(44, 'age', {'name': 'Draft', 'goals': []})
            draft_id = await self.db.delete(44)
            response = await client.get(f'/archive/{draft_id}', headers=auth)
            self.assertEqual(response.status, 200)
            self.assertIn('Фото не загружено', await response.text())
            self.assertEqual((await client.get(f'/archive/{draft_id}/photo', headers=auth)).status, 404)

    async def test_analytics_sessions_boundary_sources_and_deduplication(self):
        from datetime import datetime, timedelta, timezone
        t = datetime(2026,1,1,12,tzinfo=timezone.utc)
        a = await self.db.begin_update(1001, 201, 'start', 'poster', t)
        self.assertIsNone(await self.db.begin_update(1001,201,'start','other',t))
        b = await self.db.begin_update(1002,201,'message',at=t+timedelta(minutes=29))
        c = await self.db.begin_update(1003,201,'start','chat',t+timedelta(minutes=59))
        self.assertEqual(a['session_id'],b['session_id'])
        self.assertNotEqual(a['session_id'],c['session_id'])
        user = await self.db.pool.fetchrow('SELECT * FROM analytics_users WHERE user_id=201')
        self.assertEqual(user['start_count'],2)
        self.assertEqual(user['first_source'],'poster')
        self.assertEqual(user['last_source'],'chat')
        self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM analytics_sessions'),2)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='start_first'"),1)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='start_repeat'"),1)

    async def test_analytics_cohorts_mature_days_and_moscow_midnight(self):
        from datetime import datetime, timedelta, timezone, date
        from app.reports import dashboard_report
        t = datetime(2026,1,1,12,tzinfo=timezone.utc)
        await self.db.pool.execute('INSERT INTO analytics_users(user_id,known_before_tracking) VALUES(203,true)')
        for update_id,uid,day in [(1,201,0),(2,202,0),(3,203,0),(4,201,1),(5,201,7),(6,204,8)]:
            await self.db.begin_update(update_id,uid,'start','campaign',t+timedelta(days=day))
            await self.db.finish_update(update_id,20)
        r = await dashboard_report(self.db,date(2026,1,1),date(2026,1,9),now=t+timedelta(days=9))
        self.assertEqual(r['users']['new'],3)
        self.assertEqual(r['activity']['dau'],1)
        self.assertEqual(r['activity']['wau'],2)
        self.assertEqual(r['activity']['mau'],4)
        cohort = next(row for row in r['retention'] if row['day']==date(2026,1,1))
        self.assertEqual((cohort['users'],cohort['d1'],cohort['d7']),(2,1,1))
        recent = next(row for row in r['retention'] if row['day']==date(2026,1,9))
        self.assertIsNone(recent['d1'])
        self.assertIsNone(recent['d7'])
        await self.db.begin_update(7,205,'message',at=datetime(2026,1,1,21,1,tzinfo=timezone.utc))
        self.assertEqual(await self.db.pool.fetchval('SELECT day FROM analytics_activity_days WHERE user_id=205'), date(2026,1,2))

    async def test_activity_middleware_dedup_and_technical_failure_redaction(self):
        from app.analytics import ActivityMiddleware, TelegramMetricsMiddleware
        from aiogram.exceptions import TelegramForbiddenError
        self.dp.update.outer_middleware(ActivityMiddleware(self.db))
        self.session.middleware(TelegramMetricsMiddleware(self.db))
        await self.send('/start poster')
        self.seq = 0
        await self.send('/start poster')  # same Telegram update_id
        self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM analytics_updates'),1)
        self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM analytics_attempts'),1)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='telegram_call'"),1)
        self.session.make_request.side_effect = TelegramForbiddenError(method=None, message='SENSITIVE_TOKEN_AND_TEXT')
        with self.assertRaises(TelegramForbiddenError):
            await self.send('hello')
        result = await self.db.pool.fetchrow('SELECT * FROM analytics_updates WHERE update_id=2')
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['error_type'],'TelegramForbiddenError')
        events = str(await self.db.pool.fetch('SELECT * FROM analytics_events'))
        self.assertNotIn('SENSITIVE_TOKEN_AND_TEXT',events)
        self.assertNotIn('hello',events)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='telegram_call' AND properties->>'ok'='false'"),1)

    async def test_analytics_funnel_real_flow_and_business_events_survive_delete(self):
        from app.analytics import ActivityMiddleware
        self.dp.update.outer_middleware(ActivityMiddleware(self.db))
        await self.send('/start test')
        await self.click_draft('agree')
        await self.click_draft('male')
        await self.send('25')
        await self.send(photo=True)
        await self.send('О себе')
        await self.click_draft('goal_friends')
        await self.click_draft('goal_project')
        await self.click_draft('goal_project')
        await self.click_draft('done')
        await self.click_draft('publish')
        attempt = await self.db.pool.fetchrow('SELECT * FROM analytics_attempts')
        self.assertEqual(attempt['status'],'published')
        self.assertEqual(list(attempt['completed_steps']),['consent','gender','age','photo','description','goals','preview'])
        self.assertFalse(attempt['is_edit'])
        await self.send('/edit')
        self.assertTrue(await self.db.pool.fetchval('SELECT is_edit FROM analytics_attempts WHERE status=\'in_progress\''))
        await self.send('/cancel')
        await self.send('/hide')
        await self.send('/hide')
        await self.send('/show')
        await self.publish(43,goals=['friends'])
        await self.db.react(42,43,True)
        await self.db.react(42,43,True)
        await self.db.react(43,42,True)
        await self.db.delete(42)
        await self.db.delete(42)
        for event_name,total in [('profile_hidden',1),('profile_liked',2),('match_created',1),('profile_deleted',1)]:
            self.assertEqual(await self.db.pool.fetchval('SELECT count(*) FROM analytics_events WHERE name=$1',event_name),total)
        self.assertEqual(await self.db.pool.fetchval('SELECT status FROM analytics_attempts WHERE id=$1',attempt['id']),'published')

    async def test_analytics_view_only_after_delivery_and_empty_browse(self):
        from app.handlers import browse
        from aiogram.exceptions import TelegramForbiddenError
        await self.publish(42)
        await self.publish(43)
        message = SimpleNamespace(answer_photo=AsyncMock(side_effect=TelegramForbiddenError(method=None,message='blocked')),
                                  answer=AsyncMock())
        with self.assertRaises(TelegramForbiddenError):
            await browse(message,self.db,42)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='profile_viewed'"),0)
        message.answer_photo.side_effect = None
        await browse(message,self.db,42)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='profile_viewed'"),1)
        await self.db.react(42,43,False)
        await browse(message,self.db,42)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='browse_empty'"),1)

    async def test_dashboard_settings_csv_and_paused_registration(self):
        import re
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:correct-long-password').decode()}
        async with TestClient(TestServer(create_app(self.settings,self.db,self.bot))) as client:
            for path in ('/dashboard','/settings','/analytics/daily.csv'):
                self.assertEqual((await client.get(path)).status,401)
            response = await client.get('/',headers=auth)
            self.assertEqual(response.status,200)
            body = await response.text()
            self.assertIn('Начинаем собирать данные',body)
            self.assertIn('D7 retention',body)
            self.assertEqual((await client.get('/?from=wrong',headers=auth)).status,400)
            response = await client.get('/analytics/daily.csv',headers=auth)
            self.assertEqual(response.status,200)
            self.assertIn('date_moscow',await response.text())
            response = await client.get('/settings',headers=auth)
            body = await response.text()
            token = re.search(r'name="csrf" value="([^"]+)"',body).group(1)
            self.assertEqual((await client.post('/settings',headers=auth,data={})).status,403)
            response = await client.post('/settings',headers=auth,data={'csrf':token},allow_redirects=False)
            self.assertEqual(response.status,303)
            self.assertFalse((await self.db.settings())['registrations_open'])
            await self.send('/start')
            self.assertIsNone(await self.db.draft(42))
            await self.publish(42)
            await self.send('/edit')
            self.assertIsNotNone(await self.db.draft(42))
            self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM analytics_events WHERE name='settings_changed'"),1)
