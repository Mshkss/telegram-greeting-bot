"""Event-scoped teams. Every mutation locks users then the event, matching registration locking."""
import json
import re
import secrets
from app.analytics import event
from app.events import status_of
from app.post_format import safe_url

ROLES={'backend':'Backend','frontend':'Frontend','mobile':'Mobile','ml':'ML / Data Science',
       'design':'Дизайн','analytics':'Аналитика','product':'Product / Pitch'}
EXPERIENCE={'first':'Первый хакатон','some':'Уже участвовал','many':'Опытный участник'}
HOURS={'few':'Несколько часов в день','half':'Полдня','full':'Полный день'}
SCHEMA='''
CREATE TABLE matching_profiles (
 user_id BIGINT PRIMARY KEY REFERENCES participants(user_id) ON DELETE CASCADE,
 roles TEXT[] NOT NULL,experience TEXT NOT NULL,consented_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE event_preferences (
 event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,user_id BIGINT REFERENCES participants(user_id) ON DELETE CASCADE,
 roles TEXT[] NOT NULL,hours TEXT NOT NULL,PRIMARY KEY(event_id,user_id)
);
CREATE TABLE teams (
 id BIGSERIAL PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 captain_id BIGINT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,
 wanted_roles TEXT[] NOT NULL,tags TEXT[] NOT NULL,hours TEXT NOT NULL,capacity INTEGER NOT NULL CHECK(capacity BETWEEN 2 AND 10),
 chat_url TEXT NOT NULL DEFAULT '',active BOOLEAN NOT NULL DEFAULT true,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(id,event_id)
);
CREATE TABLE team_members (
 event_id BIGINT NOT NULL,user_id BIGINT NOT NULL REFERENCES participants(user_id) ON DELETE CASCADE,
 team_id BIGINT NOT NULL,role TEXT,joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(event_id,user_id),FOREIGN KEY(team_id,event_id) REFERENCES teams(id,event_id) ON DELETE CASCADE
);
CREATE TABLE team_applications (
 id BIGSERIAL PRIMARY KEY,team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
 user_id BIGINT NOT NULL REFERENCES participants(user_id) ON DELETE CASCADE,role TEXT NOT NULL,
 message TEXT NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('apply','invite')),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','withdrawn','cancelled')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(team_id,user_id)
);
CREATE INDEX team_applications_inbox_idx ON team_applications(user_id,status);
CREATE INDEX team_members_team_idx ON team_members(team_id);
CREATE TABLE team_ui_drafts (
 user_id BIGINT PRIMARY KEY,step TEXT NOT NULL,data JSONB NOT NULL,version TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
'''


def tags(text):
    aliases={'js':'javascript','ts':'typescript','py':'python','node.js':'nodejs','react.js':'react','reactjs':'react','postgresql':'postgres'}
    return sorted({aliases.get(t,t) for t in re.findall(r'[\w+#.\-]+',text.lower()) if len(t)>1})[:40]


def validate_roles(roles):
    if not roles or len(roles)>len(ROLES) or len(set(roles))!=len(roles) or any(r not in ROLES for r in roles):
        raise ValueError('Выбери хотя бы одну роль кнопкой.')


async def lock_users(c,*uids):
    for uid in sorted(set(uids)): await c.execute('SELECT pg_advisory_xact_lock($1)',uid)


async def eligible(c,eid,uid,profile=True):
    e=await c.fetchrow('SELECT * FROM events WHERE id=$1 FOR UPDATE',eid)
    if not e or status_of(e) in ('draft','finished','cancelled'): raise ValueError('Набор команд на это событие недоступен.')
    if await c.fetchval('SELECT blocked FROM user_moderation WHERE user_id=$1',uid): raise ValueError('Доступ ограничен модератором.')
    if not await c.fetchval('SELECT 1 FROM event_registrations WHERE event_id=$1 AND user_id=$2 AND active',eid,uid):
        raise ValueError('Сначала запишись на этот хакатон.')
    if profile and not await c.fetchval('SELECT 1 FROM event_preferences WHERE event_id=$1 AND user_id=$2',eid,uid):
        raise ValueError('Сначала заполни роли и доступность в разделе «Профиль для команды».')
    return e


