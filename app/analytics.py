"""Analytics contain action codes and IDs, never message text, photos or exception messages."""
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware

log = logging.getLogger(__name__)
CONTEXT = ContextVar('analytics_context', default=None)
STEPS = ('consent', 'gender', 'age', 'photo', 'description', 'goals', 'preview')
SCHEMA = """
CREATE TABLE analytics_users (
    user_id BIGINT PRIMARY KEY,
    known_before_tracking BOOLEAN NOT NULL DEFAULT false,
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    first_start_at TIMESTAMPTZ,
    first_source TEXT,
    last_source TEXT,
    start_count INTEGER NOT NULL DEFAULT 0,
    current_session BIGINT,
    first_published_at TIMESTAMPTZ
);
INSERT INTO analytics_users(user_id,known_before_tracking)
SELECT DISTINCT user_id,true FROM (
 SELECT user_id FROM profiles UNION SELECT user_id FROM drafts UNION SELECT user_id FROM profile_archives
) existing;
CREATE TABLE analytics_sessions (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL, last_activity TIMESTAMPTZ NOT NULL,
    actions INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX analytics_sessions_time_idx ON analytics_sessions(started_at);
CREATE TABLE analytics_updates (
    update_id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL, session_id BIGINT,
    received_at TIMESTAMPTZ NOT NULL, action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing', duration_ms DOUBLE PRECISION, error_type TEXT
);
CREATE INDEX analytics_updates_time_idx ON analytics_updates(received_at);
CREATE TABLE analytics_activity_days (
    user_id BIGINT NOT NULL, day DATE NOT NULL, PRIMARY KEY(user_id,day)
);
CREATE INDEX analytics_activity_day_idx ON analytics_activity_days(day);
CREATE TABLE analytics_events (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, session_id BIGINT, update_id BIGINT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(), name TEXT NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}', dedupe_key TEXT UNIQUE
);
CREATE INDEX analytics_events_time_idx ON analytics_events(occurred_at,name);
CREATE INDEX analytics_events_user_idx ON analytics_events(user_id,occurred_at);
CREATE TABLE analytics_attempts (
    id UUID PRIMARY KEY, user_id BIGINT NOT NULL, is_edit BOOLEAN NOT NULL,
    legacy BOOLEAN NOT NULL DEFAULT false, started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity TIMESTAMPTZ NOT NULL DEFAULT now(), last_step TEXT NOT NULL,
    completed_steps TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'in_progress', published_at TIMESTAMPTZ
);
CREATE INDEX analytics_attempts_time_idx ON analytics_attempts(started_at);
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS attempt_id UUID;
CREATE TABLE app_settings (
    id BOOLEAN PRIMARY KEY DEFAULT true CHECK(id),
    registrations_open BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by TEXT NOT NULL DEFAULT 'system'
);
INSERT INTO app_settings(id) VALUES(true);
"""


def utcnow():
    return datetime.now(timezone.utc)


def start_source(text):
    parts = (text or '').split(maxsplit=1)
    if not parts or not re.fullmatch(r'/start(?:@[A-Za-z0-9_]+)?', parts[0]):
        return None
    if len(parts) == 1:
        return 'direct'
    return parts[1] if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', parts[1]) else 'invalid'


def action_code(event):
    if getattr(event, 'data', None):
        parts = event.data.split(':')
        if parts[0] == 'menu' and len(parts) == 2 and parts[1] in ('profile','edit','browse','matches','hide','show','delete'):
            return 'menu_' + parts[1]
        if parts[0] == 'draft':
            return 'draft_button'
        if parts[0] == 'react' and parts[-1] in ('like','skip'):
            return parts[-1]
        return 'other_button'
    text = getattr(event, 'text', None) or ''
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
    if command in ('/start','/profile','/edit','/browse','/matches','/hide','/show','/delete','/cancel'):
        return command[1:]
    return 'photo' if getattr(event, 'photo', None) else 'message'


async def event(conn, name, user_id=None, properties=None, dedupe_key=None, at=None):
    context = CONTEXT.get() or {}
    await conn.execute("""INSERT INTO analytics_events
        (user_id,session_id,update_id,occurred_at,name,properties,dedupe_key)
        VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(dedupe_key) DO NOTHING""",
        user_id, context.get('session_id'), context.get('update_id'), at or utcnow(), name,
        json.dumps(properties or {}), dedupe_key)


