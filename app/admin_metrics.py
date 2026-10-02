import csv
import html
import io

from aiohttp import web

from app.reports import MOSCOW, dashboard_report, date_range


def esc(value):
    return html.escape(str(value))


def pct(n, d):
    return f'{n/d*100:.1f}%' if d else '—'


def num(value, suffix=''):
    return '—' if value is None else f'{float(value):.0f}{suffix}'


def table(headers, rows):
    head = ''.join(f'<th>{esc(h)}</th>' for h in headers)
    body = ''.join('<tr>'+''.join(f'<td>{value}</td>' for value in row)+'</tr>' for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body or f"<tr><td colspan={len(headers)}>Данных пока нет.</td></tr>"}</tbody></table></div>'


def card(label, value, help=''):
    return f'<div class="metric"><span class="muted">{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(help)}</small></div>'


STEPS = [('started','Начал заполнение'), ('consent','Подтвердил 18+'), ('gender','Указал пол'),
         ('age','Указал возраст'), ('photo','Добавил фото'), ('description','Добавил описание'),
         ('goals','Выбрал цели'), ('published','Опубликовал')]
STEP_LABELS = {'consent':'Подтверждение 18+', 'gender':'Пол', 'age':'Возраст', 'photo':'Фото',
               'description':'Описание', 'goals':'Цели', 'preview':'Предпросмотр'}
EVENT_LABELS = {
    'profile_viewed':'Доставленные карточки', 'profile_liked':'Лайки', 'profile_skipped':'Пропуски',
    'match_created':'Созданные взаимные симпатии', 'browse_empty':'Пустая выдача',
    'profile_published':'Публикации / обновления анкет', 'profile_hidden':'Скрытия', 'profile_shown':'Возврат в поиск',
    'profile_deleted':'Удаления анкет', 'draft_deleted':'Удаления черновиков',
    'profile_fill_cancelled':'Отмена заполнения', 'profile_fill_restarted':'Перезапуск заполнения',
    'profile_fill_resumed':'Продолжение черновика через /start', 'validation_error':'Ошибки заполнения',
    'stale_button':'Нажатия устаревших кнопок', 'registration_paused':'Попытки регистрации во время паузы',
    'moderation_block':'Блокировки', 'moderation_unblock':'Разблокировки',
}


