"""Password-protected, read-only administration UI. Photos never go to disk."""
import asyncio
import base64
import binascii
import os
import hashlib
import hmac
import html
import io
import math
import time
from collections import OrderedDict
from urllib.parse import urlencode

from aiohttp import ClientError, web
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.db import Database
from app.domain import GOALS, GENDERS
from app.telegram import create_bot

DB = web.AppKey('db', Database)
BOT = web.AppKey('bot', object)


def esc(value):
    return html.escape(str(value or ''))


STYLE = '''
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101319;color:#edf0f7;font:16px/1.6 system-ui,sans-serif}
main{max-width:1120px;margin:40px auto;padding:0 24px}header{display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:32px;line-height:1.2}h2{margin-top:0}a{color:#b6bcff;text-decoration:none}a:hover{text-decoration:underline}
.muted{color:#a5abba}.card{background:#1a1f29;border:1px solid #303847;border-radius:16px;padding:24px;margin:20px 0}
.stats{display:flex;gap:24px}.stats strong{font-size:28px;display:block}form{display:flex;gap:12px;margin:24px 0}
input,button{border:1px solid #424c60;border-radius:10px;padding:12px 16px;font:inherit}input{background:#101319;color:white;flex:1;min-width:0}
button{background:#b6bcff;color:#11142b;cursor:pointer}table{border-collapse:collapse;width:100%;text-align:left}td,th{padding:14px 12px;border-bottom:1px solid #303847}th{color:#a5abba;font-weight:500}
.scroll{overflow:auto}.badge{display:inline-block;background:#303647;border-radius:16px;padding:2px 10px;font-size:13px;white-space:nowrap}
.profile{display:grid;grid-template-columns:minmax(200px,360px) 1fr;gap:28px}img{max-width:100%;border-radius:12px;max-height:560px;object-fit:contain}
.description{white-space:pre-wrap;overflow-wrap:anywhere}dl{display:grid;grid-template-columns:140px 1fr;gap:10px}dt{color:#a5abba}dd{margin:0;overflow-wrap:anywhere}
nav{display:flex;gap:24px;margin:24px 0}@media(max-width:650px){.profile{grid-template-columns:1fr}header{display:block}dl{grid-template-columns:1fr}main{padding:0 16px}}
'''


def page(title, content):
    return web.Response(text=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Знакомства</title>
<style>{STYLE}</style></head><body><main><header><a href="/">💘 Знакомства / Админка</a>
<span class="muted">Только для администратора</span></header>{content}</main></body></html>''', content_type='text/html')


def auth_middleware(username, password):
    expected = hashlib.sha256((username + ':' + password).encode()).digest()
    failures = OrderedDict()

    @web.middleware
    async def middleware(request, handler):
        peer, now = request.remote or 'unknown', time.monotonic()
        count, started = failures.get(peer, (0, now))
        if now - started >= 60:
            count, started = 0, now
        if count >= 10:
            response = web.Response(status=429, text='Слишком много попыток. Подождите минуту.', headers={'Retry-After': '60'})
        else:
            try:
                scheme, encoded = request.headers.get('Authorization', '').split(' ', 1)
                if scheme.lower() != 'basic':
                    raise ValueError('Unsupported authorization')
                credentials = base64.b64decode(encoded, validate=True).decode('utf-8')
                supplied = hashlib.sha256(credentials.encode()).digest()
                valid = hmac.compare_digest(supplied, expected)
            except (ValueError, UnicodeError, binascii.Error):
                valid = False
            if not valid:
                failures[peer] = (count + 1, started)
                failures.move_to_end(peer)
                while len(failures) > 1024:
                    failures.popitem(last=False)
                response = web.Response(status=401, text='Нужна авторизация администратора.',
                    headers={'WWW-Authenticate': 'Basic realm="Dating admin", charset="UTF-8"'})
            else:
                failures.pop(peer, None)
                try:
                    response = await handler(request)
                except web.HTTPException as error:
                    response = web.Response(status=error.status, text=error.text, headers=error.headers)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        })
        return response
    return middleware


async def index(request):
    query = request.query.get('q', '')[:100].strip()
    try:
        number = max(1, min(int(request.query.get('page', '1')), 100000))
    except ValueError:
        number = 1
    db = request.app[DB]
    total, profiles = await db.list_profiles(query, number)
    drafts = await db.draft_count()
    rows = ''.join(f'''<tr><td><a href="/profiles/{p['user_id']}">{esc(p['name'])}</a>
<div class="muted">{p['user_id']}</div></td><td>{esc('@'+p['username'] if p['username'] else '—')}</td>
<td>{p['age']}</td><td>{esc(GENDERS[p['gender']])}</td>
<td><span class="badge">{'В поиске' if p['active'] else 'Скрыта'}</span></td>
<td>{p['created_at'].strftime('%d.%m.%Y')}</td></tr>''' for p in profiles)
    nav = ''
    if number > 1:
        nav += f'<a href="/?{esc(urlencode(dict(q=query,page=number-1)))}">← Назад</a>'
    if number*30 < total:
        nav += f'<a href="/?{esc(urlencode(dict(q=query,page=number+1)))}">Далее →</a>'
    return page('Анкеты', f'''<h1>Анкеты участников</h1><div class="stats">
<div><strong>{total}</strong><span class="muted">{'Найдено анкет' if query else 'Сохранённых анкет'}</span></div>
<div><strong>{drafts}</strong><span class="muted">Незавершённых анкет</span></div></div>
<form method="get"><input name="q" value="{esc(query)}" placeholder="Имя, username или Telegram ID" aria-label="Поиск">
<button>Найти</button></form><div class="card scroll"><table><thead><tr><th>Участник</th><th>Telegram</th>
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
    return page(p['name'], f'''<nav><a href="/">← Все анкеты</a></nav><div class="card profile">
<div><img src="/profiles/{p['user_id']}/photo" alt="Фото анкеты"></div><div>
<h1>{esc(p['name'])}, {p['age']}</h1><span class="badge">{'В поиске' if p['active'] else 'Скрыта'}</span>
<dl><dt>Telegram ID</dt><dd>{p['user_id']}</dd><dt>Username</dt><dd>{esc('@'+p['username'] if p['username'] else 'Не задан')}</dd>
<dt>Пол</dt><dd>{esc(GENDERS[p['gender']])}</dd>
<dt>Создана, UTC</dt><dd>{p['created_at'].strftime('%d.%m.%Y %H:%M')}</dd>
<dt>Изменена, UTC</dt><dd>{p['updated_at'].strftime('%d.%m.%Y %H:%M')}</dd></dl>
<h2>О себе</h2><p class="description">{esc(p['description'])}</p><h2>Цели знакомства</h2><ul>{goals}</ul></div></div>''')


async def photo(request):
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


def create_app(settings, db=None, bot=None):
    app = web.Application(middlewares=[auth_middleware(settings.admin_user, settings.admin_password)], client_max_size=1024)

    async def resources(app):
        database = db or await Database.connect(settings)
        telegram = bot or create_bot(settings)
        app[DB], app[BOT] = database, telegram
        try:
            await database.initialize()
            yield
        finally:
            if bot is None:
                await telegram.session.close()
            if db is None:
                await database.close()
    app.cleanup_ctx.append(resources)
    app.add_routes([web.get('/', index), web.get('/profiles/{uid}', detail), web.get('/profiles/{uid}/photo', photo)])
    return app


if __name__ == '__main__':
    web.run_app(create_app(Settings.load()), host=os.getenv('ADMIN_HOST', '127.0.0.1'), port=8080, access_log=None)
