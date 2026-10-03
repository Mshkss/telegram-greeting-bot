"""Private, read-only view of a single user's recorded journey."""
from app.admin_urls import base_path, admin_url
import json
from urllib.parse import urlencode

from aiohttp import web

from app.admin_metrics import EVENT_LABELS, STEP_LABELS, card, esc, num, table
from app.reports import MOSCOW, date_range, user_history_report

STEPS = [('consent','Подтвердил 18+'),('gender','Указал пол'),('age','Указал возраст'),
         ('photo','Добавил фото'),('description','Добавил описание'),('goals','Выбрал цели')]
STATUS = {'in_progress':'Не завершена','published':'Опубликована','cancelled':'Отменена',
          'restarted':'Начата заново','deleted':'Удалена'}
ACTIONS = {key:'/'+key for key in ('start','profile','edit','browse','matches','hide','show','delete','cancel')}
ACTIONS.update({'message':'Сообщение','photo':'Фото','draft_button':'Кнопка заполнения',
    'like':'Кнопка «Лайк»','skip':'Кнопка «Пропуск»','other_button':'Другая кнопка'})
ACTIONS.update({'menu_'+key:'Меню: '+label for key,label in {
    'profile':'моя анкета','edit':'редактирование','browse':'смотреть анкеты','matches':'совпадения',
    'hide':'скрытие','show':'возврат в поиск','delete':'удаление'}.items()})
LABELS = {**EVENT_LABELS, 'start_first':'Первый наблюдаемый /start','start_repeat':'Повторный /start',
    'profile_fill_started':'Начало заполнения','profile_step_completed':'Шаг пройден',
    'profile_viewed':'Карточка доставлена','profile_liked':'Лайк','profile_skipped':'Пропуск',
    'match_created':'Взаимная симпатия','profile_published':'Анкета опубликована',
    'profile_deleted':'Анкета удалена','draft_deleted':'Черновик удалён'}