def dashboard_html(r):
    events = r['events']
    count = lambda name: events.get(name, {}).get('total', 0)
    active, sessions, funnel = r['activity'], r['sessions'], r['funnel']
    first, repeat = count('start_first'), count('start_repeat')
    mature = lambda key: [row for row in r['retention'] if row[key] is not None]
    retained = lambda key: sum(row[key] for row in mature(key))
    eligible = lambda key: sum(row['users'] for row in mature(key))
    kpis = ''.join([
        card('Активные за период', active['period'], 'Хотя бы одно сообщение или нажатие'),
        card('Новые пользователи', r['users']['new'], 'Первое наблюдаемое действие; без известных старых'),
        card('DAU', active['dau'], f"За {r['end']} по Москве"),
        card('WAU', active['wau'], '7 календарных дней до выбранной даты'),
        card('MAU', active['mau'], '30 календарных дней до выбранной даты'),
        card('Сессии', sessions['total'], 'Новая после ≥30 минут без действий'),
        card('D1 retention', pct(retained('d1'), eligible('d1')), f"{retained('d1')} / {eligible('d1')} в зрелых когортах"),
        card('D7 retention', pct(retained('d7'), eligible('d7')), f"{retained('d7')} / {eligible('d7')} в зрелых когортах"),
    ])
    day_rows = [(esc(row['day']), row['active'], row['new']) for row in r['daily']]
    max_active = max([row['active'] for row in r['daily']] + [1])
    bars = ''.join(f'<div class="barcol" title="{row["day"]}: {row["active"]} активных"><div class="bar" style="height:{row["active"]/max_active*100:.2f}%"></div></div>' for row in r['daily'])
    funnel_rows, previous = [], funnel['started']
    for key, label in STEPS:
        n = funnel[key]
        percent = n/funnel['started']*100 if funnel['started'] else 0
        funnel_rows.append((esc(label), n, '—' if key=='started' else max(0,previous-n),
                            pct(n,funnel['started']), f'<div class="meter"><i style="width:{percent:.2f}%"></i></div>'))
        previous = n
    source_rows = [(esc(row['source']),row['starts'],row['visitors'],row['new_users']) for row in r['sources']]
    cohort_rows = [(esc(row['day']), row['users'],
        'Ещё рано' if row['d1'] is None else f'{row["d1"]}/{row["users"]} · {pct(row["d1"],row["users"])}',
        'Ещё рано' if row['d7'] is None else f'{row["d7"]}/{row["users"]} · {pct(row["d7"],row["users"])}') for row in r['retention']]
    event_rows = [(esc(label), count(name), events.get(name,{}).get('users',0)) for name,label in EVENT_LABELS.items()]
    action_rows = [(esc(row['action']),row['total'],row['errors'],num(row['p95'],' мс')) for row in r['action_times']]
    telegram_rows = [(esc(row['channel']),esc(row['method']),row['total'],row['failures'],num(row['p95'],' мс')) for row in r['telegram']]
    errors = [(esc(row['at'].astimezone(MOSCOW).strftime('%d.%m %H:%M:%S')),esc(row['user_id'] or '—'),esc(row['kind']),esc(row['error_type'])) for row in r['errors']]
    tracking = r['tracking_since'].astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')
    empty_note = '<div class="card"><h2>Начинаем собирать данные</h2><p>За выбранный период ещё нет действий. Отправьте боту /start или нажмите кнопку после установки обновления.</p></div>' if not active['period'] else ''
    controls = f'<form method="get"><label>С <input type="date" name="from" value="{r["start"]}" required></label><label>По <input type="date" name="to" value="{r["end"]}" max="{r["today"]}" required></label><button>Показать</button></form>'
    return f'''<h1>Дашборд</h1><p class="muted">Дни по Москве · сбор с {tracking} МСК · даты включительно</p>
{controls}{empty_note}<div class="metrics">{kpis}</div>
<section class="card"><h2>Активность по дням</h2><div class="bars" role="img" aria-label="Активные пользователи по дням, точные значения в таблице ниже">{bars}</div>
<p class="muted">{r['start']} → {r['end']}. Открытие чата и чтение сообщений без действия не наблюдаются.</p>
<details><summary>Точные значения и новые пользователи</summary>{table(['Дата','Активные','Новые'],day_rows)}</details>
<a href="/analytics/daily.csv?from={r['start']}&to={r['end']}">Скачать CSV по дням</a></section>
<section class="card"><h2>Привлечение</h2><div class="metrics">
{card('Первый наблюдаемый /start',first,'Включая ранее известных пользователей')}
{card('Повторные /start',repeat,'От второго отслеженного запуска')}
{card('Всего наблюдаемых пользователей',r['users']['observed'],'За всё время сбора')}
{card('Известны до обновления',r['users']['legacy'],'Не считаются новыми когортами')}
</div>{table(['Источник','Запуски','Уникальные запустившие','Новые: первый источник'],source_rows)}
<p class="muted">Источник запуска — параметр /start. direct — без метки, invalid — некорректная метка, unknown — /start ещё не наблюдался.
«Новые» атрибутируются первому наблюдаемому /start; повторная ссылка не переписывает первый источник. Это доставленные команды, не клики по ссылке.</p></section>
<section class="card"><h2>Воронка первой анкеты</h2><p class="muted">Попытки, начатые в выбранный период, и их прогресс на текущий момент.
Редактирования, повторная регистрация после удаления и старые черновики исключены. Один человек может начать несколько попыток.</p>
{table(['Шаг','Прошли','Не дошли с прошлого шага','От начала','Прогресс'],funnel_rows)}
<p>Без действия ≥24 часов: <strong>{funnel['stalled']}</strong>. Это незавершённые попытки, а не доказанный уход.</p>
{table(['На каком шаге остановились ≥24 ч','Попыток'],[(esc(STEP_LABELS.get(row['last_step'],row['last_step'])),row['n']) for row in r['dropoffs']])}
<p class="muted">Отменены: {funnel['cancelled']} · Начаты заново: {funnel['restarted']} · Удалены: {funnel['deleted']}.</p></section>
<section class="card"><h2>Действия и результат</h2><div class="metrics">
{card('Доля лайков среди оценок',pct(count('profile_liked'),count('profile_liked')+count('profile_skipped')))}
{card('Доля пустых выдач',pct(count('browse_empty'),count('browse_empty')+count('profile_viewed')))}
{card('Активные с взаимной симпатией',pct(r['matched_active_users'],active['period']),'Оба участника пары, активные в периоде')}
{card('До первой публикации · медиана',num(r['activation_seconds'],' с'),'Новые пользователи, опубликовавшие до конца периода')}
</div>{table(['Событие','Количество','Уникальные инициаторы'],event_rows)}
<p class="muted">Карточка засчитывается после успешной отправки Telegram — факт прочтения неизвестен.
При появлении взаимной симпатии событие одно, инициатор — автор второго лайка. После удаления и новой регистрации та же пара может создать новое совпадение.
Повторное нажатие без изменения состояния не создаёт лайк, публикацию или скрытие.</p></section>
<section class="card"><h2>Возвращаемость по когортам</h2>
<p class="muted">Когорта — дата первого наблюдаемого действия нового пользователя. D1 / D7 — действие ровно на следующий / седьмой день.
Незавершившийся день в процент не включён. Ранее известные пользователи исключены.</p>
{table(['Первый день','Пользователей','D1','D7'],cohort_rows)}</section>
<section class="card"><h2>Сессии</h2><div class="metrics">
{card('Действий за сессию',f"{float(sessions['avg_actions']):.1f}")}
{card('Средняя наблюдаемая длина',num(sessions['avg_seconds'],' с'),'Между первым и последним действием, без хвоста ожидания')}
{card('С одним действием',sessions['single_action'],'Не равно отказу или непрочтению')}
{card('Анкеты в поиске сейчас',r['profile_counts']['searchable'],f"Всего текущих анкет: {r['profile_counts']['total']}")}
</div></section>
<section class="card"><h2>Техническое состояние</h2><div class="metrics">
{card('Обработано входящих',r['updates']['total'])}
{card('Ошибки обработки',r['updates']['errors'],pct(r['updates']['errors'],r['updates']['total']))}
{card('Время обработки p50 / p95',num(r['updates']['p50'])+' / '+num(r['updates']['p95'])+' мс','Включает БД и отправку ответов, без ожидания polling')}
{card('Без завершения обработки',r['updates']['processing'],'Выполняются сейчас или процесс был прерван')}
</div>{table(['Действие','Входящих','Ошибок','p95'],action_rows)}
<h3>Вызовы Telegram API</h3>{table(['Канал','Метод','Попыток','Неуспешно','p95'],telegram_rows)}
<p class="muted">sendMessage / sendPhoto — отправка сообщений. Ошибки getUpdates — polling; успешные ожидания polling не сохраняются.
Отправка получателю не делает его активным. Ошибка API и вызванная ею ошибка обработчика — разные уровни, их нельзя складывать.</p>
<h3>Последние ошибки в периоде</h3>{table(['Время, МСК','Пользователь','Операция','Тип ошибки'],errors)}
<p class="muted">Сохраняется тип ошибки, без текста сообщений, секретов и URL. При недоступной БД или падении процесса полной гарантии записи нет; проверяйте также логи контейнеров.</p></section>'''


