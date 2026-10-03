"""Password-protected, moderation administration UI. Photos never go to disk."""
from app.admin_urls import base_path, admin_url
import asyncio
import os
import hashlib
import hmac
import html
import io
import math
import time
import secrets
from urllib.parse import urlencode

from aiohttp import ClientError, web
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.admin_urls import normalize_base_path, prefix_middleware
from app.admin_auth import auth_middleware, register_access_log
from app.db import Database
from app.domain import GOALS, GENDERS
from app.telegram import create_bot

DB = web.AppKey('db', Database)
BOT = web.AppKey('bot', object)
CSRF_KEY = web.AppKey('csrf_key', bytes)
ADMIN_USER = web.AppKey('admin_user', str)


def esc(value):
    return html.escape(str(value or ''))


STYLE = '''
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101319;color:#edf0f7;font:16px/1.6 system-ui,sans-serif}
main{max-width:1120px;margin:40px auto;padding:0 24px}header{display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:32px;line-height:1.2}h2{margin-top:0}a{color:#b6bcff;text-decoration:none}a:hover{text-decoration:underline}
.muted{color:#a5abba}.card{background:#1a1f29;border:1px solid #303847;border-radius:16px;padding:24px;margin:20px 0}
.stats{display:flex;gap:24px}.stats strong{font-size:28px;display:block}form{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}
input,button,textarea,select{border:1px solid #424c60;border-radius:10px;padding:12px 16px;font:inherit}input,textarea,select{background:#101319;color:white;flex:1;min-width:0}
button{background:#b6bcff;color:#11142b;cursor:pointer}table{border-collapse:collapse;width:100%;text-align:left}td,th{padding:14px 12px;border-bottom:1px solid #303847}th{color:#a5abba;font-weight:500}
.scroll{overflow:auto}.badge{display:inline-block;background:#303647;border-radius:16px;padding:2px 10px;font-size:13px;white-space:nowrap}
.moderation{display:block}.moderation textarea{display:block;width:100%;margin:12px 0;min-height:90px}.danger{background:#f4a0a0}.history{font-size:14px}.profile{display:grid;grid-template-columns:minmax(200px,360px) 1fr;gap:28px}img{max-width:100%;border-radius:12px;max-height:560px;object-fit:contain}
.description{white-space:pre-wrap;overflow-wrap:anywhere}dl{display:grid;grid-template-columns:140px 1fr;gap:10px}dt{color:#a5abba}dd{margin:0;overflow-wrap:anywhere}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:20px 0}.metric{padding:18px;background:#151923;border:1px solid #303847;border-radius:12px}.metric strong{display:block;font-size:28px}.metric small{display:block;color:#a5abba;margin-top:8px}.meter{width:130px;height:9px;background:#303847;border-radius:5px}.meter i{display:block;height:100%;background:#a9b3ff;border-radius:5px}.bars{display:flex;align-items:flex-end;height:150px;gap:3px}.barcol{height:100%;flex:1;display:flex;align-items:flex-end;min-width:1px}.bar{background:#a9b3ff;width:100%;border-radius:3px 3px 0 0}details{margin:20px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}nav{display:flex;flex-wrap:wrap;gap:24px;margin:24px 0}@media(max-width:650px){.profile{grid-template-columns:1fr}header{display:block}dl{grid-template-columns:1fr}main{padding:0 16px}}
'''


