import json
import secrets

import asyncpg

from app.domain import validate_profile

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


class Database:
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

    async def close(self):
        await self.pool.close()

    async def profile(self, user_id):
        return await self.pool.fetchrow('SELECT * FROM profiles WHERE user_id=$1', user_id)

    async def draft(self, user_id):
        row = await self.pool.fetchrow('SELECT * FROM drafts WHERE user_id=$1', user_id)
        if row:
            return {**dict(row), 'data': json.loads(row['data'])}

    async def save_draft(self, user_id, step, data):
        version = secrets.token_hex(4)
        await self.pool.execute('''INSERT INTO drafts(user_id,step,data,version) VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id) DO UPDATE SET step=$2,data=$3,version=$4,updated_at=now()''',
            user_id, step, json.dumps(data), version)
        return {'step': step, 'data': data, 'version': version}

    async def cancel(self, user_id):
        await self.pool.execute('DELETE FROM drafts WHERE user_id=$1', user_id)

    async def publish(self, user_id, version):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
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
                await conn.execute('DELETE FROM drafts WHERE user_id=$1', user_id)
                return True

    async def visibility(self, user_id, active):
        await self.pool.execute('UPDATE profiles SET active=$2,updated_at=now() WHERE user_id=$1', user_id, active)

    async def delete(self, user_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('DELETE FROM drafts WHERE user_id=$1', user_id)
                await conn.execute('DELETE FROM profiles WHERE user_id=$1', user_id)

    async def list_profiles(self, query='', page=1):
        where = "($1='' OR name ILIKE '%'||$1||'%' OR username ILIKE '%'||$1||'%' OR user_id::text=$1)"
        total = await self.pool.fetchval('SELECT count(*) FROM profiles WHERE '+where, query)
        rows = await self.pool.fetch('SELECT * FROM profiles WHERE '+where+
            ' ORDER BY created_at DESC,user_id LIMIT 30 OFFSET $2', query, (page-1)*30)
        return total, rows

    async def draft_count(self):
        return await self.pool.fetchval('SELECT count(*) FROM drafts')

    async def candidate(self, user_id):
        return await self.pool.fetchrow('''SELECT p.* FROM profiles p
            JOIN profiles me ON me.user_id=$1
            WHERE me.active AND p.active AND p.user_id<>$1 AND p.goals && me.goals
            AND NOT EXISTS(SELECT 1 FROM reactions r WHERE r.actor=$1 AND r.target=p.user_id)
            ORDER BY p.created_at DESC,p.user_id LIMIT 1''', user_id)

    async def react(self, actor, target, liked):
        if actor == target:
            return False, False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize both directions of a pair without a cross-process race.
                await conn.execute('SELECT pg_advisory_xact_lock($1)', min(actor, target))
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
                return bool(result), bool(result and mutual)

    async def matches(self, user_id):
        return await self.pool.fetch('''SELECT p.* FROM profiles p
            JOIN reactions a ON a.target=p.user_id AND a.actor=$1 AND a.liked
            JOIN reactions b ON b.actor=p.user_id AND b.target=$1 AND b.liked
            WHERE p.active ORDER BY a.created_at DESC LIMIT 50''', user_id)
