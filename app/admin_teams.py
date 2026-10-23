"""Authenticated team administration; public paths follow ADMIN_BASE_PATH."""
import json
from aiohttp import web
from app.admin_urls import base_path,admin_url
from app.admin_metrics import esc,table,card
from app.teams import ROLES,HOURS,EXPERIENCE

STATES={'pending':'Ожидает','accepted':'Принята','rejected':'Отклонена','withdrawn':'Отозвана','cancelled':'Отменена'}


def register_team_admin(app,db_key,user_key,page,csrf_token,check_csrf):
    def number(raw):
        try:
            value=int(raw)
            if not 0<value<=2**63-1: raise ValueError
            return value
        except ValueError: raise web.HTTPBadRequest(text='Некорректный номер')

    async def listing(request):
        db=request.app[db_key];eid=number(request.query['event_id']) if request.query.get('event_id') else None
        n=min(number(request.query.get('page','1')),100000)
        rows=await db.pool.fetch('''SELECT t.*,e.title,(SELECT count(*) FROM team_members m WHERE m.team_id=t.id) AS size,
            (SELECT count(*) FROM team_applications a WHERE a.team_id=t.id AND a.status='pending') AS pending
            FROM teams t JOIN events e ON e.id=t.event_id WHERE ($1::bigint IS NULL OR t.event_id=$1)
            ORDER BY t.active DESC,t.id DESC LIMIT 30 OFFSET $2''',eid,(n-1)*30)
        counts=await db.pool.fetchrow('''SELECT count(*) FILTER(WHERE active) AS active,count(*) FILTER(WHERE NOT active) AS closed
            FROM teams WHERE $1::bigint IS NULL OR event_id=$1''',eid)
        items=[(f'<a href="{base_path()}/teams/{t["id"]}">{esc(t["name"])}</a>',
            f'<a href="{base_path()}/events/{t["event_id"]}">{esc(t["title"])}</a>',
            f'<a href="{base_path()}/participants/{t["captain_id"]}">{t["captain_id"]}</a>',
            f'{t["size"]}/{t["capacity"]}',t['pending'],'Активна' if t['active'] else 'Распущена') for t in rows]
        suffix=f'&amp;event_id={eid}' if eid else ''
        nav=f'<a href="{base_path()}/teams?page={n+1}{suffix}">Далее →</a>' if len(rows)==30 else ''
        if n>1: nav+=f' <a href="{base_path()}/teams?page={n-1}{suffix}">← Назад</a>'
        return page('Команды',f'<h1>Команды</h1><div class="metrics">{card("Активных",counts["active"])}{card("Распущенных",counts["closed"])}</div>'
            +f'<form><label>ID события <input name="event_id" type="number" min="1" value="{eid or ""}"></label><button>Показать</button><a href="{base_path()}/teams">Все события</a></form>'
            +table(['Команда','Хакатон','Капитан · Telegram ID','Состав','Ожидают решения','Статус'],items)+nav)

    async def detail(request):
        tid=number(request.match_info['tid']);db=request.app[db_key]
        t=await db.pool.fetchrow('SELECT t.*,e.title FROM teams t JOIN events e ON e.id=t.event_id WHERE t.id=$1',tid)
        if not t: raise web.HTTPNotFound(text='Команда не найдена')
        if request.method=='POST':
            form=await request.post()
            if not check_csrf(request,tid,'close_team',form.get('csrf','')): raise web.HTTPForbidden(text='Обновите форму')
            try: await db.admin_close_team(tid,form.get('reason',''),request.app[user_key])
            except ValueError as error: raise web.HTTPBadRequest(text=str(error))
            except LookupError: raise web.HTTPNotFound(text='Команда не найдена')
            raise web.HTTPSeeOther(location=admin_url(f'/teams/{tid}'))
        members=await db.pool.fetch('''SELECT m.*,p.name,p.stack,p.username FROM team_members m JOIN participants p USING(user_id)
            WHERE team_id=$1 ORDER BY joined_at,user_id''',tid)
        def person(uid,name): return f'<a href="{base_path()}/users/{uid}">{esc(name)} · {uid}</a>'
        people=[(person(m['user_id'],m['name']),esc('@'+m['username'] if m['username'] else '—'),
            '👑 Капитан' if m['user_id']==t['captain_id'] else esc(ROLES.get(m['role'],'Участник')),esc(m['stack'])) for m in members]
        n=min(number(request.query.get('page','1')),100000)
        applications=await db.pool.fetch('''SELECT a.*,p.name,p.stack FROM team_applications a JOIN participants p ON p.user_id=a.user_id
            WHERE team_id=$1 ORDER BY a.id DESC LIMIT 30 OFFSET $2''',tid,(n-1)*30)
        applications=[(person(a['user_id'],a['name']),esc(ROLES[a['role']]),'Приглашение' if a['direction']=='invite' else 'Заявка',
            esc(STATES[a['status']]),esc(a['stack']),esc(a['message'])) for a in applications]
        nav=f'<a href="{base_path()}/teams/{tid}?page={n+1}">Следующие заявки →</a>' if len(applications)==30 else ''
        if n>1: nav+=f' <a href="{base_path()}/teams/{tid}?page={n-1}">← Предыдущие</a>'
        history=await db.pool.fetch("SELECT properties,occurred_at FROM analytics_events WHERE name='team_moderated' AND properties->>'team_id'=$1 ORDER BY occurred_at DESC LIMIT 20",str(tid))
        audit=table(['Время','Модератор','Причина'],[(esc(a['occurred_at']),esc(json.loads(a['properties']).get('moderator','')),esc(json.loads(a['properties']).get('reason',''))) for a in history]) if history else ''
        form=f'''<h2>Модерация</h2><p>Роспуск уберёт команду из поиска, освободит участников и отменит ожидающие заявки. История останется.</p>
<form method="post" class="moderation"><input type="hidden" name="csrf" value="{csrf_token(request,tid,'close_team')}">
<label>Причина <textarea name="reason" required maxlength="500"></textarea></label><button class="danger">Распустить команду</button></form>''' if t['active'] else '<p>Команда распущена.</p>'
        return page(t['name'],f'''<p><a href="{base_path()}/teams?event_id={t['event_id']}">← Команды события</a></p><h1>{esc(t['name'])}</h1>
<p><a href="{base_path()}/events/{t['event_id']}">{esc(t['title'])}</a> · {'Активна' if t['active'] else 'Распущена'} · Лимит: {t['capacity']}</p>
<p class="description">{esc(t['description'])}</p><p>Открываемые роли: {esc(', '.join(ROLES[r] for r in t['wanted_roles']))}</p>
<p>Стек: {esc(', '.join(t['tags']))} · Время: {esc(HOURS[t['hours']])}</p><p>Чат команды: {esc(t['chat_url'] or '—')}</p>
<h2>Состав</h2>{table(['Участник','Username','Роль','Стек'],people)}<h2>Заявки и приглашения</h2>{table(['Участник','Роль','Тип','Статус','Стек','Сообщение'],applications)}{nav}{form}{audit}''')

    app.add_routes([web.get('/teams',listing),web.get('/teams/{tid}',detail),web.post('/teams/{tid}',detail)])
