"""Basic authentication, bounded throttling, and durable access audit."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
from time import monotonic
from datetime import datetime, timezone
from collections import OrderedDict
from urllib.parse import urlencode

from aiohttp import web

from app.admin_urls import base_path

log = logging.getLogger('admin.access')
OUTCOMES = {'success':'Успешная авторизация','denied':'Неверные данные',
            'challenge':'Запрос без авторизации','throttled':'Ограничение попыток'}


def trusted_networks(value):
    return tuple(ipaddress.ip_network(part.strip(),strict=False) for part in value.split(',') if part.strip())


def client_address(request, networks):
    peer = request.remote or 'unknown'
    try:
        direct = ipaddress.ip_address(peer)
        trusted = any(direct in network for network in networks)
        # Nginx must overwrite X-Real-IP, not append or pass the client's value.
        forwarded = request.headers.get('X-Real-IP')
        if trusted and forwarded:
            return str(ipaddress.ip_address(forwarded)),peer,True
    except ValueError:
        pass
    return peer,peer,False


def auth_middleware(username, password, db_key, proxies):
    expected = hashlib.sha256((username+':'+password).encode()).digest()
    networks = trusted_networks(proxies)
    failures, visits = OrderedDict(),OrderedDict()
    lock = asyncio.Lock()

    def remember(cache,key,value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache)>4096:
            cache.popitem(last=False)

    async def audit(request, outcome, ip, peer, forwarded):
        resource = request.match_info.route.resource
        # Route templates only: never queries, form contents, arbitrary URLs or credentials.
        route = resource.canonical if resource is not None else '(unknown)'
        data = dict(outcome=outcome,username=username if outcome=='success' else None,
            client_ip=ip,peer_ip=peer,forwarded=forwarded,
            user_agent=request.headers.get('User-Agent','')[:512],method=request.method,route=route)
        log.info(json.dumps({'event':'admin_auth','occurred_at':datetime.now(timezone.utc).isoformat(),**data},ensure_ascii=True))
        try:
            await request.app[db_key].pool.execute("""INSERT INTO admin_access_events
                (outcome,username,client_ip,peer_ip,forwarded,user_agent,method,route)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)""", *data.values())
        except Exception as error:
            # Keep authentication usable during an audit-store outage; stdout keeps the event.
            log.error('admin_audit_write_failed type=%s',type(error).__name__)

    @web.middleware
    async def middleware(request, handler):
        ip,peer,forwarded = client_address(request,networks)
        header = request.headers.get('Authorization','')
        try:
            scheme,encoded = header.split(' ',1)
            if scheme.lower()!='basic':
                raise ValueError
            credentials = base64.b64decode(encoded,validate=True).decode('utf-8')
            valid = hmac.compare_digest(hashlib.sha256(credentials.encode()).digest(),expected)
        except (ValueError,UnicodeError):
            valid = False
        now = monotonic()
        # Serialize the small state transition so parallel asset requests log one visit.
        async with lock:
            count,started = failures.get(ip,(0,now))
            if now-started>=60:
                count,started=0,now
            outcome = None
            if count>=10:
                status=429
                if count==10:
                    outcome='throttled'
                    remember(failures,ip,(11,started))
            elif not valid:
                status=401
                outcome='denied' if header else 'challenge'
                remember(failures,ip,(count+1,started))
            else:
                status=200
                failures.pop(ip,None)
                agent = request.headers.get('User-Agent','')[:512]
                key=(ip,hashlib.sha256(agent.encode()).digest())
                previous = visits.get(key)
                if previous is None or now-previous>=1800:
                    outcome='success'
                remember(visits,key,now)
        if outcome:
            await audit(request,outcome,ip,peer,forwarded)
        if status==429:
            response=web.Response(status=429,text='Слишком много попыток. Подождите минуту.',headers={'Retry-After':'60'})
        elif status==401:
            response=web.Response(status=401,text='Нужна авторизация администратора.',
                headers={'WWW-Authenticate':'Basic realm="Dating admin", charset="UTF-8"'})
        else:
            try:
                response=await handler(request)
            except web.HTTPException as error:
                response=web.Response(status=error.status,text=error.text,headers=error.headers)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})
        return response
    return middleware


def register_access_log(app, db_key, page):
    from app.admin_metrics import esc,table
    from app.reports import MOSCOW

    async def access_log(request):
        outcome=request.query.get('outcome','')
        if outcome and outcome not in OUTCOMES:
            raise web.HTTPBadRequest(text='Неизвестный результат авторизации.')
        try:
            number=int(request.query.get('page','1'))
            if not 1<=number<=1000000000:
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text='Некорректный номер страницы.')
        async with request.app[db_key].pool.acquire() as conn:
            async with conn.transaction(isolation='repeatable_read',readonly=True):
                total=await conn.fetchval("SELECT count(*) FROM admin_access_events WHERE $1='' OR outcome=$1",outcome)
                pages=max(1,(total+49)//50)
                number=min(number,pages)
                rows=await conn.fetch("""SELECT * FROM admin_access_events WHERE $1='' OR outcome=$1
                    ORDER BY id DESC LIMIT 50 OFFSET $2""",outcome,(number-1)*50)
        options=''.join(f'<option value="{key}" {"selected" if outcome==key else ""}>{label}</option>'
            for key,label in [('', 'Все события'),*OUTCOMES.items()])
        nav=[]
        for n,label in [(number-1,'← Назад'),(number+1,'Далее →')]:
            if 1<=n<=pages:
                nav.append(f'<a href="{base_path()}/access-log?{esc(urlencode(dict(outcome=outcome,page=n)))}">{label}</a>')
        items=[(esc(r['occurred_at'].astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M:%S')),
            esc(OUTCOMES[r['outcome']]),esc(r['username'] or '—'),esc(r['client_ip']),
            esc(r['peer_ip'])+(' · доверенный прокси' if r['forwarded'] else ' · прямое соединение'),
            esc(r['user_agent'] or '—'),esc(r['method']+' '+r['route'])) for r in rows]
        return page('Журнал входов',f'''<h1>Журнал входов</h1>
<p class="muted">Basic Auth: успешная авторизация — первый запрос с верными данными после ≥30 минут бездействия
для пары IP + браузер. После перезапуска админки или смены IP появится новая запись.
Это наблюдаемые посещения, а не точное число людей или нажатий «Войти».</p>
<form method="get"><label>Результат <select name="outcome">{options}</select></label><button>Показать</button></form>
<p>Записей: {total} · Страница {number} из {pages}</p>
{table(['Время, МСК','Результат','Администратор','IP клиента','IP соединения','Браузер (User-Agent)','Маршрут'],items)}
<nav>{' · '.join(nav)}</nav><p class="muted">Без паролей, Authorization и параметров URL.
IP клиента берётся из X-Real-IP только от явно доверенного прокси. Иначе показан IP соединения.
Ограничение попыток записывается один раз на минутное окно. История хранится в PostgreSQL без автоматической очистки.</p>''')
    app.router.add_get('/access-log',access_log)
