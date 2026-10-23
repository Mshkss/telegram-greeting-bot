"""Team menus reuse the current bot message. Drafts survive restarts."""
import logging
from aiogram import F
from aiogram.exceptions import TelegramAPIError
from app.teams import ROLES,EXPERIENCE,HOURS

log=logging.getLogger(__name__)


def register_team_handlers(router,db,render,keyboard,menu):
    async def notify(bot,uid,text,tid=None):
        try:
            await bot.send_message(uid,text,parse_mode=None,reply_markup=keyboard([[('Открыть команду',f'tm:detail:{tid}')]]) if tid else None)
        except TelegramAPIError: log.warning('team_notification_failed')

    async def show(message,uid,eid,replace=True):
        row=await db.event_record(eid)
        if not row or row['status']=='draft': raise ValueError('Событие недоступно.')
        mine=await db.my_team(eid,uid)
        rows=[[('Профиль для команды',f'tm:profile:{eid}')],
              [('Подходящие команды',f'tm:list:{eid}:1:0'),('Все команды',f'tm:list:{eid}:1:1')],
              [('Мои заявки и приглашения',f'tm:inbox:{eid}:1')]]
        rows.append([('Моя команда',f'tm:detail:{mine["id"]}')] if mine else [('Создать команду',f'tm:create:{eid}')])
        if not mine:
            seeking=await db.pool.fetchval('SELECT needs_team FROM event_registrations WHERE event_id=$1 AND user_id=$2 AND active',eid,uid)
            if seeking is not None: rows.append([('Скрыть из поиска' if seeking else 'Искать команду',f'tm:visibility:{eid}:{0 if seeking else 1}')])
        rows.append([('← К событию',f'event:{eid}'),('В меню','menu:home')])
        await render(message,f"Команды · {row['title']}\n\nСначала запишись на событие и заполни профиль для команды. Вступление происходит только по согласию обеих сторон. На один хакатон — одна команда.",replace=replace,reply_markup=keyboard(rows))

    async def event_menu(message,uid,n=1,replace=True):
        rows=await db.pool.fetch("SELECT e.id,e.title FROM events e JOIN event_registrations r ON r.event_id=e.id WHERE r.user_id=$1 AND r.active AND e.status<>'draft' ORDER BY e.starts_at DESC NULLS LAST,e.id",uid)
        pages=max(1,(len(rows)+7)//8);n=min(max(1,n),pages)
        buttons=[[(e['title'],f'tm:home:{e["id"]}')] for e in rows[(n-1)*8:n*8]]
        if n>1: buttons.append([('← Назад',f'tm:events:{n-1}')])
        if n<pages: buttons.append([('Далее →',f'tm:events:{n+1}')])
        buttons.append([('В меню','menu:home')])
        await render(message,f'Команды · выбери хакатон · {n}/{pages}'+('' if rows else '\nСначала запишись на событие.'),replace=replace,reply_markup=keyboard(buttons))

    async def detail(message,uid,tid):
        t=await db.team_detail(tid,uid)
        members='\n'.join(('👑 ' if m['user_id']==t['captain_id'] else '• ')+m['name']+' · '+ROLES.get(m['role'],'участник') for m in t['members'])
        text=f"{t['name']} · {len(t['members'])}/{t['capacity']}\n\n{t['description']}\n\nСостав:\n{members}\n\nИщем: "+(', '.join(ROLES[r] for r in t['vacancies']) or 'свободных мест нет')
        text+='\nВремя: '+HOURS[t['hours']]+'\nСтек: '+(', '.join(t['tags']) or 'не указан')
        rows=[]
        if uid==t['captain_id']:
            rows.extend([[('Заявки',f'tm:requests:{tid}:1'),('Найти участников',f'tm:people:{tid}:1')],
                         [('Изменить открытые роли',f'tm:editroles:{tid}'),('Ссылка на чат',f'tm:chat:{tid}')],
                         [('Управление участниками',f'tm:members:{tid}')], [('Распустить команду',f'tm:closeask:{tid}')]])
        elif t['is_member']: rows.append([('Выйти из команды',f'tm:leaveask:{tid}')])
        else:
            pending=await db.pool.fetchrow("SELECT id,direction FROM team_applications WHERE team_id=$1 AND user_id=$2 AND status='pending'",tid,uid)
            if pending and pending['direction']=='invite':
                rows.append([('Принять приглашение',f'tm:accept:{pending["id"]}'),('Отклонить',f'tm:reject:{pending["id"]}')])
            elif pending:
                text+='\n\nТвоя заявка ожидает решения капитана.'
                rows.append([('Отозвать заявку',f'tm:withdraw:{pending["id"]}')])
            elif not await db.my_team(t['event_id'],uid):
                roles=await db.pool.fetchval('SELECT roles FROM event_preferences WHERE event_id=$1 AND user_id=$2',t['event_id'],uid) or []
                for role in t['vacancies']:
                    if role in roles: rows.append([('Подать заявку · '+ROLES[role],f'tm:apply:{tid}:{role}')])
        if t['is_member']:
            text+='\n\nЧат команды: '+('доступен по кнопке ниже' if t['chat_url'] else 'капитан пока не добавил ссылку')
            text+='\nКонтакт капитана доступен только участникам.'
            from aiogram.types import InlineKeyboardButton
            markup=keyboard(rows+[[('← Команды',f'tm:home:{t["event_id"]}'),('В меню','menu:home')]])
            if t['chat_url']: markup.inline_keyboard.insert(0,[InlineKeyboardButton(text='Открыть чат команды',url=t['chat_url'])])
            markup.inline_keyboard.insert(0,[InlineKeyboardButton(text='Написать капитану',url=f'tg://user?id={t["captain_id"]}')])
        else: markup=keyboard(rows+[[('← Команды',f'tm:home:{t["event_id"]}'),('В меню','menu:home')]])
        await render(message,text,replace=True,reply_markup=markup)

    async def prompt(message,draft,replace=True):
        data=draft['data'];step=draft['step'];version=draft['version']
        cb=lambda value:f'tm:d:{version}:{value}'
        rows=[]
        if step in ('match_roles','team_roles','edit_roles'):
            selected=data.get('roles' if step=='match_roles' else 'wanted_roles',[])
            rows=[[(('✅ ' if r in selected else '')+label,cb(r))] for r,label in ROLES.items()]
            rows.append([('Готово',cb('done'))]);text='Выбери роли (можно несколько). В команде открывается одно место на каждую выбранную роль.'
        elif step=='match_consent':
            text='Для поиска твои имя, стек, опыт, роли и доступность будут видны участникам этого хакатона. В списке ищущих ты появишься только с отметкой «нужна команда». Возраст и фото не используются в рекомендациях. Согласен?'
            rows=[[('Согласен',cb('agree'))]]
        elif step=='match_experience': text='Опыт участия?';rows=[[(v,cb(k))] for k,v in EXPERIENCE.items()]
        elif step=='match_hours': text='Сколько времени готов уделять этому хакатону?';rows=[[(v,cb(k))] for k,v in HOURS.items()]
        else:
            text={'team_name':'Название команды: до 64 символов.',
                  'team_description':'Идея и описание команды: до 500 символов.',
                  'team_tags':'Стек, который нужен команде. Например: React, Python, Figma. До 200 символов.',
                  'team_capacity':'Максимум участников вместе с капитаном: от 2 до 10.',
                  'team_chat':'Ссылка на чат (http/https). Её видят только принятые участники. Можно пропустить.',
                  'chat_edit':'Новая ссылка на чат. «-» — убрать. До 500 символов.',
                  'application_message':'Короткое сообщение капитану: почему хочешь в команду? До 300 символов.'}[step]
            if step=='team_chat': rows=[[('Пропустить',cb('skip'))]]
        rows.append([('Отменить заполнение', 'tm:cancel')])
        await render(message,text,replace=replace,reply_markup=keyboard(rows))

    async def team_list(message,uid,eid,n,all_teams):
        items=await db.team_catalog(eid,uid,all_teams)
        pages=max(1,(len(items)+3)//4);n=max(1,min(n,pages))
        lines=[f'Команды · {n}/{pages}'];rows=[]
        for t in items[(n-1)*4:n*4]:
            lines.append(t['name']+'\n'+'; '.join(t['reasons']));rows.append([(t['name'],f'tm:detail:{t["id"]}')])
        if not items: lines.append('Пока нет подходящих команд. Открой весь список или создай свою.')
        if n>1: rows.append([('← Назад',f'tm:list:{eid}:{n-1}:{int(all_teams)}')])
        if n<pages: rows.append([('Далее →',f'tm:list:{eid}:{n+1}:{int(all_teams)}')])
        rows.append([('← Команды',f'tm:home:{eid}'),('В меню','menu:home')])
        await render(message,'\n\n'.join(lines),replace=True,reply_markup=keyboard(rows))

    async def inbox(message,uid,eid,n,tid=None):
        if tid:
            t=await db.team_detail(tid,uid)
            if t['captain_id']!=uid: raise ValueError('Заявки видит капитан.')
            rows=await db.pool.fetch('''SELECT a.*,t.name,p.name AS person,p.stack FROM team_applications a JOIN teams t ON t.id=a.team_id
                JOIN participants p ON p.user_id=a.user_id WHERE a.team_id=$1 ORDER BY a.created_at DESC''',tid)
        else:
            rows=await db.pool.fetch('''SELECT a.*,t.name,p.name AS person,p.stack FROM team_applications a JOIN teams t ON t.id=a.team_id
                JOIN participants p ON p.user_id=a.user_id WHERE t.event_id=$1 AND a.user_id=$2 ORDER BY a.created_at DESC''',eid,uid)
        pages=max(1,(len(rows)+1)//2);n=max(1,min(n,pages));text=[f'Заявки / приглашения · {n}/{pages}'];buttons=[]
        states={'pending':'Ожидает решения','accepted':'Принята','rejected':'Отклонена','withdrawn':'Отозвана','cancelled':'Отменена'}
        for a in rows[(n-1)*2:n*2]:
            text.append(f"#{a['id']} · {a['name']}\n{a['person']} · {ROLES[a['role']]}\n{states[a['status']]} · "+('Приглашение' if a['direction']=='invite' else 'Заявка')+f"\nСтек: {a['stack']}\n{a['message']}")
            if a['status']=='pending':
                decision=(tid and a['direction']=='apply') or (not tid and a['direction']=='invite')
                if decision: buttons.append([(f"Принять #{a['id']}",f'tm:accept:{a["id"]}'),(f"Отклонить #{a['id']}",f'tm:reject:{a["id"]}')])
                elif (tid and a['direction']=='invite') or (not tid and a['direction']=='apply'): buttons.append([(f"Отозвать #{a['id']}",f'tm:withdraw:{a["id"]}')])
        if not rows: text.append('Заявок пока нет.')
        prefix=f'tm:requests:{tid}' if tid else f'tm:inbox:{eid}'
        if n>1: buttons.append([('← Назад',f'{prefix}:{n-1}')])
        if n<pages: buttons.append([('Далее →',f'{prefix}:{n+1}')])
        buttons.append([('← Команда',f'tm:detail:{tid}')] if tid else [('← Команды',f'tm:home:{eid}')])
        await render(message,'\n\n'.join(text),replace=True,reply_markup=keyboard(buttons))

    @router.callback_query(F.data.startswith('tm:'))
    async def click(callback):
        await callback.answer();uid=callback.from_user.id;parts=callback.data.split(':');m=callback.message
        try:
            action=parts[1]
            if action=='cancel':
                draft=await db.team_draft(uid);await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid)
                if draft: await show(m,uid,draft['data']['eid'])
                else: await render(m,'Заполнение отменено.',replace=True,reply_markup=menu())
                return
            if action=='d':
                draft=await db.team_draft(uid)
                if not draft or draft['version']!=parts[2]: raise ValueError('Кнопка устарела. Открой раздел команд заново.')
                data=draft['data'];step=draft['step'];value=parts[3];next_step=None
                if step=='match_consent' and value=='agree': next_step='match_roles'
                elif step in ('match_roles','team_roles','edit_roles'):
                    key='roles' if step=='match_roles' else 'wanted_roles';chosen=data.setdefault(key,[])
                    if value in ROLES:
                        chosen.remove(value) if value in chosen else chosen.append(value)
                        next_step=step
                    elif value=='done':
                        if not chosen: raise ValueError('Выбери хотя бы одну роль.')
                        if step=='edit_roles':
                            await db.manage_team(data['tid'],uid,'roles',value=chosen)
                            await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid);await detail(m,uid,data['tid']);return
                        next_step='match_experience' if step=='match_roles' else 'team_tags'
                elif step=='match_experience' and value in EXPERIENCE: data['experience']=value;next_step='match_hours'
                elif step=='match_hours' and value in HOURS:
                    await db.set_matching_profile(data['eid'],uid,data['roles'],data['experience'],value)
                    await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid);await show(m,uid,data['eid']);return
                elif step=='team_chat' and value=='skip':
                    data['chat_url']='';tid=await db.create_team(data['eid'],uid,data)
                    await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid);await detail(m,uid,tid);return
                if not next_step: raise ValueError('Используй текущий шаг.')
                await prompt(m,await db.save_team_draft(uid,next_step,data));return
            positions=[2]+([3] if action in ('invite','transfer','remove','removeask') else [])
            ids=[int(parts[i]) for i in positions]
            if any(not 0<v<=2**63-1 for v in ids): raise ValueError('Неверный идентификатор.')
            ident=int(parts[2])
            if action=='events': await event_menu(m,uid,ident)
            elif action=='home': await show(m,uid,ident)
            elif action=='visibility':
                await db.set_team_visibility(ident,uid,parts[3]=='1');await show(m,uid,ident)
            elif action in ('profile','create'):
                from app.teams import eligible
                async with db.pool.acquire() as c,c.transaction(): await eligible(c,ident,uid,action=='create')
                await db.finish_participant(uid)
                step='match_consent' if action=='profile' else 'team_name'
                await prompt(m,await db.save_team_draft(uid,step,{'eid':ident}))
            elif action=='detail': await detail(m,uid,ident)
            elif action=='list': await team_list(m,uid,ident,int(parts[3]),parts[4]=='1')
            elif action=='inbox': await inbox(m,uid,ident,int(parts[3]))
            elif action=='requests':
                t=await db.team_detail(ident,uid);await inbox(m,uid,t['event_id'],int(parts[3]),ident)
            elif action in ('apply','chat','editroles'):
                t=await db.team_detail(ident,uid)
                if action!='apply' and t['captain_id']!=uid: raise ValueError('Доступно капитану.')
                data={'eid':t['event_id'],'tid':ident}
                if action=='apply':
                    if parts[3] not in t['vacancies']: raise ValueError('Роль недоступна.')
                    data['role']=parts[3]
                if action=='editroles': data['wanted_roles']=list(t['wanted_roles'])
                await prompt(m,await db.save_team_draft(uid,{'apply':'application_message','chat':'chat_edit','editroles':'edit_roles'}[action],data))
            elif action in ('accept','reject'):
                changed,recipient,tid=await db.decide_application(ident,uid,action=='accept')
                if changed: await notify(callback.bot,recipient,'Заявка / приглашение '+('принята. Состав и чат доступны в карточке команды.' if action=='accept' else 'отклонена.'),tid if action=='accept' else None)
                await detail(m,uid,tid)
            elif action=='withdraw':
                eid,tid=await db.withdraw_application(ident,uid);await inbox(m,uid,eid,1,tid)
            elif action=='people':
                people=await db.candidates(ident,uid);n=max(1,int(parts[3]));pages=max(1,(len(people)+1)//2);n=min(n,pages)
                text=[f'Участники · {n}/{pages}'];buttons=[]
                for p in people[(n-1)*2:n*2]:
                    text.append(p['name']+'\n'+p['stack']+'\n'+EXPERIENCE[p['experience']]+'\n'+'; '.join(p['reasons']))
                    for role in p['roles']: buttons.append([(f"Пригласить {p['name']} · {ROLES[role]}",f'tm:invite:{ident}:{p["user_id"]}:{role}')])
                if not people: text.append('Пока нет подходящих участников, ищущих команду.')
                if n>1: buttons.append([('← Назад',f'tm:people:{ident}:{n-1}')])
                if n<pages: buttons.append([('Далее →',f'tm:people:{ident}:{n+1}')])
                buttons.append([('← Команда',f'tm:detail:{ident}')]);await render(m,'\n\n'.join(text),replace=True,reply_markup=keyboard(buttons))
            elif action=='invite':
                aid=await db.apply_team(ident,int(parts[3]),parts[4],'Приглашение капитана',direction='invite',captain=uid)
                t=await db.team_detail(ident,uid)
                await notify(callback.bot,int(parts[3]),f"Тебя приглашают в «{t['name']}» на роль {ROLES[parts[4]]}. Решение — в «Мои заявки и приглашения».",ident)
                await detail(m,uid,ident)
            elif action=='members':
                t=await db.team_detail(ident,uid)
                if t['captain_id']!=uid: raise ValueError('Доступно капитану.')
                buttons=[]
                for p in t['members']:
                    if p['user_id']!=uid: buttons.append([(f'Передать капитанство: {p["name"]}',f'tm:transfer:{ident}:{p["user_id"]}'),('Исключить',f'tm:removeask:{ident}:{p["user_id"]}')])
                buttons.append([('← Команда',f'tm:detail:{ident}')]);await render(m,'Управление участниками',replace=True,reply_markup=keyboard(buttons))
            elif action in ('closeask','leaveask','removeask'):
                target=':'+parts[3] if len(parts)>3 else ''
                operation=action.removesuffix('ask')
                await render(m,'Подтвердить действие? При роспуске все участники снова появятся среди ищущих команду.',replace=True,
                    reply_markup=keyboard([[('Подтвердить',f'tm:{operation}:{ident}{target}'),('Отмена',f'tm:detail:{ident}')]]))
            elif action in ('close','leave','transfer','remove'):
                t=await db.team_detail(ident,uid)
                await db.manage_team(ident,uid,action,target=int(parts[3]) if len(parts)>3 else None)
                await show(m,uid,t['event_id'])
            else: raise ValueError('Кнопка недоступна.')
        except (ValueError,IndexError) as error:
            await render(m,str(error) or 'Неверная кнопка.',replace=True,reply_markup=menu())

    async def has_draft(message): return bool(await db.team_draft(message.from_user.id))

    @router.message(has_draft)
    async def fill(message):
        uid=message.from_user.id;draft=await db.team_draft(uid);data=draft['data'];step=draft['step'];text=(message.text or '').strip()
        if text=='/start': await prompt(message,draft,replace=False);return
        if text=='/cancel':
            await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid)
            await render(message,'Заполнение команды отменено.',reply_markup=menu());return
        if text.startswith('/'):
            await render(message,'Сначала закончи заполнение или /cancel.');return
        try:
            next_step={'team_name':'team_description','team_description':'team_roles','team_tags':'team_capacity','team_capacity':'team_chat'}.get(step)
            if step in ('team_name','team_description','team_tags'):
                limit={'team_name':64,'team_description':500,'team_tags':200}[step]
                if not 1<=len(text)<=limit: raise ValueError(f'Нужен текст от 1 до {limit} символов.')
                data[{'team_name':'name','team_description':'description','team_tags':'tags'}[step]]=text
            elif step=='team_capacity':
                if not text.isascii() or not text.isdigit() or len(text)>2 or not 2<=int(text)<=10: raise ValueError('Введи число от 2 до 10.')
                data['capacity']=int(text)
            elif step=='team_chat':
                data['chat_url']=text;tid=await db.create_team(data['eid'],uid,data)
                await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid)
                await render(message,'Команда создана.',reply_markup=keyboard([[('Моя команда',f'tm:detail:{tid}')]]));return
            elif step=='chat_edit':
                await db.manage_team(data['tid'],uid,'chat',value='' if text=='-' else text)
                await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid)
                await render(message,'Ссылка обновлена.',reply_markup=keyboard([[('Моя команда',f'tm:detail:{data["tid"]}')]]));return
            elif step=='application_message':
                if not 1<=len(text)<=300: raise ValueError('Сообщение: от 1 до 300 символов.')
                aid=await db.apply_team(data['tid'],uid,data['role'],text)
                t=await db.team_detail(data['tid'],uid)
                await db.pool.execute('DELETE FROM team_ui_drafts WHERE user_id=$1',uid)
                await notify(message.bot,t['captain_id'],f"Новая заявка в «{t['name']}». Открой заявки команды.",data['tid'])
                await render(message,'Заявка отправлена.',reply_markup=keyboard([[('Проверить заявки',f'tm:inbox:{data["eid"]}:1')]]));return
            else:
                await prompt(message,draft,replace=False);return
            await prompt(message,await db.save_team_draft(uid,next_step,data),replace=False)
        except ValueError as error: await render(message,str(error))

    return event_menu