async def detach_member(c,eid,uid,removed_by=None):
    """Used atomically by leave, registration cancellation and profile removal."""
    member=await c.fetchrow('SELECT m.*,t.captain_id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.event_id=$1 AND m.user_id=$2',eid,uid)
    if member:
        if member['captain_id']==uid:
            members=await c.fetch('DELETE FROM team_members WHERE team_id=$1 RETURNING user_id',member['team_id'])
            await c.execute('UPDATE teams SET active=false,chat_url=\'\' WHERE id=$1',member['team_id'])
            await c.execute("UPDATE team_applications SET status='cancelled',updated_at=now() WHERE team_id=$1 AND status IN ('pending','accepted')",member['team_id'])
            await c.execute('UPDATE event_registrations SET needs_team=true WHERE event_id=$1 AND user_id=ANY($2::bigint[]) AND active',eid,[m['user_id'] for m in members])
            await event(c,'team_closed',uid,{'event_id':eid,'team_id':member['team_id']})
        else:
            await c.execute('DELETE FROM team_members WHERE event_id=$1 AND user_id=$2',eid,uid)
            await c.execute('UPDATE event_registrations SET needs_team=true WHERE event_id=$1 AND user_id=$2 AND active',eid,uid)
        await event(c,'team_member_removed' if removed_by else 'team_left',removed_by or uid,{'event_id':eid,'team_id':member['team_id'],'target_id':uid})
    await c.execute("UPDATE team_applications a SET status='withdrawn',updated_at=now() FROM teams t WHERE a.team_id=t.id AND t.event_id=$1 AND a.user_id=$2 AND a.status='accepted'",eid,uid)
    await c.execute("UPDATE team_applications a SET status='cancelled',updated_at=now() FROM teams t WHERE a.team_id=t.id AND t.event_id=$1 AND a.user_id=$2 AND a.status='pending'",eid,uid)