def register_metrics(app, db_key, user_key, page, csrf_token, check_csrf):
    async def dashboard(request):
        try:
            start, end = date_range(request.query)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        report = await dashboard_report(request.app[db_key], start, end)
        return page('Дашборд', dashboard_html(report))

    async def daily_csv(request):
        try:
            start, end = date_range(request.query)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        report = await dashboard_report(request.app[db_key], start, end)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['date_moscow','active_users','new_users'])
        for row in report['daily']:
            writer.writerow([row['day'].isoformat(),row['active'],row['new']])
        return web.Response(body=('\ufeff'+output.getvalue()).encode(), content_type='text/csv',
            headers={'Content-Disposition': 'attachment; filename="daily_metrics.csv"'})

    async def settings_page(request):
        settings = await request.app[db_key].settings()
        checked = 'checked' if settings['registrations_open'] else ''
        return page('Настройки', f'''<h1>Настройки</h1><section class="card"><h2>Приём новых анкет</h2>
<form method="post" class="moderation"><input type="hidden" name="csrf" value="{csrf_token(request,0,'settings')}">
<label><input type="checkbox" name="registrations_open" value="yes" {checked}> Разрешить новые анкеты</label>
<p class="muted">Пауза запрещает начинать новые анкеты. Уже начатые черновики можно закончить, существующие анкеты — редактировать.</p>
<button>Сохранить</button></form><p class="muted">Изменено: {esc(settings['updated_at'].astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M'))} МСК · {esc(settings['updated_by'])}</p></section>
<section class="card"><h2>Правила аналитики</h2><dl><dt>Часовой пояс</dt><dd>Москва · UTC+3</dd>
<dt>Новая сессия</dt><dd>После 30 минут без входящего действия (граница включительно)</dd>
<dt>Активность</dt><dd>Сообщение или кнопка в личном чате; действия ботов и групп исключены</dd>
<dt>Возвращаемость</dt><dd>Ровно день 1 / день 7 от первого действия; только завершившиеся дни</dd>
<dt>Воронка</dt><dd>Попытки создания первой анкеты; редактирования исключены</dd>
<dt>Источники</dt><dd>Первый наблюдаемый /start и метка каждого последующего запуска</dd>
<dt>История</dt><dd>Начинается с обновления; автоматической очистки событий нет</dd></dl>
<p class="muted">Эти правила зафиксированы для сопоставимости истории. Тестовые действия и действия владельца бота также учитываются.</p></section>
<section class="card"><h2>Ссылки для привлечения</h2><p>Размещайте разные ссылки для разных каналов:</p>
<pre>https://t.me/ИМЯ_БОТА?start=itmo_chat\nhttps://t.me/ИМЯ_БОТА?start=poster_october</pre>
<p>Замените ИМЯ_БОТА своим username. Метка: 1–64 символа A–Z, a–z, 0–9, _ и -. Не используйте метки direct, invalid и unknown — они служебные.</p>
<p class="muted">Само открытие ссылки без доставленной команды /start не наблюдается.</p></section>''')

    async def save_settings(request):
        form = await request.post()
        if not check_csrf(request,0,'settings',form.get('csrf','')):
            raise web.HTTPForbidden(text='Обновите страницу настроек и повторите.')
        await request.app[db_key].update_settings(form.get('registrations_open')=='yes', request.app[user_key])
        raise web.HTTPSeeOther(location='/settings')

    app.add_routes([web.get('/',dashboard),web.get('/dashboard',dashboard),web.get('/analytics/daily.csv',daily_csv),
                    web.get('/settings',settings_page),web.post('/settings',save_settings)])