def page(title, content):
    return web.Response(text=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Знакомства</title>
<style>{STYLE}</style></head><body><main><header><a href="{base_path()}/">💘 Знакомства / Админка</a>
<nav><a href="{base_path()}/dashboard">Дашборд</a><a href="{base_path()}/profiles">Анкеты</a><a href="{base_path()}/users">Пользователи</a><a href="{base_path()}/settings">Настройки</a><a href="{base_path()}/access-log">Журнал входов</a></nav></header>{content}</main></body></html>''', content_type='text/html')




async def index(request):
    query = request.query.get('q', '')[:100].strip()
    try:
        number = max(1, min(int(request.query.get('page', '1')), 100000))
    except ValueError:
        number = 1
    db = request.app[DB]
    status = request.query.get('status', 'all')
    total, profiles = await db.list_profiles(query, number, status)
    drafts = await db.draft_count()
    rows = ''.join(f'''<tr><td><a href="{base_path()}/profiles/{p['user_id']}">{esc(p['name'])}</a>
<div class="muted">{p['user_id']}</div></td><td>{esc('@'+p['username'] if p['username'] else '—')}</td>
<td>{p['age']}</td><td>{esc(GENDERS[p['gender']])}</td>
<td><span class="badge">{profile_status(p)}</span></td>
<td>{p['created_at'].strftime('%d.%m.%Y')}</td></tr>''' for p in profiles)
    nav = ''
    if number > 1:
        nav += f'<a href="{base_path()}/profiles?{esc(urlencode(dict(q=query,status=status,page=number-1)))}">← Назад</a>'
    if number*30 < total:
        nav += f'<a href="{base_path()}/profiles?{esc(urlencode(dict(q=query,status=status,page=number+1)))}">Далее →</a>'
    options = ''.join(f'<option value="{key}" {"selected" if key == status else ""}>{label}</option>' for key, label in [('all','Все анкеты'),('active','В поиске'),('hidden','Скрытые'),('blocked','Заблокированные')])
    return page('Анкеты', f'''<nav><a href="{base_path()}/profiles">Текущие анкеты</a><a href="{base_path()}/archive">Архив удалённых</a></nav><h1>Анкеты участников</h1><div class="stats">
<div><strong>{total}</strong><span class="muted">{'Найдено анкет' if query else 'Сохранённых анкет'}</span></div>
<div><strong>{drafts}</strong><span class="muted">Незавершённых анкет</span></div></div>
<form method="get"><input name="q" value="{esc(query)}" placeholder="Имя, username или Telegram ID" aria-label="Поиск">
<select name="status" aria-label="Статус">{options}</select><button>Найти</button></form><div class="card scroll"><table><thead><tr><th>Участник</th><th>Telegram</th>
<th>Возраст</th><th>Пол</th><th>Статус</th><th>Создана, UTC</th></tr></thead><tbody>
{rows or '<tr><td colspan="6">Анкет пока нет.</td></tr>'}</tbody></table></div>
<nav>{nav}</nav><p class="muted">Страница {number} из {max(1, math.ceil(total/30))}. По 30 анкет на странице.</p>''')


async def get_profile(request):
    try:
        uid = int(request.match_info['uid'])
        if not 0 < uid <= 2**63-1:
            raise ValueError
    except ValueError:
        raise web.HTTPNotFound(text='Анкета не найдена')
    profile = await request.app[DB].profile(uid)
    if not profile:
        raise web.HTTPNotFound(text='Анкета не найдена')
    return profile


async def detail(request):
    p = await get_profile(request)
    goals = ''.join(f'<li>{esc(GOALS[g])}</li>' for g in p['goals'])
    moderation = await moderation_panel(request, p['user_id'], f"/profiles/{p['user_id']}/moderation")
    return page(p['name'], f'''<nav><a href="{base_path()}/profiles">← Все анкеты</a></nav><div class="card profile">
<div><img src="{base_path()}/profiles/{p['user_id']}/photo" alt="Фото анкеты"></div><div>
<h1>{esc(p['name'])}, {p['age']}</h1><span class="badge">{profile_status(p)}</span>
<p><a href="{base_path()}/users/{p['user_id']}">Действия и попытки пользователя →</a></p>
<dl><dt>Telegram ID</dt><dd>{p['user_id']}</dd><dt>Username</dt><dd>{esc('@'+p['username'] if p['username'] else 'Не задан')}</dd>
<dt>Пол</dt><dd>{esc(GENDERS[p['gender']])}</dd>
<dt>Создана, UTC</dt><dd>{p['created_at'].strftime('%d.%m.%Y %H:%M')}</dd>
<dt>Изменена, UTC</dt><dd>{p['updated_at'].strftime('%d.%m.%Y %H:%M')}</dd></dl>
<h2>О себе</h2><p class="description">{esc(p['description'])}</p><h2>Цели знакомства</h2><ul>{goals}</ul></div></div>{moderation}''')


async def photo(request):
    if 'aid' in request.match_info:
        archive = await get_archive(request)
        p = archive['profile_snapshot'] or (archive['draft_snapshot'] or {}).get('data', {})
        if request.query.get('part') == 'draft':
            p = (archive['draft_snapshot'] or {}).get('data', {})
        if not p.get('photo_file_id'):
            raise web.HTTPNotFound(text='Фото не было загружено')
    else:
        p = await get_profile(request)
    bot = request.app[BOT]
    try:
        async with asyncio.timeout(20):
            file = await bot.get_file(p['photo_file_id'])
            if not file.file_path or (file.file_size or 0) > 10*1024*1024:
                raise web.HTTPBadGateway(text='Фото недоступно')
            # Limit memory even if Telegram reports an inaccurate file size.
            class BoundedBuffer(io.BytesIO):
                def write(self, data):
                    if self.tell() + len(data) > 10*1024*1024:
                        raise ValueError('Photo too large')
                    return super().write(data)
            buffer = BoundedBuffer()
            await bot.download_file(file.file_path, destination=buffer)
            data = buffer.getvalue()
            if not data.startswith(b'\xff\xd8\xff'):
                raise web.HTTPBadGateway(text='Неподдерживаемый формат фото')
            return web.Response(body=data, content_type='image/jpeg')
    except (TelegramAPIError, ClientError, TimeoutError, ValueError):
        raise web.HTTPBadGateway(text='Не удалось получить фото из Telegram. Повторите позже.')



def profile_status(p):
    return 'Заблокирована' if p['blocked'] else ('В поиске' if p['active'] else 'Скрыта')


def csrf_token(request, uid, action):
    expires = int(time.time()) + 3600
    signature = hmac.new(request.app[CSRF_KEY], f'{uid}:{action}:{expires}'.encode(), hashlib.sha256).hexdigest()
    return f'{expires}.{signature}'


def check_csrf(request, uid, action, token):
    try:
        expires, signature = token.split('.', 1)
        if not int(time.time()) <= int(expires) <= int(time.time()) + 3600:
            return False
        expected = hmac.new(request.app[CSRF_KEY], f'{uid}:{action}:{expires}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (ValueError, AttributeError):
        return False


async def moderation_panel(request, uid, action_url):
    db = request.app[DB]
    state = await db.moderation(uid)
    history = await db.moderation_history(uid)
    action = 'unblock' if state['blocked'] else 'block'
    title = 'Разблокировать пользователя' if state['blocked'] else 'Заблокировать пользователя'
    rows = ''.join(f"<tr><td>{event['created_at'].strftime('%d.%m.%Y %H:%M')}</td>"
        f"<td>{'Блокировка' if event['action']=='block' else 'Разблокировка'}</td>"
        f"<td>{esc(event['reason'])}</td><td>{esc(event['moderator'])}</td></tr>" for event in history)
    return f'''<section class="card"><h2>Модерация пользователя</h2>
<p><strong>{'Заблокирован' if state['blocked'] else 'Не заблокирован'}</strong></p>
<p class="description">{esc(state['reason'])}</p>
<p class="muted">Блокировка исключает пользователя из поиска, лайков и совпадений.
Удаление и новая регистрация её не снимают. Разблокировка не восстанавливает удалённую анкету.</p>
<form class="moderation" method="post" action="{esc(admin_url(action_url))}">
<input type="hidden" name="action" value="{action}">
<input type="hidden" name="csrf" value="{csrf_token(request, uid, action)}">
<label>Причина (видна пользователю при блокировке)
<textarea name="reason" maxlength="500" required placeholder="Укажите причину действия"></textarea></label>
<button class="{'danger' if action=='block' else ''}">{title}</button></form>
<h3>История действий · последние 50</h3><div class="scroll"><table class="history"><thead>
<tr><th>Дата, UTC</th><th>Действие</th><th>Причина</th><th>Администратор</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4">Действий пока нет.</td></tr>'}</tbody></table></div>
<a href="{base_path()}/archive?q={uid}">Все удаления этого пользователя →</a></section>'''


async def moderate(request):
    if 'aid' in request.match_info:
        record = await get_archive(request)
        uid = record['user_id']
        location = f"/archive/{record['id']}"
    else:
        profile = await get_profile(request)
        uid = profile['user_id']
        location = f'/profiles/{uid}'
    form = await request.post()
    action, token = form.get('action', ''), form.get('csrf', '')
    if action not in ('block', 'unblock') or not check_csrf(request, uid, action, token):
        raise web.HTTPForbidden(text='Форма устарела или неверна. Обновите страницу анкеты и повторите.')
    reason = form.get('reason', '')
    if not isinstance(reason, str):
        raise web.HTTPBadRequest(text='Укажите текст причины')
    try:
        await request.app[DB].moderate(uid, action == 'block', reason, request.app[ADMIN_USER])
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    except LookupError:
        raise web.HTTPNotFound(text='Участник не найден')
    raise web.HTTPSeeOther(location=admin_url(location))


async def get_archive(request):
    try:
        aid = int(request.match_info['aid'])
        if not 0 < aid <= 2**63-1:
            raise ValueError
    except ValueError:
        raise web.HTTPNotFound(text='Запись архива не найдена')
    record = await request.app[DB].archive(aid)
    if not record:
        raise web.HTTPNotFound(text='Запись архива не найдена')
    return record


async def archive_index(request):
    query = request.query.get('q', '')[:100].strip()
    try:
        number = max(1, min(int(request.query.get('page', '1')), 100000))
    except ValueError:
        number = 1
    total, archives = await request.app[DB].list_archives(query, number)
    rows = ''
    for record in archives:
        data = record['profile_snapshot'] or (record['draft_snapshot'] or {}).get('data', {})
        rows += f'''<tr><td><a href="{base_path()}/archive/{record['id']}">#{record['id']} · {esc(data.get('name','Без имени'))}</a>
<div class="muted">{record['user_id']}</div></td><td>{esc(data.get('username','—'))}</td>
<td>{'Анкета' if record['profile_snapshot'] else 'Черновик'}</td>
<td>{record['deleted_at'].strftime('%d.%m.%Y %H:%M')}</td></tr>'''
    nav = ''
    if number > 1:
        nav += f'<a href="{base_path()}/archive?{esc(urlencode(dict(q=query,page=number-1)))}">← Назад</a>'
    if number*30 < total:
        nav += f'<a href="{base_path()}/archive?{esc(urlencode(dict(q=query,page=number+1)))}">Далее →</a>'
    return page('Архив удалённых', f'''<h1>Архив удалённых анкет</h1>
<p class="muted">Каждое удаление — отдельная запись. В поиске бота эти анкеты не показываются.</p>
<form method="get"><input name="q" value="{esc(query)}" placeholder="Имя, username или Telegram ID" aria-label="Поиск"><button>Найти</button></form>
<p>Найдено записей: {total}</p><div class="card scroll"><table><thead><tr><th>Анкета</th><th>Username</th><th>Тип</th><th>Удалена, UTC</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4">Архив пока пуст.</td></tr>'}</tbody></table></div><nav>{nav}</nav>
<p class="muted">Страница {number} из {max(1, math.ceil(total/30))}.</p>''')


def archive_snapshot(data, photo_url, title):
    photo_html = f'<img src="{esc(admin_url(photo_url))}" alt="Фото удалённой анкеты">' if data.get('photo_file_id') else '<p>Фото не загружено</p>'
    goals = ''.join(f'<li>{esc(GOALS.get(goal,goal))}</li>' for goal in data.get('goals', []))
    return f'''<section class="card"><h2>{title}</h2><div class="profile"><div>{photo_html}</div><div>
<h2>{esc(data.get('name','Без имени'))}, {esc(data.get('age','Возраст не указан'))}</h2>
<p>{esc(GENDERS.get(data.get('gender'),'Пол не указан'))}</p><p>{esc(data.get('username',''))}</p>
<p class="description">{esc(data.get('description','Описание не заполнено'))}</p><ul>{goals}</ul>
<p class="muted">Создана: {esc(data.get('created_at','—'))}<br>Изменена: {esc(data.get('updated_at','—'))}</p>
</div></div></section>'''


async def archive_detail(request):
    record = await get_archive(request)
    aid, uid = record['id'], record['user_id']
    content = f"<h1>Удалённая анкета #{aid}</h1><p>Telegram ID: {uid} · Удалена {record['deleted_at'].strftime('%d.%m.%Y %H:%M')} UTC</p>"
    content += f'<p><a href="{base_path()}/users/{uid}">Действия и попытки пользователя →</a></p>'
    if record['profile_snapshot']:
        content += archive_snapshot(record['profile_snapshot'], f'/archive/{aid}/photo', 'Сохранённая анкета на момент удаления')
    if record['draft_snapshot']:
        draft = record['draft_snapshot']
        content += archive_snapshot(draft['data'], f'/archive/{aid}/photo?part=draft', f"Черновик · шаг {esc(draft['step'])}")
    if await request.app[DB].profile(uid):
        content += f'<p><a href="{base_path()}/profiles/{uid}">Текущая анкета пользователя →</a></p>'
    content += await moderation_panel(request, uid, f'/archive/{aid}/moderation')
    return page('Удалённая анкета', content)


def create_app(settings, db=None, bot=None):
    prefix = normalize_base_path(getattr(settings, 'admin_base_path', ''))
    app = web.Application(middlewares=[prefix_middleware(prefix),
        auth_middleware(settings.admin_user, settings.admin_password, DB,
            getattr(settings, 'admin_trusted_proxies', '127.0.0.1/32,::1/128'))], client_max_size=16384)

    async def resources(app):
        database = db or await Database.connect(settings)
        telegram = bot or create_bot(settings, database, channel='admin')
        app[DB], app[BOT] = database, telegram
        try:
            await database.initialize()
            yield
        finally:
            if bot is None:
                await telegram.session.close()
            if db is None:
                await database.close()
    app[CSRF_KEY] = secrets.token_bytes(32)
    app[ADMIN_USER] = settings.admin_user
    app.cleanup_ctx.append(resources)
    app.add_routes([web.get('/profiles', index), web.get('/profiles/{uid}', detail),
        web.get('/profiles/{uid}/photo', photo), web.post('/profiles/{uid}/moderation', moderate),
        web.get('/archive', archive_index), web.get('/archive/{aid}', archive_detail),
        web.get('/archive/{aid}/photo', photo), web.post('/archive/{aid}/moderation', moderate)])
    from app.admin_metrics import register_metrics
    register_metrics(app, DB, ADMIN_USER, page, csrf_token, check_csrf)
    register_access_log(app, DB, page)
    if prefix:
        # Both proxy_pass forms work: preserve /admin/... or strip it to /... .
        # All generated browser URLs always use the configured public prefix.
        for route in list(app.router.routes()):
            app.router.add_route(route.method, prefix+route.resource.canonical, route.handler)
        async def trailing_slash(request):
            raise web.HTTPPermanentRedirect(location=prefix+'/')
        app.router.add_get(prefix, trailing_slash)
    return app


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    web.run_app(create_app(Settings.load()), host=os.getenv('ADMIN_HOST', '127.0.0.1'), port=8080, access_log=None)
