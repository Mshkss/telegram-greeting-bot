"""Event and participant services shared by bot, admin and future discovery integrations."""
import json
import secrets
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlsplit

from app.analytics import event
from app.post_format import parse_post,safe_url

STATUSES = {'draft':'Черновик','open':'Регистрация открыта','closed':'Регистрация закрыта',
            'ongoing':'Идёт','finished':'Завершено','cancelled':'Отменено'}
SCHEMA = """
CREATE TABLE participants (
 user_id BIGINT PRIMARY KEY, username TEXT, name TEXT NOT NULL, age SMALLINT NOT NULL CHECK(age BETWEEN 18 AND 100),
 photo_file_id TEXT NOT NULL, stack TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE participant_drafts (
 user_id BIGINT PRIMARY KEY, step TEXT NOT NULL, data JSONB NOT NULL, version TEXT NOT NULL,
 attempt_id UUID NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE participant_archives (
 id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,snapshot JSONB NOT NULL,deleted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE analytics_attempts ADD COLUMN format TEXT NOT NULL DEFAULT 'legacy';
CREATE TABLE events (
 id BIGSERIAL PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL,body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','open','closed','ongoing','finished','cancelled')),
 starts_at TIMESTAMPTZ NOT NULL,ends_at TIMESTAMPTZ NOT NULL,registration_until TIMESTAMPTZ NOT NULL,
 source_url TEXT NOT NULL DEFAULT '',origin TEXT NOT NULL DEFAULT 'manual',external_key TEXT UNIQUE,
 revision INTEGER NOT NULL DEFAULT 1,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(ends_at>starts_at),CHECK(registration_until<=starts_at)
);
CREATE TABLE event_registrations (
 event_id BIGINT REFERENCES events(id),user_id BIGINT NOT NULL,needs_team BOOLEAN NOT NULL,
 active BOOLEAN NOT NULL DEFAULT true,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(event_id,user_id)
);
CREATE INDEX event_registrations_user_idx ON event_registrations(user_id,event_id);
CREATE TABLE event_revisions (
 event_id BIGINT REFERENCES events(id),revision INTEGER NOT NULL,payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(event_id,revision)
);
"""


def status_of(row, now=None):
    now=now or datetime.now(timezone.utc)
    if row['status'] in ('draft','cancelled','finished'):
        return row['status']
    if row['ends_at'] and now>=row['ends_at']: return 'finished'
    if row['starts_at'] and now>=row['starts_at']: return 'ongoing'
    if row['status']=='closed' or row['registration_until'] and now>=row['registration_until']: return 'closed'
    return row['status']


def validate_participant(data):
    for key,limit in [('name',64),('stack',500)]:
        if not isinstance(data.get(key),str) or not 1<=len(data[key].strip())<=limit:
            raise ValueError(f'{key}: от 1 до {limit} символов')
    if type(data.get('age')) is not int or not 18<=data['age']<=100:
        raise ValueError('Возраст от 18 до 100 лет')
    if not data.get('photo_file_id'): raise ValueError('Добавьте фото')


