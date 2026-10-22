"""Event content and registration administration. No channel publishing runs implicitly."""
import csv
import io
from datetime import datetime
from aiohttp import web
from app.admin_urls import base_path,admin_url
from app.admin_metrics import esc,table
from app.events import STATUSES,status_of
from app.reports import MOSCOW
from app.post_format import parse_post
from pathlib import Path


def stamp(v): return v.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M') if v else 'Не указано'


def register_event_admin(app,db_key,page,csrf_token,check_csrf,photo,moderation_panel):
    def number(value):
        try:
            value=int(value)
            if not 0<value<=2**63-1: raise ValueError
            return value
        except ValueError: raise web.HTTPNotFound(text='Запись не найдена')

    async def listing(request):
        try: n=max(1,min(100000,int(request.query.get('page','1'))))
        except ValueError: raise web.HTTPBadRequest(text='Некорректная страница')
        rows=await request.app[db_key].event_list(n,admin=True)
        items=[(f'<a href="{base_path()}/events/{e["id"]}">{esc(e["title"])}</a>',esc(STATUSES[status_of(e)]),esc(stamp(e['starts_at']))) for e in rows]
        nav=f'<a href="{base_path()}/events?page={n+1}">Далее →</a>' if len(rows)==10 else ''
        if n>1: nav+=f' <a href="{base_path()}/events?page={n-1}">← Назад</a>'
        return page('События',f'<h1>События</h1><a href="{base_path()}/events/new">+ Создать событие</a>'+table(['Название','Статус','Начало, МСК'],items)+nav)

    async def edit(request):
        eid=number(request.match_info['eid']) if 'eid' in request.match_info else 0
        db=request.app[db_key];e=await db.event_record(eid) if eid else None
        if eid and not e: raise web.HTTPNotFound(text='Событие не найдено')
        if request.method=='POST':
            form=await request.post()
            if not check_csrf(request,eid,'event',form.get('csrf','')): raise web.HTTPForbidden(text='Обновите форму')
            try:
                data={k:form.get(k,'').strip() for k in ('title','summary','body','status','source_url')}
                if 'body_html' in form:
                    data['body_html']=form['body_html']
                    data['source_url']=e['source_url'] if e else ''
                data['channel_post_url']=form.get('channel_post_url','').strip()
                for k in ('starts_at','ends_at','registration_until'):
                    raw=form.get(k,'')
                    data[k]=datetime.strptime(raw,'%Y-%m-%dT%H:%M').replace(tzinfo=MOSCOW) if raw else None
                eid=await db.save_event(data,eid or None,int(form.get('revision','0')))
            except (ValueError,TypeError) as error: raise web.HTTPBadRequest(text=str(error))
            raise web.HTTPSeeOther(location=admin_url(f'/events/{eid}'))
        e=dict(e) if e else {'status':'draft'}
        from html import escape
        stored=e.get('body_html')
        post=parse_post(stored)[0] if stored else escape(e.get('body',''))
        buttons=[('bold','Жирный','B'),('italic','Курсив','I'),('underline','Подчёркнутый','U'),
            ('strike','Зачёркнутый','S'),('quote','Цитата','❯'),('code','Моноширинный','</>'),
            ('spoiler','Скрытый','◧'),('link','Добавить ссылку','↗'),('date','Дата','Дата'),('clear','Очистить формат','Сброс')]
        toolbar=''.join(f'<button type="button" data-format="{key}" title="{label}" aria-label="{label}">{esc(text)}</button>' for key,label,text in buttons)
        fields=f'''<section class="post-composer"><div class="post-toolbar" role="toolbar" aria-label="Форматирование поста">{toolbar}</div>
<div class="post-editor" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Пост события" data-post-editor>{post}</div>
<input type="hidden" name="body_html" value="{esc(post)}"><p class="muted"><span data-post-count></span> · Текст поста для Telegram.</p>
<p class="muted">Выделите текст и выберите формат. ⌘/Ctrl+B, I, U, K. Вставка из буфера — обычным текстом.</p><p role="alert" data-post-error></p></section>
<details><summary>Настройки события и записи</summary>'''
        for k,label in [('registration_until','Запись до'),('starts_at','Начало'),('ends_at','Окончание')]:
            value=e[k].astimezone(MOSCOW).strftime('%Y-%m-%dT%H:%M') if e.get(k) else ''
            fields+=f'<label>{label} (МСК) <input type="datetime-local" name="{k}" value="{value}"></label><br>'
        fields+=f'<label>Ссылка на пост в канале <input type="url" name="channel_post_url" value="{esc(e.get("channel_post_url",""))}" placeholder="https://t.me/channel/123"></label><p class="muted">После подключения канала будет заполняться автоматически.</p>'
        options=''.join(f'<option value="{k}" {"selected" if e["status"]==k else ""}>{v}</option>' for k,v in STATUSES.items())
        controls=f'<input type="hidden" name="csrf" value="{csrf_token(request,eid,"event")}"><input type="hidden" name="revision" value="{e.get("revision",0)}">'
        registrations=''
        if eid:
            rows=await db.pool.fetch('''SELECT r.*,p.name,p.stack,p.username FROM event_registrations r LEFT JOIN participants p USING(user_id)
                WHERE event_id=$1 ORDER BY r.created_at DESC LIMIT 100''',eid)
            counts=await db.pool.fetchrow('SELECT count(*) FILTER(WHERE active) AS total,count(*) FILTER(WHERE active AND needs_team) AS team FROM event_registrations WHERE event_id=$1',eid)
            items=[(f'<a href="{base_path()}/users/{r["user_id"]}">{r["user_id"]}</a>',esc(r['name'] or 'Профиль удалён'),esc(r['username'] or '—'),esc(r['stack'] or '—'), 'Нужна' if r['needs_team'] else 'Есть', 'Записан' if r['active'] else 'Отменена') for r in rows]
            registrations=f'<section class="card"><h2>Записи</h2><p>Активных: {counts["total"]} · Нужна команда: {counts["team"]}</p><a href="{base_path()}/events/{eid}/registrations.csv">Все записи CSV</a><p>Последние 100 записей; весь список — в CSV.</p>'+table(['Telegram ID','Имя','Username','Стек','Команда','Статус'],items)+'</section>'
        heading=f'<label class="event-name">Название для бота <input style="display:block;width:100%;margin-top:8px" name="title" maxlength="120" value="{esc(e.get("title",""))}" required></label><p class="muted">Используется в списке и выборе хакатона. Это поле не добавляется в пост канала.</p><label>Статус <select name="status">{options}</select></label>'
        removal=f'<p><a href="{base_path()}/events/{eid}/delete">Удалить хакатон и записи о нём</a></p>' if eid else ''
        return page('Событие',f'<h1>Пост события</h1><form class="moderation" method="post">{controls}{heading}{fields}</details><button>Сохранить пост</button></form><script src="{base_path()}/post-editor.js" defer></script>'+registrations+removal)

    async def delete(request):
        eid=number(request.match_info['eid']);db=request.app[db_key]
        e=await db.event_record(eid)
        if not e: raise web.HTTPNotFound(text='Событие не найдено')
        if request.method=='POST':
            form=await request.post()
            if not check_csrf(request,eid,'delete_event',form.get('csrf','')):
                raise web.HTTPForbidden(text='Обновите подтверждение удаления')
            try: await db.delete_event(eid,int(form.get('revision','0')))
            except ValueError as error: raise web.HTTPBadRequest(text=str(error))
            except LookupError: raise web.HTTPNotFound(text='Событие не найдено')
            raise web.HTTPSeeOther(location=admin_url('/events'))
        counts=await db.pool.fetchrow('SELECT count(*) AS total,count(*) FILTER(WHERE active) AS active FROM event_registrations WHERE event_id=$1',eid)
        return page('Удаление хакатона',f'''<h1>Удалить «{esc(e['title'])}»?</h1>
<p>Будут удалены хакатон, все его записи участников ({counts['total']}, активных: {counts['active']}), версии поста и связанные события аналитики.
Профили пользователей сохранятся. Восстановить через админку нельзя. Уже опубликованный пост в Telegram-канале этим действием не удаляется.</p>
<form method="post"><input type="hidden" name="csrf" value="{csrf_token(request,eid,'delete_event')}">
<input type="hidden" name="revision" value="{e['revision']}"><button class="danger">Удалить хакатон и записи</button>
<a href="{base_path()}/events/{eid}">Отмена</a></form>''')

    async def editor_script(request):
        return web.Response(text=Path(__file__).with_name('post_editor.js').read_text(),content_type='application/javascript')


    async def export(request):
        eid=number(request.match_info['eid']);db=request.app[db_key]
        if not await db.event_record(eid): raise web.HTTPNotFound()
        out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['telegram_id','name','username','age','stack','needs_team','active','registered_at'])
        def safe(value):
            value='' if value is None else str(value)
            return "'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value
        async with db.pool.acquire() as c,c.transaction():
            async for r in c.cursor('''SELECT r.*,p.name,p.username,p.age,p.stack FROM event_registrations r
                LEFT JOIN participants p USING(user_id) WHERE event_id=$1 ORDER BY r.created_at,r.user_id''',eid):
                writer.writerow([safe(r[k]) for k in ('user_id','name','username','age','stack','needs_team','active','created_at')])
        return web.Response(body=('\ufeff'+out.getvalue()).encode(),content_type='text/csv',headers={'Content-Disposition':f'attachment; filename="event_{eid}.csv"'})

    async def participants(request):
        uid=number(request.match_info['uid']) if 'uid' in request.match_info else None
        db=request.app[db_key]
        if uid:
            p=await db.participant(uid)
            if not p: raise web.HTTPNotFound(text='Профиль не найден')
            moderation=await moderation_panel(request,uid,f'/participants/{uid}/moderation')
            return page(p['name'],f'<h1>{esc(p["name"])}, {p["age"]}</h1><img src="{base_path()}/participants/{uid}/photo" alt="Фото участника"><p>Стек: {esc(p["stack"])}</p><a href="{base_path()}/users/{uid}">История действий</a>'+moderation)
        try: n=max(1,min(100000,int(request.query.get('page','1'))))
        except ValueError: raise web.HTTPBadRequest()
        q=request.query.get('q','').strip()[:100]
        rows=await db.pool.fetch("SELECT * FROM participants WHERE $1='' OR strpos(lower(concat_ws(' ',user_id::text,name,username,stack)),lower($1))>0 ORDER BY updated_at DESC,user_id LIMIT 30 OFFSET $2",q,(n-1)*30)
        from urllib.parse import urlencode
        nav=f'<a href="{base_path()}/participants?{esc(urlencode(dict(q=q,page=n+1)))}">Далее →</a>' if len(rows)==30 else ''
        if n>1: nav+=f' <a href="{base_path()}/participants?{esc(urlencode(dict(q=q,page=n-1)))}">← Назад</a>'
        items=[(f'<a href="{base_path()}/participants/{p["user_id"]}">{esc(p["name"])}</a>',p['user_id'],p['age'],esc(p['stack'])) for p in rows]
        return page('Профили',f'<h1>Профили участников</h1><form><input name="q" value="{esc(q)}" placeholder="Имя, ID, стек"><button>Найти</button></form>'+table(['Имя','Telegram ID','Возраст','Стек'],items)+nav+f'<p><a href="{base_path()}/profiles">Прежние анкеты и архив</a> · <a href="{base_path()}/participant-archive">Архив профилей участников</a></p>')

    async def archive(request):
        rows=await request.app[db_key].pool.fetch('SELECT * FROM participant_archives ORDER BY id DESC LIMIT 100')
        import json
        items=[]
        for r in rows:
            d=json.loads(r['snapshot']);p=d.get('profile') or d.get('draft') or {}
            items.append((r['id'],r['user_id'],esc(p.get('name','—')),esc(p.get('age','—')),esc(p.get('stack','—')),esc(stamp(r['deleted_at']))))
        return page('Архив профилей', '<h1>Последние 100 удалённых профилей</h1>'+table(['Архив','ID','Имя','Возраст','Стек','Удалён, МСК'],items))

    app.add_routes([web.get('/post-editor.js',editor_script),web.get('/events',listing),web.get('/events/new',edit),web.post('/events/new',edit),
        web.get('/events/{eid}/delete',delete),web.post('/events/{eid}/delete',delete),web.get('/events/{eid}/registrations.csv',export),web.get('/events/{eid}',edit),web.post('/events/{eid}',edit),
        web.get('/participants',participants),web.get('/participants/{uid}',participants),
        web.get('/participants/{uid}/photo',photo),web.get('/participant-archive',archive)])