class TeamStore:
    async def team_draft(self,uid):
        row=await self.pool.fetchrow('SELECT * FROM team_ui_drafts WHERE user_id=$1',uid)
        return {**dict(row),'data':json.loads(row['data'])} if row else None

    async def save_team_draft(self,uid,step,data):
        version=secrets.token_hex(4)
        await self.pool.execute('''INSERT INTO team_ui_drafts(user_id,step,data,version) VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id) DO UPDATE SET step=$2,data=$3,version=$4,updated_at=now()''',uid,step,json.dumps(data),version)
        return {'step':step,'data':data,'version':version}

    async def set_matching_profile(self,eid,uid,roles,experience,hours):
        validate_roles(roles)
        if experience not in EXPERIENCE or hours not in HOURS: raise ValueError('Используй предложенные варианты.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid);await eligible(c,eid,uid,False)
            await c.execute('INSERT INTO matching_profiles(user_id,roles,experience) VALUES($1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET roles=$2,experience=$3,consented_at=now()',uid,roles,experience)
            await c.execute('INSERT INTO event_preferences(event_id,user_id,roles,hours) VALUES($1,$2,$3,$4) ON CONFLICT(event_id,user_id) DO UPDATE SET roles=$3,hours=$4',eid,uid,roles,hours)
            await c.execute('UPDATE teams SET hours=$3 WHERE event_id=$1 AND captain_id=$2 AND active',eid,uid,hours)
            await event(c,'matching_profile_saved',uid,{'event_id':eid})

    async def my_team(self,eid,uid):
        return await self.pool.fetchrow('SELECT t.* FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.event_id=$1 AND m.user_id=$2 AND t.active',eid,uid)

    async def create_team(self,eid,uid,data):
        validate_roles(data['wanted_roles'])
        if not 1<=len(data['name'].strip())<=64 or not 1<=len(data['description'].strip())<=500: raise ValueError('Название до 64 символов, описание до 500.')
        if type(data['capacity']) is not int or not 2<=data['capacity']<=10: raise ValueError('В команде от 2 до 10 человек, включая капитана.')
        if len(data.get('tags',''))>200: raise ValueError('Стек команды: до 200 символов.')
        if data.get('chat_url'):
            safe_url(data['chat_url'])
            if len(data['chat_url'])>500: raise ValueError('Ссылка слишком длинная.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid);await eligible(c,eid,uid)
            if await c.fetchval('SELECT 1 FROM team_members WHERE event_id=$1 AND user_id=$2',eid,uid): raise ValueError('Ты уже состоишь в команде этого хакатона.')
            hours=await c.fetchval('SELECT hours FROM event_preferences WHERE event_id=$1 AND user_id=$2',eid,uid)
            tid=await c.fetchval('''INSERT INTO teams(event_id,captain_id,name,description,wanted_roles,tags,hours,capacity,chat_url)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id''',eid,uid,data['name'].strip(),data['description'].strip(),data['wanted_roles'],tags(data.get('tags','')),hours,data['capacity'],data.get('chat_url',''))
            await c.execute('INSERT INTO team_members(event_id,user_id,team_id) VALUES($1,$2,$3)',eid,uid,tid)
            await c.execute('UPDATE event_registrations SET needs_team=false WHERE event_id=$1 AND user_id=$2',eid,uid)
            await c.execute("UPDATE team_applications a SET status='cancelled' FROM teams t WHERE a.team_id=t.id AND t.event_id=$1 AND a.user_id=$2 AND a.status='pending'",eid,uid)
            await event(c,'team_created',uid,{'event_id':eid,'team_id':tid})
            return tid

    async def team_detail(self,tid,uid):
        async with self.pool.acquire() as c,c.transaction():
            t=await c.fetchrow('SELECT * FROM teams WHERE id=$1 AND active',tid)
            if not t: raise ValueError('Команда недоступна.')
            await eligible(c,t['event_id'],uid,False)
            members=await c.fetch('SELECT m.user_id,m.role,p.name FROM team_members m JOIN participants p USING(user_id) WHERE m.team_id=$1 ORDER BY joined_at,user_id',tid)
            filled={m['role'] for m in members}
            d=dict(t);d['members']=members;d['vacancies']=[r for r in t['wanted_roles'] if r not in filled]
            d['is_member']=any(m['user_id']==uid for m in members)
            if not d['is_member']: d['chat_url']=''
            if len(members)>=t['capacity']: d['vacancies']=[]
            return d

    async def team_catalog(self,eid,uid,all_teams=False):
        async with self.pool.acquire() as c,c.transaction():
            await eligible(c,eid,uid)
            pref=await c.fetchrow('SELECT * FROM event_preferences WHERE event_id=$1 AND user_id=$2',eid,uid)
            p=await c.fetchrow('SELECT stack FROM participants WHERE user_id=$1',uid)
            rows=await c.fetch('''SELECT t.*,array(SELECT m.role FROM team_members m WHERE m.team_id=t.id AND m.role IS NOT NULL) AS filled,
                (SELECT count(*) FROM team_members m WHERE m.team_id=t.id) AS size FROM teams t
                WHERE t.event_id=$1 AND t.active AND NOT EXISTS(SELECT 1 FROM team_members own WHERE own.team_id=t.id AND own.user_id=$2) AND NOT EXISTS(SELECT 1 FROM user_moderation um WHERE um.user_id=t.captain_id AND um.blocked)''',eid,uid)
            results=[]
            for t in rows:
                vacant=[r for r in t['wanted_roles'] if r not in t['filled']] if t['size']<t['capacity'] else []
                roles=set(pref['roles']) & set(vacant);common=set(tags(p['stack'])) & set(t['tags'])
                if not vacant or (not all_teams and not roles): continue
                reasons=[]
                if roles: reasons.append('Нужная роль: '+', '.join(ROLES[r] for r in sorted(roles)))
                if common: reasons.append('Общий стек: '+', '.join(sorted(common)))
                if t['hours']==pref['hours']: reasons.append('Совпадает доступное время')
                # Never expose chat links to non-members in recommendation results.
                results.append({'id':t['id'],'name':t['name'],'reasons':reasons or ['Есть открытые роли'],
                    'rank':(len(roles),len(common),t['hours']==pref['hours'])})
            return sorted(results,key=lambda t:(tuple(-int(v) for v in t['rank']),t['id']))

    async def candidates(self,tid,uid):
        team=await self.team_detail(tid,uid)
        if team['captain_id']!=uid: raise ValueError('Поиск участников доступен капитану.')
        rows=await self.pool.fetch('''SELECT p.user_id,p.name,p.stack,f.roles,f.hours,mp.experience FROM participants p
            JOIN event_preferences f ON f.user_id=p.user_id AND f.event_id=$1 JOIN matching_profiles mp ON mp.user_id=p.user_id
            JOIN event_registrations r ON r.user_id=p.user_id AND r.event_id=f.event_id
            WHERE r.active AND r.needs_team AND NOT EXISTS(SELECT 1 FROM team_members m WHERE m.event_id=f.event_id AND m.user_id=p.user_id)
            AND NOT EXISTS(SELECT 1 FROM user_moderation um WHERE um.user_id=p.user_id AND um.blocked)''',team['event_id'])
        result=[]
        for p in rows:
            roles=set(p['roles']) & set(team['vacancies']);common=set(tags(p['stack'])) & set(team['tags'])
            if roles:
                result.append({**dict(p),'roles':sorted(roles),'reasons':['Роли: '+', '.join(ROLES[r] for r in sorted(roles))]+(['Общий стек: '+', '.join(sorted(common))] if common else [])+(['Совпадает время'] if p['hours']==team['hours'] else []),
                    'rank':(len(roles),len(common),p['hours']==team['hours'])})
        return sorted(result,key=lambda p:(tuple(-int(v) for v in p['rank']),p['user_id']))

    async def apply_team(self,tid,uid,role,message,direction='apply',captain=None):
        if role not in ROLES or len(message)>300: raise ValueError('Выбери роль; сообщение до 300 символов.')
        t=await self.pool.fetchrow('SELECT * FROM teams WHERE id=$1',tid)
        if not t: raise ValueError('Команда недоступна.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid,*([captain] if captain else []));await eligible(c,t['event_id'],uid)
            t=await c.fetchrow('SELECT * FROM teams WHERE id=$1 AND active',tid)
            if not t: raise ValueError('Команда недоступна.')
            await eligible(c,t['event_id'],t['captain_id'],False)
            if direction=='invite' and captain!=t['captain_id']: raise ValueError('Приглашать может только капитан.')
            if direction not in ('apply','invite'): raise ValueError('Неверная заявка.')
            if uid==t['captain_id'] or await c.fetchval('SELECT 1 FROM team_members WHERE event_id=$1 AND user_id=$2',t['event_id'],uid): raise ValueError('Участник уже состоит в команде.')
            roles=await c.fetchval('SELECT roles FROM event_preferences WHERE event_id=$1 AND user_id=$2',t['event_id'],uid)
            if role not in roles: raise ValueError('Эта роль не указана в профиле участника.')
            if role not in t['wanted_roles'] or await c.fetchval('SELECT 1 FROM team_members WHERE team_id=$1 AND role=$2',tid,role): raise ValueError('Роль уже занята.')
            if await c.fetchval('SELECT count(*) FROM team_members WHERE team_id=$1',tid)>=t['capacity']: raise ValueError('Команда заполнена.')
            if direction=='invite' and not await c.fetchval('SELECT needs_team FROM event_registrations WHERE event_id=$1 AND user_id=$2',t['event_id'],uid): raise ValueError('Участник не ищет команду.')
            old=await c.fetchrow('SELECT * FROM team_applications WHERE team_id=$1 AND user_id=$2',tid,uid)
            if old and old['status'] in ('pending','accepted','rejected'): raise ValueError('Для этой команды уже есть заявка или решение. Проверь «Мои заявки».')
            aid=await c.fetchval('''INSERT INTO team_applications(team_id,user_id,role,message,direction) VALUES($1,$2,$3,$4,$5)
                ON CONFLICT(team_id,user_id) DO UPDATE SET role=$3,message=$4,direction=$5,status='pending',updated_at=now() RETURNING id''',tid,uid,role,message,direction)
            await event(c,'team_application_sent',uid,{'event_id':t['event_id'],'team_id':tid,'direction':direction})
            return aid

    async def decide_application(self,aid,actor,accept):
        a=await self.pool.fetchrow('SELECT a.*,t.event_id,t.captain_id FROM team_applications a JOIN teams t ON t.id=a.team_id WHERE a.id=$1',aid)
        if not a: raise ValueError('Заявка не найдена.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,actor,a['user_id']);await eligible(c,a['event_id'],actor,False)
            t=await c.fetchrow('SELECT * FROM teams WHERE id=$1 AND active',a['team_id'])
            if not t: raise ValueError('Команда недоступна.')
            current=await c.fetchrow('SELECT * FROM team_applications WHERE id=$1 FOR UPDATE',aid)
            owner=a['user_id'] if current['direction']=='invite' else t['captain_id']
            if actor!=owner: raise ValueError('Решение доступно только получателю заявки.')
            if current['status']!='pending': return False,a['user_id'],t['id']
            if accept:
                roles=await c.fetchval('SELECT roles FROM event_preferences WHERE event_id=$1 AND user_id=$2',a['event_id'],a['user_id'])
                if not roles or current['role'] not in roles: raise ValueError('Участник изменил роли в профиле. Нужна новая заявка.')
                await eligible(c,a['event_id'],a['user_id']);await eligible(c,a['event_id'],t['captain_id'],False)
                if await c.fetchval('SELECT 1 FROM team_members WHERE event_id=$1 AND user_id=$2',a['event_id'],a['user_id']): raise ValueError('Участник уже вступил в другую команду.')
                if current['role'] not in t['wanted_roles'] or await c.fetchval('SELECT 1 FROM team_members WHERE team_id=$1 AND role=$2',t['id'],current['role']): raise ValueError('Роль уже занята.')
                if await c.fetchval('SELECT count(*) FROM team_members WHERE team_id=$1',t['id'])>=t['capacity']: raise ValueError('Команда заполнена.')
                await c.execute('INSERT INTO team_members(event_id,user_id,team_id,role) VALUES($1,$2,$3,$4)',a['event_id'],a['user_id'],t['id'],current['role'])
                await c.execute('UPDATE event_registrations SET needs_team=false WHERE event_id=$1 AND user_id=$2',a['event_id'],a['user_id'])
                await c.execute("UPDATE team_applications a SET status='cancelled' FROM teams t WHERE a.team_id=t.id AND t.event_id=$1 AND a.user_id=$2 AND a.status='pending' AND a.id<>$3",a['event_id'],a['user_id'],aid)
            await c.execute("UPDATE team_applications SET status=$2,updated_at=now() WHERE id=$1",aid,'accepted' if accept else 'rejected')
            await event(c,'team_application_accepted' if accept else 'team_application_rejected',actor,{'event_id':a['event_id'],'team_id':t['id']})
            return True,(t['captain_id'] if current['direction']=='invite' else a['user_id']),t['id']

    async def withdraw_application(self,aid,uid):
        a=await self.pool.fetchrow('SELECT a.*,t.event_id FROM team_applications a JOIN teams t ON t.id=a.team_id WHERE a.id=$1',aid)
        if not a: raise ValueError('Заявка не найдена.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid);await c.fetchrow('SELECT id FROM events WHERE id=$1 FOR UPDATE',a['event_id'])
            captain=await c.fetchval('SELECT captain_id FROM teams WHERE id=$1',a['team_id'])
            owner=captain if a['direction']=='invite' else a['user_id']
            if owner!=uid: raise ValueError('Нельзя отозвать чужую заявку.')
            changed=await c.fetchval("UPDATE team_applications SET status='withdrawn',updated_at=now() WHERE id=$1 AND status='pending' RETURNING id",aid)
            if changed: await event(c,'team_application_withdrawn',uid,{'event_id':a['event_id'],'team_id':a['team_id']})
            return a['event_id'],a['team_id'] if a['direction']=='invite' else None

    async def manage_team(self,tid,uid,action,target=None,value=None):
        t=await self.pool.fetchrow('SELECT * FROM teams WHERE id=$1',tid)
        if not t: raise ValueError('Команда недоступна.')
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid,*([target] if target else []))
            if not await c.fetchrow('SELECT id FROM events WHERE id=$1 FOR UPDATE',t['event_id']): raise ValueError('Событие недоступно.')
            t=await c.fetchrow('SELECT * FROM teams WHERE id=$1 AND active',tid)
            if not t: raise ValueError('Команда недоступна.')
            member=await c.fetchval('SELECT 1 FROM team_members WHERE team_id=$1 AND user_id=$2',tid,uid)
            if not member: raise ValueError('Ты не состоишь в этой команде.')
            if action=='leave':
                if uid==t['captain_id']: raise ValueError('Передай капитанство или распусти команду.')
                await detach_member(c,t['event_id'],uid);return
            if uid!=t['captain_id']: raise ValueError('Это действие доступно капитану.')
            if action not in ('close','remove'):
                await eligible(c,t['event_id'],uid,False)
            if action=='close': await detach_member(c,t['event_id'],uid)
            elif action=='transfer':
                if target==uid or not await c.fetchval('SELECT 1 FROM team_members WHERE team_id=$1 AND user_id=$2',tid,target): raise ValueError('Выбери другого участника команды.')
                await c.execute('UPDATE teams SET captain_id=$2 WHERE id=$1',tid,target)
                await event(c,'team_captain_changed',uid,{'event_id':t['event_id'],'team_id':tid,'target_id':target})
            elif action=='remove':
                if target==uid or not await c.fetchval('SELECT 1 FROM team_members WHERE team_id=$1 AND user_id=$2',tid,target): raise ValueError('Выбери участника команды.')
                await detach_member(c,t['event_id'],target,removed_by=uid)
            elif action=='chat':
                if value: safe_url(value)
                if len(value)>500: raise ValueError('Ссылка слишком длинная.')
                await c.execute('UPDATE teams SET chat_url=$2 WHERE id=$1',tid,value)
            elif action=='roles':
                validate_roles(value);await c.execute('UPDATE teams SET wanted_roles=$2 WHERE id=$1',tid,value)
            else: raise ValueError('Неизвестное действие.')

    async def admin_close_team(self,tid,reason,moderator):
        reason=reason.strip()
        if not 1<=len(reason)<=500: raise ValueError('Укажи причину: от 1 до 500 символов.')
        async with self.pool.acquire() as c,c.transaction():
            eid=await c.fetchval('SELECT event_id FROM teams WHERE id=$1',tid)
            if not eid: raise LookupError('Команда не найдена')
            await c.fetchrow('SELECT id FROM events WHERE id=$1 FOR UPDATE',eid)
            t=await c.fetchrow('SELECT * FROM teams WHERE id=$1',tid)
            if not t: raise LookupError('Команда не найдена')
            if not t['active']: return False
            await detach_member(c,eid,t['captain_id'])
            await event(c,'team_moderated',t['captain_id'],{'event_id':eid,'team_id':tid,'reason':reason,'moderator':moderator})
            return True

    async def set_team_visibility(self,eid,uid,seeking):
        async with self.pool.acquire() as c,c.transaction():
            await lock_users(c,uid);await eligible(c,eid,uid,False)
            if await c.fetchval('SELECT 1 FROM team_members WHERE event_id=$1 AND user_id=$2',eid,uid):
                raise ValueError('Сначала выйди из команды.')
            await c.execute('UPDATE event_registrations SET needs_team=$3,updated_at=now() WHERE event_id=$1 AND user_id=$2',eid,uid,seeking)
            await event(c,'team_search_shown' if seeking else 'team_search_hidden',uid,{'event_id':eid})
