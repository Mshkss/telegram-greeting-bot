import json
import secrets
from uuid import uuid4, UUID

import asyncpg

from app.domain import validate_profile
from app.analytics import AnalyticsStore, SCHEMA as ANALYTICS_SCHEMA, event

SCHEMA = '''
CREATE TABLE IF NOT EXISTS profiles (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female')),
    age SMALLINT NOT NULL CHECK (age BETWEEN 18 AND 100),
    photo_file_id TEXT NOT NULL,
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 500),
    goals TEXT[] NOT NULL CHECK (cardinality(goals) BETWEEN 1 AND 5 AND
        goals <@ ARRAY['dating','friends','company','project','interests']::text[]),
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS drafts (
    user_id BIGINT PRIMARY KEY,
    step TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reactions (
    actor BIGINT REFERENCES profiles(user_id) ON DELETE CASCADE,
    target BIGINT REFERENCES profiles(user_id) ON DELETE CASCADE,
    liked BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(actor,target),
    CHECK(actor<>target)
);
CREATE INDEX IF NOT EXISTS profiles_goals_idx ON profiles USING gin(goals);
'''


MIGRATIONS = [(2, """
CREATE TABLE profile_archives (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    profile_snapshot JSONB,
    draft_snapshot JSONB,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (profile_snapshot IS NOT NULL OR draft_snapshot IS NOT NULL)
);
CREATE INDEX profile_archives_user_idx ON profile_archives(user_id, deleted_at DESC);
CREATE TABLE user_moderation (
    user_id BIGINT PRIMARY KEY,
    blocked BOOLEAN NOT NULL DEFAULT false,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE moderation_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('block','unblock')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    moderator TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX moderation_events_user_idx ON moderation_events(user_id, id DESC);
""")]

MIGRATIONS.append((3, ANALYTICS_SCHEMA))
MIGRATIONS.append((4, """
ALTER TABLE analytics_users ADD COLUMN display_name TEXT;
ALTER TABLE analytics_users ADD COLUMN username TEXT;
UPDATE analytics_users u SET display_name=old.name,username=old.username
FROM (
    SELECT a.user_id,coalesce(p.name,d.data->>'name',
        h.profile_snapshot->>'name',h.draft_snapshot->'data'->>'name') AS name,
        coalesce(p.username,d.data->>'username',
        h.profile_snapshot->>'username',h.draft_snapshot->'data'->>'username') AS username
    FROM analytics_users a LEFT JOIN profiles p USING(user_id) LEFT JOIN drafts d USING(user_id)
    LEFT JOIN LATERAL (SELECT profile_snapshot,draft_snapshot FROM profile_archives
        WHERE user_id=a.user_id ORDER BY deleted_at DESC,id DESC LIMIT 1) h ON true
) old WHERE old.user_id=u.user_id;
CREATE INDEX analytics_users_last_seen_idx ON analytics_users(last_seen DESC NULLS LAST,user_id DESC);
"""))

MIGRATIONS.append((5, """
CREATE INDEX analytics_attempts_user_idx ON analytics_attempts(user_id,started_at DESC,id DESC);
CREATE INDEX analytics_sessions_user_idx ON analytics_sessions(user_id,started_at DESC,id DESC);
CREATE INDEX analytics_updates_user_idx ON analytics_updates(user_id,received_at DESC);
CREATE INDEX analytics_events_match_target_idx ON analytics_events((properties->>'target_id'),occurred_at DESC)
    WHERE name='match_created';
CREATE INDEX analytics_events_failed_recipient_idx ON analytics_events((properties->>'recipient_id'),occurred_at DESC)
    WHERE name='telegram_call' AND properties->>'ok'='false';
"""))

PROFILE_SELECT = """SELECT p.*, coalesce(m.blocked,false) AS blocked,
    coalesce(m.reason,'') AS moderation_reason FROM profiles p
    LEFT JOIN user_moderation m ON m.user_id=p.user_id """