class AnalyticsStore:
    async def track(self, name, user_id=None, properties=None, dedupe_key=None):
        async with self.pool.acquire() as conn:
            await event(conn, name, user_id, properties, dedupe_key)

    async def begin_update(self, update_id, user_id, action, source=None, at=None):
        at = at or utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock($1)', -user_id)
                inserted = await conn.fetchval("""INSERT INTO analytics_updates(update_id,user_id,received_at,action)
                    VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING true""", update_id, user_id, at, action)
                if not inserted:
                    return None
                await conn.execute('INSERT INTO analytics_users(user_id) VALUES($1) ON CONFLICT DO NOTHING', user_id)
                user = await conn.fetchrow('SELECT * FROM analytics_users WHERE user_id=$1 FOR UPDATE', user_id)
                session = user['current_session']
                if not user['last_seen'] or at >= user['last_seen'] + timedelta(minutes=30):
                    session = await conn.fetchval("""INSERT INTO analytics_sessions(user_id,started_at,last_activity)
                        VALUES($1,$2,$2) RETURNING id""", user_id, at)
                else:
                    await conn.execute('UPDATE analytics_sessions SET last_activity=greatest(last_activity,$2),actions=actions+1 WHERE id=$1', session, at)
                await conn.execute("""UPDATE analytics_users SET first_seen=coalesce(first_seen,$2),
                    last_seen=greatest(last_seen,$2),current_session=$3 WHERE user_id=$1""", user_id, at, session)
                await conn.execute('UPDATE analytics_updates SET session_id=$2 WHERE update_id=$1', update_id, session)
                await conn.execute("""INSERT INTO analytics_activity_days(user_id,day)
                    VALUES($1,($2::timestamptz AT TIME ZONE 'Europe/Moscow')::date) ON CONFLICT DO NOTHING""", user_id, at)
                context = {'user_id': user_id, 'update_id': update_id, 'session_id': session}
                token = CONTEXT.set(context)
                try:
                    await event(conn, 'user_action', user_id, {'action': action}, f'update:{update_id}', at)
                    if source is not None:
                        name = 'start_first' if user['start_count'] == 0 else 'start_repeat'
                        await conn.execute("""UPDATE analytics_users SET first_start_at=coalesce(first_start_at,$2),
                            first_source=coalesce(first_source,$3),last_source=$3,start_count=start_count+1 WHERE user_id=$1""", user_id, at, source)
                        await event(conn, name, user_id, {'source': source, 'legacy': user['known_before_tracking']}, f'start:{update_id}', at)
                finally:
                    CONTEXT.reset(token)
                return context

    async def finish_update(self, update_id, duration_ms, error_type=None):
        await self.pool.execute("""UPDATE analytics_updates SET status=$2,duration_ms=$3,error_type=$4
            WHERE update_id=$1""", update_id, 'error' if error_type else 'ok', duration_ms, error_type)

    async def settings(self):
        return await self.pool.fetchrow('SELECT * FROM app_settings WHERE id=true')

    async def update_settings(self, registrations_open, actor):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchrow('SELECT * FROM app_settings WHERE id=true FOR UPDATE')
                if previous['registrations_open'] != registrations_open:
                    await conn.execute('UPDATE app_settings SET registrations_open=$1,updated_at=now(),updated_by=$2 WHERE id=true', registrations_open, actor)
                    await event(conn, 'settings_changed', properties={'registrations_open': registrations_open, 'actor': actor})


class ActivityMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, update, data):
        incoming = update.message or update.callback_query
        user = incoming.from_user if incoming else None
        message = incoming.message if update.callback_query else incoming
        if not user or user.is_bot or not message or message.chat.type != 'private':
            return await handler(update, data)
        started = time.perf_counter()
        context = await self.db.begin_update(update.update_id, user.id, action_code(incoming),
            start_source(incoming.text) if update.message else None)
        if context is None:
            return None  # Telegram redelivery: never repeat a side effect.
        token = CONTEXT.set(context)
        error = None
        try:
            return await handler(update, data)
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            try:
                await self.db.finish_update(update.update_id, (time.perf_counter()-started)*1000, error)
            except Exception:
                log.error('analytics_finish_failed')  # No exception message: it could contain credentials.
            CONTEXT.reset(token)


class TelegramMetricsMiddleware:
    def __init__(self, db, channel='bot'):
        self.db, self.channel = db, channel

    async def __call__(self, make_request, bot, method):
        started, error = time.perf_counter(), None
        try:
            return await make_request(bot, method)
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            name = method.__api_method__
            # Successful polling is waiting, not outgoing delivery latency.
            if name != 'getUpdates' or error:
                context = CONTEXT.get() or {}
                props = {'method': name, 'duration_ms': (time.perf_counter()-started)*1000,
                         'ok': error is None, 'error_type': error, 'channel': self.channel}
                target = getattr(method, 'chat_id', None)
                if isinstance(target, int):
                    props['recipient_id'] = target
                try:
                    await self.db.track('telegram_call', context.get('user_id'), props)
                except Exception:
                    log.error('analytics_telegram_failed')