def stamp(value):
    return esc(value.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M:%S')) if value else '—'


def user_link(value):
    # Only numeric Telegram IDs become URLs; all other values remain escaped text.
    if isinstance(value,int) and not isinstance(value,bool) and 0 < value <= 2**63-1:
        return f'<a href="{base_path()}/users/{value}"><code>{value}</code></a>'
    return esc(value) if value is not None else '—'


def event_row(row, uid):
    props = json.loads(row['properties']) if isinstance(row['properties'],str) else row['properties']
    name = row['name']
    details = []
    if name == 'user_action':
        title = 'Входящее действие'
        details.append(esc(ACTIONS.get(props.get('action'),props.get('action','—'))))
    else:
        title = LABELS.get(name,name)
    if name in ('start_first','start_repeat'):
        details.append('Источник: '+esc(props.get('source','unknown')))
    if name in ('profile_step_completed','validation_error'):
        details.append(esc(STEP_LABELS.get(props.get('step'),props.get('step','—'))))
    if name in ('profile_viewed','profile_liked','profile_skipped','match_created'):
        target = row['user_id'] if name=='match_created' and row['user_id']!=uid else props.get('target_id')
        details.append('С пользователем: '+user_link(target) if name=='match_created' else 'Анкета: '+user_link(target))
    if name == 'profile_fill_started':
        details.append('Редактирование / повторная анкета' if props.get('is_edit') else 'Первая анкета')
        if props.get('legacy'):
            details.append('Продолжение старого черновика')
    archive = props.get('archive_id')
    if name in ('profile_deleted','draft_deleted') and isinstance(archive,int):
        details.append(f'<a href="{base_path()}/archive/{archive}">Архив #{archive}</a>')
    if props.get('attempt_id'):
        details.append('Попытка: <code>'+esc(props['attempt_id'])+'</code>')
    # A match initiated by someone else must not look like this user's own session.
    session = row['session_id'] if row['user_id']==uid else None
    return (stamp(row['occurred_at']),esc(title),'<br>'.join(details) or '—',esc(session) if session else '—')


def attempt_card(attempt):
    completed = set(attempt['completed_steps'])
    steps = ['Начал заполнение']+[label for code,label in STEPS if code in completed]
    if attempt['status']=='published':
        steps.append('Опубликовал')
    progress = ' → '.join(esc(step) for step in steps)
    step = esc(STEP_LABELS.get(attempt['last_step'],attempt['last_step']))
    outcome = 'Ожидается: '+step if attempt['status']=='in_progress' else 'Последний шаг: '+step
    kind = 'Редактирование / повторная анкета' if attempt['is_edit'] else 'Первая анкета'
    if attempt['legacy']:
        kind += ' · старый черновик, история неполная'
    return f'''<details class="card" open><summary><strong>{esc(STATUS.get(attempt['status'],attempt['status']))}</strong> · {esc(kind)} · {stamp(attempt['started_at'])}</summary>
<p>{progress}</p><p>{outcome}</p>
<p class="muted">Начало отслеживания попытки: {stamp(attempt['started_at'])}<br>
Последнее изменение: {stamp(attempt['last_activity'])} · Публикация: {stamp(attempt['published_at'])}<br>
Попытка <code>{esc(attempt['id'])}</code></p></details>'''


def history_html(report):
    user = report['user']
    uid = user['user_id']
    start,end = report['start'],report['end']
    params = {'from':str(start),'to':str(end)} if start else {}
    params.update({key+'_page':report[key]['page'] for key in ('attempts','events','sessions','errors')})
    def pagination(key):
        data = report[key]
        def link(number,label):
            url = urlencode({**params,key+'_page':number})
            return f'<a href="{base_path()}/users/{uid}?{esc(url)}#{key}">{label}</a>'
        parts = [f"Всего: {data['total']} · Страница {data['page']} из {data['pages']}"]
        if data['page']>1:
            parts.insert(0,link(data['page']-1,'← Назад'))
        if data['page']<data['pages']:
            parts.append(link(data['page']+1,'Далее →'))
        return '<p>'+' · '.join(parts)+'</p>'
    links = [f'<a href="{base_path()}/users">← Пользователи</a>']
    if user['active'] is not None:
        links.append(f'<a href="{base_path()}/profiles/{uid}">Текущая анкета</a>')
    if user['has_archive']:
        links.append(f'<a href="{base_path()}/archive?q={uid}">Архив анкет</a>')
    counts = {r['name']:r['total'] for r in report['counts']}
    metrics = ''.join([card('Входящие действия',report['updates']['total']),
        card('Просмотры карточек',counts.get('profile_viewed',0)),card('Лайки',counts.get('profile_liked',0)),
        card('Пропуски',counts.get('profile_skipped',0)),card('Взаимные симпатии',counts.get('match_created',0)),
        card('Обработка p95',num(report['updates']['p95'],' мс'))])
    sessions = [(esc(r['id']),stamp(r['started_at']),stamp(r['last_activity']),
        num((r['last_activity']-r['started_at']).total_seconds(),' с'),r['actions']) for r in report['sessions']['rows']]
    errors = []
    for r in report['errors']['rows']:
        operation = ACTIONS.get(r['operation'],r['operation']) if r['kind']=='handler' else r['operation']
        details = 'Обработка входящего действия' if r['kind']=='handler' else 'Telegram API · '+esc(r['channel'] or '—')
        if r['recipient_id'] is not None:
            details += '<br>Получатель: '+user_link(r['recipient_id'])
        errors.append((stamp(r['at']),details,esc(operation),esc(r['error_type'] or '—'),
                       num(r['duration_ms'],' мс'),esc(r['session_id']) if r['session_id'] else '—'))
    attempts = ''.join(attempt_card(a) for a in report['attempts']['rows']) or '<p>Попыток заполнения за этот период нет.</p>'
    period = f'{start} — {end}' if start else 'Вся сохранённая история'
    return f'''<h1>{esc(user['display_name'] or 'Пользователь')} · <code>{uid}</code></h1>
<nav>{' · '.join(links)}</nav><p>{esc('@'+user['username']) if user['username'] else 'Username не задан'} · {'Заблокирован' if user['blocked'] else 'Не заблокирован'}</p>
<section class="card"><h2>О пользователе · за всё время</h2><dl>
<dt>Первое действие</dt><dd>{stamp(user['first_seen'])}</dd><dt>Последнее действие</dt><dd>{stamp(user['last_seen'])}</dd>
<dt>Первый /start</dt><dd>{stamp(user['first_start_at'])}</dd><dt>Запусков /start</dt><dd>{user['start_count']}</dd>
<dt>Первый источник</dt><dd>{esc(user['first_source'] or 'unknown')}</dd><dt>Последний источник</dt><dd>{esc(user['last_source'] or 'unknown')}</dd>
<dt>Первая публикация</dt><dd>{stamp(user['first_published_at'])}</dd></dl>
<p class="muted">{'Известен до установки аналитики.' if user['known_before_tracking'] else ''}
Сбор событий с {stamp(report['tracking_since'])} МСК. Имя и username — последние известные боту.</p></section>
<form method="get"><label>С <input type="date" name="from" value="{start or ''}" required></label>
<label>По <input type="date" name="to" value="{end or ''}" required></label><button>Показать период</button><a href="{base_path()}/users/{uid}">Вся история</a></form>
<h2>{esc(period)}</h2><p class="muted">Время по Москве. Тексты сообщений и фотографии в ленте не сохраняются.</p>
<nav><a href="#attempts">Попытки заполнения</a><a href="#events">Лента действий</a><a href="#sessions">Сессии</a><a href="#errors">Ошибки</a></nav>
<div class="metrics">{metrics}</div>
<section id="attempts"><h2>Попытки заполнения</h2><p class="muted">Все типы попыток, начатые в выбранный период, с прогрессом на текущий момент.
В отличие от общей воронки здесь видны также редактирования, повторные анкеты и старые черновики. Ожидаемый шаг ещё не пройден.</p>
{attempts}{pagination('attempts')}</section>
<section id="events" class="card"><h2>Лента действий</h2><p class="muted">Сначала новые события. Входящее действие и его результат — отдельные строки.
Взаимная симпатия видна обоим участникам. Просмотр означает доставку карточки.</p>
{table(['Время, МСК','Событие','Подробности','Сессия'],[event_row(r,uid) for r in report['events']['rows']])}{pagination('events')}</section>
<section id="sessions" class="card"><h2>Сессии</h2><p class="muted">Новая после ≥30 минут без действий. Показаны сессии, пересекающие период, целиком.
Длительность — между первым и последним действием; ожидание после последнего действия не включено.</p>
{table(['ID','Начало, МСК','Последнее действие, МСК','Длительность','Действий'],sessions)}{pagination('sessions')}</section>
<section id="errors" class="card"><h2>Ошибки</h2><p>Ошибок обработки: {report['updates']['errors']} · Входящих без завершения: {report['updates']['processing']}.</p>
<p class="muted">Ошибки обработчика и Telegram API показаны отдельно: один сбой может дать две строки.
Включены неуспешные отправки этому пользователю, даже если их вызвало действие другого человека.
Сохраняется только тип ошибки. Входящие без завершения выполняются сейчас или были прерваны.</p>
{table(['Время, МСК','Уровень','Операция','Тип ошибки','Время обработки','Сессия'],errors)}{pagination('errors')}</section>'''


def register_user_pages(app, db_key, page):
    async def user_detail(request):
        try:
            uid = int(request.match_info['uid'])
            if not 0 < uid <= 2**63-1:
                raise ValueError
        except ValueError:
            raise web.HTTPNotFound(text='Пользователь не найден.')
        try:
            if 'from' in request.query or 'to' in request.query:
                start,end = date_range(request.query)
            else:
                start,end = None,None
            pages = {key:int(request.query.get(key+'_page','1')) for key in ('attempts','events','sessions','errors')}
            if any(value<1 or value>1000000000 for value in pages.values()):
                raise ValueError('Некорректный номер страницы.')
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        report = await user_history_report(request.app[db_key],uid,start,end,pages)
        if report is None:
            raise web.HTTPNotFound(text='Пользователь не найден.')
        return page(f'Пользователь {uid}',history_html(report))

    app.router.add_get('/users/{uid}',user_detail)
