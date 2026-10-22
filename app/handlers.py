"""Private-chat event registration. Legacy dating callbacks never execute here."""
import hashlib
from html import escape
from aiogram import F,Router
from aiogram.filters import Command,CommandStart
from aiogram.exceptions import TelegramAPIError,TelegramBadRequest
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup
from app.events import status_of,STATUSES


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])


def menu():
    return keyboard([[('📅 События','menu:events'),('🎟 Мои записи','menu:registrations')],
        [('👤 Мой профиль','menu:profile'),('✏️ Изменить профиль','menu:edit')],
        [('🗑 Удалить профиль','menu:delete')]])


def caption(p):
    return f"{p['name']}, {p['age']}\n\nСтек и навыки:\n{p['stack']}"


def when(value):
    from app.reports import MOSCOW
    if value is None: return 'не указано'
    return value.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')+' МСК'


async def prompt(message,draft):
    step=draft['step']; cb=lambda v:f"participant:{draft['version']}:{v}"
    texts={'consent':'Бот помогает записываться на хакатоны и другие командные события. Пока регистрация доступна с 18 лет. '
        'Имя, возраст, фото, стек и ваши записи доступны администратору; публичного каталога людей нет. '
        'Фото хранится в Telegram, у нас — file_id. Действия учитываются для аналитики. '
        'При удалении профиля записи отменяются, копия профиля остаётся в закрытом архиве администратора. '
        '/cancel — отменить заполнение.',
        'name':'Как тебя зовут? Отправь имя (до 64 символов).','age':'Сколько тебе лет? Число от 18 до 100.',
        'photo':'Пришли фотографию или возьми доступное фото своего профиля Telegram.',
        'stack':'Укажи стек, навыки и роль в команде. Например: Python, FastAPI, backend. До 500 символов.'}
    if step=='preview':
        await message.answer_photo(draft['data']['photo_file_id'],caption=caption(draft['data']),
            reply_markup=keyboard([[('✅ Сохранить',cb('save'))]]));return
    markup=None
    if step=='consent': markup=keyboard([[('Мне есть 18, продолжить',cb('agree'))]])
    if step=='photo': markup=keyboard([[('Взять фото из Telegram',cb('telegram_photo'))]])
    await message.answer(texts[step],reply_markup=markup)