class EventStore:
    async def participant(self,uid):
        return await self.pool.fetchrow('SELECT * FROM participants WHERE user_id=$1',uid)

    async def participant_draft(self,uid):
        row=await self.pool.fetchrow('SELECT * FROM participant_drafts WHERE user_id=$1',uid)
        return {**dict(row),'data':json.loads(row['data'])} if row else None

    async def save_participant_draft(self,uid,step,data):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1)',uid)
            old=await c.fetchrow('SELECT * FROM participant_drafts WHERE user_id=$1',uid)
            attempt=old['attempt_id'] if old and step!='consent' else uuid4()
            if not old or step=='consent':
                if old: await c.execute("UPDATE analytics_attempts SET status='restarted',last_activity=now() WHERE id=$1",old['attempt_id'])
                editing=bool(await c.fetchval('SELECT 1 FROM participants WHERE user_id=$1',uid))
                await c.execute("INSERT INTO analytics_attempts(id,user_id,is_edit,last_step,format) VALUES($1,$2,$3,$4,'participant')",attempt,uid,editing,step)
                await event(c,'participant_fill_started',uid,{'attempt_id':str(attempt)})
            elif old['step']!=step:
                await c.execute('UPDATE analytics_attempts SET completed_steps=array_append(completed_steps,$2) WHERE id=$1 AND NOT ($2=ANY(completed_steps))',attempt,old['step'])
                await event(c,'participant_step_completed',uid,{'step':old['step'],'attempt_id':str(attempt)})
            await c.execute('UPDATE analytics_attempts SET last_step=$2,last_activity=now() WHERE id=$1',attempt,step)
            version=secrets.token_hex(4)
            await c.execute('''INSERT INTO participant_drafts(user_id,step,data,version,attempt_id) VALUES($1,$2,$3,$4,$5)
                ON CONFLICT(user_id) DO UPDATE SET step=$2,data=$3,version=$4,attempt_id=$5,updated_at=now()''',uid,step,json.dumps(data),version,attempt)
        return {'step':step,'data':data,'version':version}

    async def finish_participant(self,uid,version=None,delete=False):
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1)',uid)
            draft=await c.fetchrow('SELECT * FROM participant_drafts WHERE user_id=$1',uid)
            if version:
                if not draft or draft['version']!=version or draft['step']!='preview': return False
                d=json.loads(draft['data']); validate_participant(d)
                await c.execute('''INSERT INTO participants(user_id,username,name,age,photo_file_id,stack) VALUES($1,$2,$3,$4,$5,$6)
                    ON CONFLICT(user_id) DO UPDATE SET username=$2,name=$3,age=$4,photo_file_id=$5,stack=$6,updated_at=now()''',uid,d.get('username'),d['name'],d['age'],d['photo_file_id'],d['stack'])
                await c.execute("UPDATE analytics_attempts SET status='published',published_at=now(),last_activity=now() WHERE id=$1",draft['attempt_id'])
                await c.execute("INSERT INTO analytics_users(user_id,first_published_at) VALUES($1,now()) ON CONFLICT(user_id) DO UPDATE SET first_published_at=coalesce(analytics_users.first_published_at,now())",uid)
                await event(c,'participant_saved',uid,{'attempt_id':str(draft['attempt_id'])})
            elif draft:
                await c.execute("UPDATE analytics_attempts SET status=$2,last_activity=now() WHERE id=$1",draft['attempt_id'],'deleted' if delete else 'cancelled')
            if delete:
                profile=await c.fetchval('SELECT to_jsonb(p) FROM participants p WHERE user_id=$1',uid)
                if profile or draft:
                    await c.execute('INSERT INTO participant_archives(user_id,snapshot) VALUES($1,$2)',uid,json.dumps({'profile':json.loads(profile) if profile else None,'draft':json.loads(draft['data']) if draft else None}))
                    await event(c,'participant_deleted',uid)
                for registration in await c.fetch('SELECT event_id FROM event_registrations WHERE user_id=$1 AND active',uid):
                    await event(c,'event_registration_cancelled',uid,{'event_id':registration['event_id'],'reason':'profile_deleted'})
                await c.execute('DELETE FROM participants WHERE user_id=$1',uid)
                await c.execute('UPDATE event_registrations SET active=false,updated_at=now() WHERE user_id=$1 AND active',uid)
            await c.execute('DELETE FROM participant_drafts WHERE user_id=$1',uid)
            return True

    async def event_record(self,eid):
        return await self.pool.fetchrow('SELECT * FROM events WHERE id=$1',eid)

    async def event_page(self,page=1):
        async with self.pool.acquire() as c,c.transaction(isolation='repeatable_read',readonly=True):
            total=await c.fetchval("SELECT count(*) FROM events WHERE status<>'draft'")
            pages=max(1,(total+7)//8);page=min(max(1,page),pages)
            rows=await c.fetch("""SELECT * FROM events WHERE status<>'draft' ORDER BY
                CASE WHEN status='open' AND (registration_until IS NULL OR registration_until>now()) THEN 0
                WHEN status NOT IN ('cancelled','finished') AND starts_at<=now() AND ends_at>now() THEN 1 ELSE 2 END,
                starts_at NULLS LAST,id LIMIT 8 OFFSET $1""",(page-1)*8)
        return rows,page,pages

    async def event_list(self,page=1,admin=False):
        where='' if admin else "WHERE status<>'draft'"
        order='starts_at DESC NULLS LAST,id DESC' if admin else "CASE WHEN status='open' AND (registration_until IS NULL OR registration_until>now()) THEN 0 WHEN status NOT IN ('cancelled','finished') AND starts_at<=now() AND ends_at>now() THEN 1 ELSE 2 END,starts_at,id"
        return await self.pool.fetch('SELECT * FROM events '+where+' ORDER BY '+order+' LIMIT 10 OFFSET $1',(page-1)*10)

    async def save_event(self,data,eid=None,revision=None,origin='manual',external_key=None):
        data=dict(data)
        if data.get('body_html') is not None:
            data['body_html'],data['body'],data['title']=parse_post(data['body_html'])
            data['summary']=data['body'][:350]
        if data.get('channel_post_url'): safe_url(data['channel_post_url'],channel=True)
        if len(data.get('channel_post_url',''))>500: raise ValueError('Ссылка на пост слишком длинная')
        for k,limit in [('title',120),('summary',350),('body',3000)]:
            if not 1<=len(data.get(k,'').strip())<=limit: raise ValueError(f'{k}: от 1 до {limit} символов')
        if data['status'] not in STATUSES: raise ValueError('Неизвестный статус')
        if data.get('ends_at') and data.get('starts_at') and data['ends_at']<=data['starts_at']: raise ValueError('Окончание должно быть после начала')
        if data.get('registration_until') and data.get('starts_at') and data['registration_until']>data['starts_at']: raise ValueError('Дедлайн не позже начала')
        if len(data.get('source_url',''))>2000: raise ValueError('Ссылка: до 2000 символов')
        if data.get('source_url'):
            u=urlsplit(data['source_url'])
            if u.scheme not in ('https','http') or not u.hostname or u.username or u.password: raise ValueError('Нужна ссылка http(s)')
        async with self.pool.acquire() as c,c.transaction():
            vals=[data[k] for k in ('title','summary','body','status','starts_at','ends_at','registration_until')]+[data.get('source_url','')]
            if eid:
                row=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',eid)
                if not row: raise LookupError('Событие не найдено')
                if row['revision']!=revision: raise ValueError('Событие уже изменено. Обновите страницу.')
                await c.execute('''UPDATE events SET title=$2,summary=$3,body=$4,status=$5,starts_at=$6,ends_at=$7,
                    registration_until=$8,source_url=$9,revision=revision+1,updated_at=now() WHERE id=$1''',eid,*vals)
            else:
                # Discovery integrations must create drafts for human review.
                if origin!='manual': vals[3]='draft'
                eid=await c.fetchval('''INSERT INTO events(title,summary,body,status,starts_at,ends_at,registration_until,source_url,origin,external_key)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id''',*vals,origin,external_key)
            if 'body_html' in data:
                await c.execute('UPDATE events SET body_html=$2 WHERE id=$1',eid,data['body_html'])
            elif eid:
                await c.execute('UPDATE events SET body_html=NULL WHERE id=$1',eid)
            if 'channel_post_url' in data:
                await c.execute('UPDATE events SET channel_post_url=$2 WHERE id=$1',eid,data['channel_post_url'])
            await c.execute('INSERT INTO event_revisions(event_id,revision,payload) SELECT id,revision,to_jsonb(e) FROM events e WHERE id=$1',eid)
            await event(c,'event_updated',properties={'event_id':eid})
        return eid

    async def register_event(self,eid,uid,needs_team=None,cancel=False):
        async with self.pool.acquire() as c,c.transaction():
            # Profile deletion and sign-up share the same user lock.
            await c.execute('SELECT pg_advisory_xact_lock($1)',uid)
            row=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',eid)
            if not row or (row['status']=='draft' and not cancel): raise ValueError('Событие недоступно')
            old=await c.fetchrow('SELECT * FROM event_registrations WHERE event_id=$1 AND user_id=$2',eid,uid)
            if cancel:
                if old and old['active']:
                    await c.execute('UPDATE event_registrations SET active=false,updated_at=now() WHERE event_id=$1 AND user_id=$2',eid,uid)
                    await event(c,'event_registration_cancelled',uid,{'event_id':eid})
                return
            if type(needs_team) is not bool: raise ValueError('Выберите, нужна ли команда')
            if status_of(row)!='open': raise ValueError('Регистрация уже закрыта')
            if await c.fetchval('SELECT blocked FROM user_moderation WHERE user_id=$1',uid): raise ValueError('Запись ограничена модератором')
            if not await c.fetchval('SELECT 1 FROM participants WHERE user_id=$1',uid): raise ValueError('Сначала заполните профиль')
            if old and old['active'] and old['needs_team']==needs_team: return
            await c.execute('''INSERT INTO event_registrations(event_id,user_id,needs_team) VALUES($1,$2,$3)
                ON CONFLICT(event_id,user_id) DO UPDATE SET needs_team=$3,active=true,updated_at=now()''',eid,uid,needs_team)
            await event(c,'event_registered',uid,{'event_id':eid,'needs_team':needs_team})