class Database(AnalyticsStore):
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, settings):
        pool = await asyncpg.create_pool(host=settings.db_host, port=settings.db_port,
            database=settings.db_name, user=settings.db_user, password=settings.db_password,
            min_size=1, max_size=5, command_timeout=15)
        return cls(pool)

    async def initialize(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Bot and admin can start concurrently.
                await conn.execute('SELECT pg_advisory_xact_lock(72149017)')
                await conn.execute(SCHEMA)
                await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                await conn.execute('INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING')
                for version, sql in MIGRATIONS:
                    if not await conn.fetchval('SELECT 1 FROM schema_migrations WHERE version=$1', version):
                        await conn.execute(sql)
                        await conn.execute('INSERT INTO schema_migrations(version) VALUES($1)', version)

    async def close(self):
        await self.pool.close()

    async def profile(self, user_id):
        return await self.pool.fetchrow(PROFILE_SELECT + 'WHERE p.user_id=$1', user_id)

    async def draft(self, user_id):
        row = await self.pool.fetchrow('SELECT * FROM drafts WHERE user_id=$1', user_id)
        if row:
            return {**dict(row), 'data': json.loads(row['data'])}

    async def save_draft(self, user_id, step, data):
        version = secrets.token_hex(4)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock($1)', user_id)
                old = await conn.fetchrow('SELECT * FROM drafts WHERE user_id=$1 FOR UPDATE', user_id)
                attempt_id = old['attempt_id'] if old else None
                if step == 'consent' or attempt_id is None:
                    if attempt_id:
                        await conn.execute("UPDATE analytics_attempts SET status='restarted',last_activity=now() WHERE id=$1", attempt_id)
                        await event(conn, 'profile_fill_restarted', user_id, {'attempt_id': str(attempt_id)})
                    is_edit = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM profiles WHERE user_id=$1)
                        OR EXISTS(SELECT 1 FROM profile_archives WHERE user_id=$1 AND profile_snapshot IS NOT NULL)""", user_id)
                    attempt_id = uuid4()
                    legacy = bool(old and old['attempt_id'] is None and step != 'consent')
                    await conn.execute("""INSERT INTO analytics_attempts(id,user_id,is_edit,legacy,last_step)
                        VALUES($1,$2,$3,$4,$5)""", attempt_id, user_id, is_edit, legacy, step)
                    await event(conn, 'profile_fill_started', user_id,
                        {'attempt_id': str(attempt_id), 'is_edit': is_edit, 'legacy': legacy}, f'attempt:{attempt_id}')
                if old and old['step'] != step and step != 'consent':
                    await conn.execute("""UPDATE analytics_attempts SET completed_steps=array_append(completed_steps,$2)
                        WHERE id=$1 AND NOT ($2=ANY(completed_steps))""", attempt_id, old['step'])
                    await event(conn, 'profile_step_completed', user_id,
                        {'attempt_id': str(attempt_id), 'step': old['step']}, f'step:{attempt_id}:{old["step"]}')
                await conn.execute('UPDATE analytics_attempts SET last_step=$2,last_activity=now() WHERE id=$1', attempt_id, step)
                await conn.execute("""INSERT INTO drafts(user_id,step,data,version,attempt_id) VALUES($1,$2,$3,$4,$5)
                    ON CONFLICT(user_id) DO UPDATE SET step=$2,data=$3,version=$4,attempt_id=$5,updated_at=now()""",
                    user_id, step, json.dumps(data), version, attempt_id)
        return {'step': step, 'data': data, 'version': version, 'attempt_id': attempt_id}

    async def cancel(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow('DELETE FROM drafts WHERE user_id=$1 RETURNING attempt_id', user_id)
                if row:
                    if row['attempt_id']:
                        await conn.execute("UPDATE analytics_attempts SET status='cancelled',last_activity=now() WHERE id=$1", row['attempt_id'])
                    await event(conn, 'profile_fill_cancelled', user_id)

    async def publish(self, user_id, version):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock($1)', user_id)
                row = await conn.fetchrow('SELECT * FROM drafts WHERE user_id=$1 FOR UPDATE', user_id)
                if not row or row['version'] != version or row['step'] != 'preview':
                    return False
                data = json.loads(row['data'])
                validate_profile(data)
                await conn.execute('''INSERT INTO profiles
                    (user_id,username,name,gender,age,photo_file_id,description,goals)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT(user_id) DO UPDATE SET username=$2,name=$3,gender=$4,age=$5,
                    photo_file_id=$6,description=$7,goals=$8,active=true,updated_at=now()''',
                    user_id, data.get('username'), data['name'], data['gender'], data['age'],
                    data['photo_file_id'], data['description'], data['goals'])
                attempt_id = row['attempt_id']
                if attempt_id:
                    await conn.execute("""UPDATE analytics_attempts SET status='published',published_at=now(),last_activity=now(),
                        completed_steps=array_append(completed_steps,'preview') WHERE id=$1""", attempt_id)
                await conn.execute("""INSERT INTO analytics_users(user_id,first_published_at) VALUES($1,now())
                    ON CONFLICT(user_id) DO UPDATE SET first_published_at=coalesce(analytics_users.first_published_at,now())""", user_id)
                await event(conn, 'profile_published', user_id, {'attempt_id': str(attempt_id) if attempt_id else None}, f'publish:{user_id}:{version}')
                await conn.execute('DELETE FROM drafts WHERE user_id=$1', user_id)
                return True

    async def visibility(self, user_id, active):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                changed = await conn.fetchval('UPDATE profiles SET active=$2,updated_at=now() WHERE user_id=$1 AND active IS DISTINCT FROM $2 RETURNING true', user_id, active)
                if changed:
                    await event(conn, 'profile_shown' if active else 'profile_hidden', user_id)
                return bool(changed)

    async def delete(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock($1)', user_id)
                profile = await conn.fetchval('SELECT to_jsonb(p) FROM profiles p WHERE user_id=$1 FOR UPDATE', user_id)
                draft = await conn.fetchval('SELECT to_jsonb(d) FROM drafts d WHERE user_id=$1 FOR UPDATE', user_id)
                archive_id = None
                if profile is not None or draft is not None:
                    archive_id = await conn.fetchval("""INSERT INTO profile_archives
                        (user_id,profile_snapshot,draft_snapshot) VALUES($1,$2,$3) RETURNING id""",
                        user_id, profile, draft)
                await conn.execute('DELETE FROM drafts WHERE user_id=$1', user_id)
                await conn.execute('DELETE FROM profiles WHERE user_id=$1', user_id)
                if archive_id:
                    if draft:
                        attempt_id = json.loads(draft).get('attempt_id')
                        if attempt_id:
                            await conn.execute("UPDATE analytics_attempts SET status='deleted',last_activity=now() WHERE id=$1::uuid", UUID(attempt_id))
                    await event(conn, 'profile_deleted' if profile else 'draft_deleted', user_id, {'archive_id': archive_id}, f'delete:{archive_id}')
                return archive_id

    async def list_profiles(self, query='', page=1, status='all'):
        states = {'all': 'true', 'active': 'p.active AND NOT coalesce(m.blocked,false)',
                  'hidden': 'NOT p.active AND NOT coalesce(m.blocked,false)',
                  'blocked': 'coalesce(m.blocked,false)'}
        where = """WHERE ($1='' OR p.name ILIKE '%'||$1||'%' OR p.username ILIKE '%'||$1||'%'
            OR p.user_id::text=$1) AND """ + states.get(status, 'true')
        source = ' FROM profiles p LEFT JOIN user_moderation m ON m.user_id=p.user_id '
        total = await self.pool.fetchval('SELECT count(*)'+source+where, query)
        rows = await self.pool.fetch(PROFILE_SELECT+where+
            ' ORDER BY p.created_at DESC,p.user_id LIMIT 30 OFFSET $2', query, (page-1)*30)
        return total, rows

    async def list_archives(self, query='', page=1):
        where = """WHERE ($1='' OR user_id::text=$1
            OR coalesce(profile_snapshot->>'name',draft_snapshot->'data'->>'name','') ILIKE '%'||$1||'%'
            OR coalesce(profile_snapshot->>'username',draft_snapshot->'data'->>'username','') ILIKE '%'||$1||'%')"""
        total = await self.pool.fetchval('SELECT count(*) FROM profile_archives '+where, query)
        rows = await self.pool.fetch('SELECT * FROM profile_archives '+where+
                                    ' ORDER BY id DESC LIMIT 30 OFFSET $2', query, (page-1)*30)
        return total, [self._archive(row) for row in rows]

    @staticmethod
    def _archive(row):
        return {**dict(row), 'profile_snapshot': json.loads(row['profile_snapshot']) if row['profile_snapshot'] else None,
                'draft_snapshot': json.loads(row['draft_snapshot']) if row['draft_snapshot'] else None}

    async def archive(self, archive_id):
        row = await self.pool.fetchrow('SELECT * FROM profile_archives WHERE id=$1', archive_id)
        return self._archive(row) if row else None

    async def moderation(self, user_id):
        row = await self.pool.fetchrow('SELECT * FROM user_moderation WHERE user_id=$1', user_id)
        return dict(row) if row else {'blocked': False, 'reason': ''}

    async def moderate(self, user_id, blocked, reason, moderator):
        reason = reason.strip()
        if not 1 <= len(reason) <= 500:
            raise ValueError('Укажите причину: от 1 до 500 символов.')
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock($1)', user_id)
                exists = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM profiles WHERE user_id=$1)
                    OR EXISTS(SELECT 1 FROM profile_archives WHERE user_id=$1)""", user_id)
                if not exists:
                    raise LookupError('Участник не найден')
                changed = await conn.fetchval("""INSERT INTO user_moderation(user_id,blocked,reason)
                    VALUES($1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET
                    blocked=$2,reason=$3,updated_at=now()
                    WHERE user_moderation.blocked IS DISTINCT FROM $2 OR user_moderation.reason IS DISTINCT FROM $3
                    RETURNING true""", user_id, blocked, reason)
                if changed:
                    await conn.execute("""INSERT INTO moderation_events(user_id,action,reason,moderator)
                        VALUES($1,$2,$3,$4)""", user_id, 'block' if blocked else 'unblock', reason, moderator)
                    await event(conn, 'moderation_block' if blocked else 'moderation_unblock', user_id)
                return bool(changed)

    async def moderation_history(self, user_id):
        return await self.pool.fetch('SELECT * FROM moderation_events WHERE user_id=$1 ORDER BY id DESC LIMIT 50', user_id)

    async def draft_count(self):
        return await self.pool.fetchval('SELECT count(*) FROM drafts')

    async def candidate(self, user_id):
        return await self.pool.fetchrow('''SELECT p.* FROM profiles p
            JOIN profiles me ON me.user_id=$1
            WHERE me.active AND p.active AND p.user_id<>$1 AND p.goals && me.goals
            AND NOT EXISTS(SELECT 1 FROM user_moderation m WHERE m.blocked AND m.user_id IN (me.user_id,p.user_id))
            AND NOT EXISTS(SELECT 1 FROM reactions r WHERE r.actor=$1 AND r.target=p.user_id)
            ORDER BY p.created_at DESC,p.user_id LIMIT 1''', user_id)

    async def react(self, actor, target, liked):
        if actor == target:
            return False, False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize both directions of a pair without a cross-process race.
                for uid in sorted((actor, target)):
                    await conn.execute('SELECT pg_advisory_xact_lock($1)', uid)
                if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM user_moderation WHERE blocked AND user_id=ANY($1::bigint[]))', [actor, target]):
                    return False, False
                rows = await conn.fetch('''SELECT * FROM profiles
                    WHERE user_id=ANY($1::bigint[]) ORDER BY user_id FOR UPDATE''', [actor, target])
                if len(rows) != 2 or not all(r['active'] for r in rows):
                    return False, False
                if not set(rows[0]['goals']) & set(rows[1]['goals']):
                    return False, False
                result = await conn.fetchval('''INSERT INTO reactions(actor,target,liked)
                    VALUES($1,$2,$3) ON CONFLICT(actor,target) DO NOTHING RETURNING true''', actor, target, liked)
                mutual = liked and bool(await conn.fetchval(
                    'SELECT liked FROM reactions WHERE actor=$1 AND target=$2', target, actor))
                if result:
                    await event(conn, 'profile_liked' if liked else 'profile_skipped', actor, {'target_id': target})
                    if mutual:
                        await event(conn, 'match_created', actor, {'target_id': target})
                return bool(result), bool(result and mutual)

    async def matches(self, user_id):
        return await self.pool.fetch('''SELECT p.* FROM profiles p
            JOIN reactions a ON a.target=p.user_id AND a.actor=$1 AND a.liked
            JOIN reactions b ON b.actor=p.user_id AND b.target=$1 AND b.liked
            WHERE p.active
            AND NOT EXISTS(SELECT 1 FROM user_moderation m WHERE m.blocked AND m.user_id IN ($1,p.user_id))
            ORDER BY a.created_at DESC LIMIT 50''', user_id)