def build_router(db):
    router=Router()
    router.message.filter(F.chat.type=='private',F.from_user,~F.from_user.is_bot)
    router.callback_query.filter(F.message.chat.type=='private',~F.from_user.is_bot)

    async def show_event(message,eid,uid):
        if not 0<eid<=2**63-1:
            await message.answer('Неверный идентификатор события.');return
        row=await db.event_record(eid)
        if not row or row['status']=='draft':
            await message.answer('Событие недоступно.',reply_markup=menu());return
        status=status_of(row)
        reg=await db.pool.fetchrow('SELECT * FROM event_registrations WHERE event_id=$1 AND user_id=$2 AND active',eid,uid)
        text=f"{row['title']}\n{STATUSES[status]}\nНачало: {when(row['starts_at'])}\nОкончание: {when(row['ends_at'])}\nЗапись до: {when(row['registration_until'])}\n\n{row['body']}"
        rows=[]
        if status=='open':
            rows.append([('🙋 Нужна команда',f'signup:{eid}:yes'),('👥 Команда есть',f'signup:{eid}:no')])
        if reg:
            text+='\n\nТы записан: '+('нужна команда' if reg['needs_team'] else 'команда есть')
            rows.append([('Отменить запись',f'signup:{eid}:cancel')])
        rows.append([('← События','menu:events')])
        if row.get('body_html'):
            header=f"{escape(row['title'])}\n{STATUSES[status]}\nНачало: {when(row['starts_at'])}\nОкончание: {when(row['ends_at'])}\nЗапись до: {when(row['registration_until'])}\n\n"
            footer=('\n\nТы записан: '+('нужна команда' if reg['needs_team'] else 'команда есть')) if reg else ''
            await message.answer(header+row['body_html']+footer,reply_markup=keyboard(rows),parse_mode='HTML')
        else:
            await message.answer(text,reply_markup=keyboard(rows),parse_mode=None)
        if row['source_url']: await message.answer('Сайт организатора: '+row['source_url'])
        await db.track('event_viewed',uid,{'event_id':eid})

    async def listing(message,page=1,replace=False,choose=False):
        events,page,pages=await db.event_page(page)
        lines=[f'<b>События · {page}/{pages}</b>']
        rows=[]
        for i,e in enumerate(events,1):
            title=escape(e['title'])
            if e['channel_post_url']:
                title=f'<a href="{escape(e["channel_post_url"],quote=True)}">{title}</a>'
            lines.append(f"{i}. {title}\n{STATUSES[status_of(e)]}")
            if choose: rows.append([(e['title'],f"event:{e['id']}")])
        if choose and events: lines.append('Выбери хакатон кнопкой ниже:')
        if events and not choose: rows.append([('Запись / подробнее',f'choose_events:{page}')])
        if not events: lines.append('Пока нет опубликованных событий.')
        nav=[]
        prefix='choose_events' if choose else 'events'
        if page>1: nav.append(('← Назад',f'{prefix}:{page-1}'))
        if page<pages: nav.append(('Далее →',f'{prefix}:{page+1}'))
        if nav: rows.append(nav)
        rows.append([('В меню','menu:home')])
        method=message.edit_text if replace else message.answer
        from aiogram.types import LinkPreviewOptions
        try:
            await method('\n\n'.join(lines),reply_markup=keyboard(rows),parse_mode='HTML',link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramBadRequest as error:
            if not replace or 'message is not modified' not in error.message.lower(): raise

    async def delete_version(uid):
        p=await db.participant(uid);d=await db.participant_draft(uid)
        return hashlib.sha256((str(p['updated_at'] if p else '')+str(d['version'] if d else '')).encode()).hexdigest()[:12]

    async def action(message,user,name):
        uid=user.id
        if name=='home': await message.answer('Главное меню',reply_markup=menu())
        elif name=='events': await listing(message)
        elif name=='profile':
            p=await db.participant(uid)
            if p: await message.answer_photo(p['photo_file_id'],caption=caption(p),reply_markup=menu())
            else: await message.answer('Профиль не заполнен. Нажми «Изменить профиль».',reply_markup=menu())
        elif name=='edit':
            if not (await db.settings())['registrations_open'] and not await db.participant(uid):
                await message.answer('Создание новых профилей временно приостановлено.');return
            draft=await db.save_participant_draft(uid,'consent',{'username':user.username})
            await prompt(message,draft)
        elif name=='registrations':
            rows=await db.pool.fetch('''SELECT e.*,r.needs_team FROM events e JOIN event_registrations r ON r.event_id=e.id
                WHERE r.user_id=$1 AND r.active ORDER BY e.starts_at DESC LIMIT 100''',uid)
            if not rows: await message.answer('У тебя пока нет записей.',reply_markup=menu())
            for e in rows:
                await message.answer(f"{e['title']} · {STATUSES[status_of(e)]}\n"+('Нужна команда' if e['needs_team'] else 'Команда есть'),
                    reply_markup=keyboard([[('Открыть',f"event:{e['id']}")]]))
        elif name=='delete':
            await message.answer('Удалить профиль и отменить все записи? Копия останется в закрытом архиве администратора.',
                reply_markup=keyboard([[('Да, удалить','participant_delete:'+await delete_version(uid)),('Отмена','menu:profile')]]))
        else: await message.answer('Это действие больше недоступно. Теперь бот помогает участвовать в событиях.',reply_markup=menu())

    @router.message(CommandStart())
    async def start(message):
        draft=await db.participant_draft(message.from_user.id)
        if draft: await prompt(message,draft)
        else:
            await message.answer('События и команды: выбирай хакатон, заполни профиль и запишись.',reply_markup=menu())

    @router.message(Command('events','profile','edit','registrations','delete'))
    async def command(message): await action(message,message.from_user,message.text.split()[0].split('@')[0][1:])

    @router.message(Command('cancel'))
    async def cancel(message):
        await db.finish_participant(message.from_user.id)
        await message.answer('Заполнение отменено. Сохранённый профиль не изменился.',reply_markup=menu())

    @router.callback_query()
    async def click(callback):
        parts=(callback.data or '').split(':');uid=callback.from_user.id
        await callback.answer()
        try:
            if parts[0]=='menu' and len(parts)==2: await action(callback.message,callback.from_user,parts[1])
            elif parts[0]=='events' and len(parts)==2 and parts[1].isdigit(): await listing(callback.message,max(1,min(int(parts[1]),100000)),replace=True)
            elif parts[0]=='choose_events' and len(parts)==2 and parts[1].isdigit(): await listing(callback.message,max(1,min(int(parts[1]),100000)),replace=True,choose=True)
            elif parts[0]=='event' and len(parts)==2 and parts[1].isdigit(): await show_event(callback.message,int(parts[1]),uid)
            elif parts[0]=='signup' and len(parts)==3 and parts[1].isdigit() and parts[2] in ('yes','no','cancel'):
                eid=int(parts[1])
                if not 0<eid<=2**63-1: raise ValueError('Неверное событие')
                if parts[2]!='cancel' and not await db.participant(uid):
                    await callback.message.answer('Сначала заполни профиль, затем снова открой событие.',reply_markup=keyboard([[('Заполнить профиль','menu:edit')]]));return
                await db.register_event(eid,uid,parts[2]=='yes',parts[2]=='cancel')
                await callback.message.answer('Запись отменена.' if parts[2]=='cancel' else 'Запись сохранена! Это запись в боте; требования организатора смотри в описании.')
                await show_event(callback.message,eid,uid)
            elif parts[0]=='participant_delete' and len(parts)==2:
                if parts[1]!=await delete_version(uid): raise ValueError('Подтверждение устарело. Открой удаление ещё раз.')
                await db.finish_participant(uid,delete=True)
                await callback.message.answer('Профиль удалён, записи отменены.',reply_markup=menu())
            elif parts[0]=='participant' and len(parts)==3:
                draft=await db.participant_draft(uid)
                if not draft or draft['version']!=parts[1]: raise ValueError('Кнопка устарела. Нажми /start.')
                data,step=draft['data'],draft['step']
                if step=='consent' and parts[2]=='agree': next_step='name'
                elif step=='photo' and parts[2]=='telegram_photo':
                    try: photos=await callback.bot.get_user_profile_photos(uid,limit=1)
                    except TelegramAPIError:
                        await callback.message.answer('Не удалось получить фото. Пришли его фотографией.');return
                    if not photos.photos:
                        await callback.message.answer('Фото Telegram недоступно. Пришли его фотографией.');return
                    data['photo_file_id']=photos.photos[0][-1].file_id;next_step='stack'
                elif step=='preview' and parts[2]=='save':
                    if not await db.finish_participant(uid,parts[1]): raise ValueError('Профиль уже обработан')
                    await callback.message.edit_reply_markup(reply_markup=None)
                    await callback.message.answer('Профиль сохранён. Теперь можно записываться на события.',reply_markup=menu());return
                else: raise ValueError('Используй текущий шаг заполнения.')
                draft=await db.save_participant_draft(uid,next_step,data)
                await callback.message.edit_reply_markup(reply_markup=None);await prompt(callback.message,draft)
            else: await callback.message.answer('Кнопка из старой версии. Нажми /start.',reply_markup=menu())
        except ValueError as error: await callback.message.answer(str(error),reply_markup=menu())
        except OverflowError: await callback.message.answer('Неверный идентификатор события.')

    @router.message()
    async def fill(message):
        uid=message.from_user.id;draft=await db.participant_draft(uid)
        if not draft: await message.answer('Выбери действие.',reply_markup=menu());return
        step,data=draft['step'],draft['data'];text=(message.text or '').strip()
        if text.startswith('/'): await message.answer('Команды: /events, /profile, /edit, /registrations, /cancel.');return
        if step=='name' and 1<=len(text)<=64: data['name']=text;next_step='age'
        elif step=='age' and text.isascii() and text.isdigit() and len(text)<=3 and 18<=int(text)<=100: data['age']=int(text);next_step='photo'
        elif step=='photo' and message.photo: data['photo_file_id']=message.photo[-1].file_id;next_step='stack'
        elif step=='stack' and 1<=len(text)<=500: data['stack']=text;next_step='preview'
        else:
            await db.track('validation_error',uid,{'step':step});await prompt(message,draft);return
        await prompt(message,await db.save_participant_draft(uid,next_step,data))
    return router
