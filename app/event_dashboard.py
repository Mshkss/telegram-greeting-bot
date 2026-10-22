from datetime import datetime,time,timedelta
from app.admin_urls import base_path
from app.admin_metrics import card,table,esc,pct,STEP_LABELS
from app.reports import MOSCOW


async def event_dashboard(db,r):
    lo=datetime.combine(r['start'],time.min,MOSCOW);hi=datetime.combine(r['end']+timedelta(days=1),time.min,MOSCOW)
    counts=await db.pool.fetchrow('''SELECT (SELECT count(*) FROM participants) AS profiles,
        (SELECT count(*) FROM event_registrations WHERE active) AS registrations,
        (SELECT count(*) FROM event_registrations WHERE active AND needs_team) AS teams,
        (SELECT count(*) FROM events WHERE status='open' AND registration_until>now()) AS open''')
    attempts=await db.pool.fetch("SELECT * FROM analytics_attempts WHERE format='participant' AND NOT is_edit AND started_at >=$1 AND started_at<$2",lo,hi)
    funnel=[('Начал',len(attempts))]+[(label,sum(code in a['completed_steps'] for a in attempts)) for code,label in [('consent','Подтвердил 18+'),('name','Имя'),('age','Возраст'),('photo','Фото'),('stack','Стек')]]+[('Сохранил профиль',sum(a['status']=='published' for a in attempts))]
    labels={'participant_saved':'Профили сохранены','event_viewed':'Открытия событий','event_registered':'Записи и изменения ответа о команде','event_registration_cancelled':'Отмены записей','participant_deleted':'Удаления профиля'}
    events=[(esc(labels[name]),r['events'].get(name,{}).get('total',0)) for name in ('participant_saved','event_viewed','event_registered','event_registration_cancelled','participant_deleted')]
    mature=lambda key:[c for c in r['retention'] if c[key] is not None]
    retention=lambda key:pct(sum(c[key] for c in mature(key)),sum(c['users'] for c in mature(key)))
    metrics=''.join([card('Профилей сейчас',counts['profiles']),card('Открытых регистраций',counts['open']),
        card('Активных записей',counts['registrations']),card('Нужна команда',counts['teams']),
        card('DAU',r['activity']['dau']),card('WAU',r['activity']['wau']),card('MAU',r['activity']['mau']),
        card('D1 retention',retention('d1')),card('D7 retention',retention('d7'))])
    empty='<p>Начинаем собирать данные — за период нет действий.</p>' if not r['activity']['period'] else ''
    sources=[(esc(c['source']),c['starts'],c['new_users']) for c in r['sources']]
    errors=[(esc(c['at']),esc(c['user_id']),esc(c['error_type'])) for c in r['errors']]
    return f'''<h1>Дашборд событий</h1><form><label>С <input type="date" name="from" value="{r['start']}"></label><label>По <input type="date" name="to" value="{r['end']}"></label><button>Показать</button></form>
{empty}{metrics}<p class="muted">Счётчики профилей и записей — текущее состояние; активность, воронка и события — выбранный период. Дни по Москве.</p>
<nav><a href="{base_path()}/events">События</a><a href="{base_path()}/participants">Профили</a><a href="{base_path()}/users">Пользователи и история</a><a href="{base_path()}/analytics/daily.csv?from={r['start']}&amp;to={r['end']}">CSV активности</a></nav>
<section class="card"><h2>Профиль участника: воронка</h2><p>Новые попытки заполнения, начатые за период. Редактирования и прежние анкеты знакомств исключены; прогресс на текущий момент.</p>{table(['Шаг','Прошли'],funnel)}</section>
<section class="card"><h2>Действия</h2>{table(['Событие','Количество'],events)}</section>
<section class="card"><h2>Активность по дням</h2>{table(['Дата','Активные','Новые'],[(esc(c['day']),c['active'],c['new']) for c in r['daily']])}</section>
<section class="card"><h2>Источники</h2>{table(['Метка /start','Запуски','Новые пользователи'],sources)}</section>
<section class="card"><h2>Техническое состояние</h2><p>Входящих: {r['updates']['total']} · Ошибок обработки: {r['updates']['errors']} · Без завершения: {r['updates']['processing']}</p>{table(['Время','ID','Ошибка'],errors)}</section>'''
