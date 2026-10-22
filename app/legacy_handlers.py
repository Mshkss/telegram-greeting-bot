import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from app.domain import GOALS, GENDERS, caption, validate_profile

log = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows])


def menu():
    return keyboard([
        [('👤 Моя анкета', 'menu:profile'), ('✏️ Заполнить / изменить', 'menu:edit')],
        [('🔎 Смотреть анкеты', 'menu:browse'), ('💞 Совпадения', 'menu:matches')],
        [('🙈 Скрыть', 'menu:hide'), ('👀 Показывать', 'menu:show')],
        [('🗑 Удалить анкету', 'menu:delete')],
    ])


async def prompt(message, draft):
    step, data, version = draft['step'], draft['data'], draft['version']
    def cb(value):
        return f'draft:{version}:{value}'
    if step == 'consent':
        await message.answer(
            'Здесь можно найти свидание, друзей или компанию. Бот для людей от 18 лет.\n\n'
            'В анкете будут видны ваше имя из Telegram, пол, возраст, фото, описание и цели. '
            'Анкету видят другие участники и администратор. Контакт в Telegram открывается '
            'при взаимном лайке. Фото хранится в Telegram; мы сохраняем его идентификатор.\n\n'
            'Продолжая, вы подтверждаете совершеннолетие и соглашаетесь на хранение данных '
            'и показ анкеты. После удаления копии анкеты и текущего черновика остаются '
            'в закрытом архиве администратора. Мы также учитываем действия в боте '
            'для статистики работы сервиса. /cancel — отменить черновик, /delete — убрать анкету из бота.',
            reply_markup=keyboard([[('Мне есть 18, продолжить', cb('agree'))]]))
    elif step == 'gender':
        await message.answer('Ты парень или девушка?', reply_markup=keyboard([
            [('👨 Парень', cb('male')), ('👩 Девушка', cb('female'))]]))
    elif step == 'age':
        await message.answer('Сколько тебе лет? Напиши число от 18 до 100.')
    elif step == 'photo':
        await message.answer('Отправь одно фото для анкеты как фотографию, не как файл.')
    elif step == 'description':
        await message.answer('Расскажи о себе: от 1 до 500 символов.')
    elif step == 'goals':
        await message.answer('Кого ищешь? Можно выбрать несколько вариантов.',
            reply_markup=goals_keyboard(data, version))
    elif step == 'preview':
        await message.answer_photo(data['photo_file_id'], caption=caption(data),
            reply_markup=keyboard([[('✅ Сохранить анкету', cb('publish'))],
                                   [('Заполнить заново', 'menu:edit')]]))


def goals_keyboard(data, version):
    selected = data.get('goals', [])
    rows = [[(('✅ ' if key in selected else '') + label, f'draft:{version}:goal_{key}')]
            for key, label in GOALS.items()]
    rows.append([('Готово →', f'draft:{version}:done')])
    return keyboard(rows)


async def show_profile(message, db, uid):
    profile = await db.profile(uid)
    if not profile:
        await message.answer('Анкеты пока нет. Нажми «Заполнить / изменить».', reply_markup=menu())
        return
    await message.answer_photo(profile['photo_file_id'], caption=caption(profile))
    if profile['blocked']:
        await message.answer('Анкета заблокирована модератором. Причина: ' + profile['moderation_reason'], reply_markup=menu())
    else:
        await message.answer('Анкета видна в поиске.' if profile['active'] else 'Анкета скрыта.', reply_markup=menu())


async def browse(message, db, uid):
    profile = await db.profile(uid)
    state = await db.moderation(uid)
    if state['blocked']:
        await message.answer('Доступ к поиску ограничен модератором. Причина: ' + state['reason'], reply_markup=menu())
        return
    if not profile or not profile['active']:
        await message.answer('Сначала заполни и включи показ своей анкеты.', reply_markup=menu())
        return
    candidate = await db.candidate(uid)
    if not candidate:
        await db.track('browse_empty', uid)
        await message.answer('Пока нет новых анкет с общими целями. Загляни позже.', reply_markup=menu())
        return
    await message.answer_photo(candidate['photo_file_id'], caption=caption(candidate),
        reply_markup=keyboard([[
            ('❤️ Нравится', f"react:{candidate['user_id']}:like"),
            ('Дальше →', f"react:{candidate['user_id']}:skip")]]))
    await db.track('profile_viewed', uid, {'target_id': candidate['user_id']})


def contact_keyboard(profile):
    url = f"tg://user?id={profile['user_id']}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='💬 Открыть профиль в Telegram', url=url)]])


async def show_matches(message, db, uid):
    state = await db.moderation(uid)
    if state['blocked']:
        await message.answer('Совпадения недоступны: ' + state['reason'], reply_markup=menu())
        return
    matches = await db.matches(uid)
    if not matches:
        await message.answer('Взаимных лайков пока нет.', reply_markup=menu())
        return
    await message.answer('Последние взаимные лайки. Доступность контакта зависит от настроек Telegram.')
    for profile in matches:
        await message.answer(f"{profile['name']}, {profile['age']}", reply_markup=contact_keyboard(profile))


def build_router(db):
    router = Router()
    router.message.filter(F.chat.type == 'private', F.from_user, ~F.from_user.is_bot)
    router.callback_query.filter(F.message.chat.type == 'private', ~F.from_user.is_bot)

    async def action(message, user, name):
        uid = user.id
        if name == 'profile':
            await show_profile(message, db, uid)
        elif name == 'edit':
            if not (await db.settings())['registrations_open'] and not await db.profile(uid):
                await db.track('registration_paused', uid)
                await message.answer('Регистрация новых анкет временно приостановлена. Загляни позже.')
                return
            state = await db.moderation(uid)
            if state['blocked']:
                await message.answer('Можно исправить анкету, но блокировку снимает администратор. Причина: ' + state['reason'])
            draft = await db.save_draft(uid, 'consent', {
                'name': user.first_name[:64], 'username': user.username, 'goals': []})
            await prompt(message, draft)
        elif name == 'browse':
            await browse(message, db, uid)
        elif name == 'matches':
            await show_matches(message, db, uid)
        elif name in ('hide', 'show'):
            if not await db.profile(uid):
                await message.answer('Сначала создай анкету.', reply_markup=menu())
                return
            await db.visibility(uid, name == 'show')
            state = await db.moderation(uid)
            text = 'Анкета скрыта.' if name == 'hide' else 'Анкета снова видна.'
            if state['blocked']:
                text = 'Блокировка модератора действует. Анкета не видна в поиске. Причина: ' + state['reason']
            await message.answer(text, reply_markup=menu())
        elif name == 'delete':
            await message.answer('Убрать анкету из бота и удалить лайки? Копии анкеты и текущего черновика останутся в закрытом архиве администратора. Они не будут видны другим участникам.',
                reply_markup=keyboard([[('Да, удалить', 'delete:confirm'), ('Отмена', 'menu:profile')]]))

    @router.message(CommandStart())
    async def start(message: Message):
        draft = await db.draft(message.from_user.id)
        if draft:
            await db.track('profile_fill_resumed', message.from_user.id)
            await message.answer('Продолжим заполнение. /cancel — отменить черновик.')
            await prompt(message, draft)
        elif await db.profile(message.from_user.id):
            await message.answer('С возвращением! Выбирай действие.', reply_markup=menu())
        else:
            await action(message, message.from_user, 'edit')

    @router.message(Command('cancel'))
    async def cancel(message: Message):
        await db.cancel(message.from_user.id)
        await message.answer('Черновик удалён. Сохранённая анкета не изменилась.', reply_markup=menu())

    @router.message(Command('profile', 'edit', 'browse', 'matches', 'hide', 'show', 'delete'))
    async def command(message: Message):
        await action(message, message.from_user, message.text.split()[0].split('@')[0][1:])

    @router.callback_query(F.data.startswith('menu:'))
    async def menu_click(callback: CallbackQuery):
        await callback.answer()
        await action(callback.message, callback.from_user, callback.data.split(':')[1])

    @router.callback_query(F.data == 'delete:confirm')
    async def delete(callback: CallbackQuery):
        await callback.answer()
        await db.delete(callback.from_user.id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer('Анкета убрана из бота, лайки удалены. Копия сохранена в закрытом архиве администратора. /start — начать заново.')

    @router.callback_query(F.data.startswith('draft:'))
    async def draft_click(callback: CallbackQuery):
        parts = callback.data.split(':')
        if len(parts) != 3:
            await callback.answer('Некорректная кнопка.')
            return
        _, version, value = parts
        uid = callback.from_user.id
        draft = await db.draft(uid)
        if not draft or draft['version'] != version:
            await db.track('stale_button', uid)
            await callback.answer('Эта кнопка устарела. Нажми /start.', show_alert=True)
            return
        step, data = draft['step'], draft['data']
        next_step = None
        if step == 'consent' and value == 'agree':
            next_step = 'gender'
        elif step == 'gender' and value in GENDERS:
            data['gender'] = value
            next_step = 'age'
        elif step == 'goals' and value.startswith('goal_') and value[5:] in GOALS:
            goal = value[5:]
            if goal in data['goals']:
                data['goals'].remove(goal)
            else:
                data['goals'].append(goal)
            draft = await db.save_draft(uid, 'goals', data)
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=goals_keyboard(data, draft['version']))
            return
        elif step == 'goals' and value == 'done':
            if not data['goals']:
                await db.track('validation_error', uid, {'step': 'goals'})
                await callback.answer('Выбери хотя бы один вариант.', show_alert=True)
                return
            validate_profile(data)
            next_step = 'preview'
        elif step == 'preview' and value == 'publish':
            saved = await db.publish(uid, version)
            await callback.answer('Сохранено!' if saved else 'Анкета уже обработана.')
            await callback.message.edit_reply_markup(reply_markup=None)
            state = await db.moderation(uid)
            text = ('Анкета сохранена, но остаётся заблокированной. Причина: ' + state['reason']
                    if state['blocked'] else 'Анкета сохранена и доступна в поиске.')
            await callback.message.answer(text, reply_markup=menu())
            return
        if next_step:
            draft = await db.save_draft(uid, next_step, data)
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)
            await prompt(callback.message, draft)
        else:
            await callback.answer('Используй кнопки текущего шага или /start.')

    @router.callback_query(F.data.startswith('react:'))
    async def reaction(callback: CallbackQuery):
        parts = callback.data.split(':')
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ('like', 'skip'):
            await callback.answer('Некорректная кнопка.')
            return
        uid, target = callback.from_user.id, int(parts[1])
        if target > 2**63-1:
            await callback.answer('Некорректная анкета.')
            return
        added, mutual = await db.react(uid, target, parts[2] == 'like')
        await callback.answer('Взаимный лайк! 💞' if mutual else ('Готово' if added else 'Анкета уже обработана или скрыта'))
        await callback.message.edit_reply_markup(reply_markup=None)
        if mutual:
            profile = await db.profile(target)
            await callback.message.answer('Вы понравились друг другу!', reply_markup=contact_keyboard(profile))
            # Persistent matches remain available even if the recipient blocked the bot.
            mine = await db.profile(uid)
            try:
                await callback.bot.send_message(target, 'У тебя взаимный лайк! 💞', reply_markup=contact_keyboard(mine))
            except TelegramAPIError:
                log.warning('Match notification could not be delivered')
        await browse(callback.message, db, uid)

    @router.message()
    async def fill(message: Message):
        uid = message.from_user.id
        draft = await db.draft(uid)
        if not draft:
            await message.answer('Выбирай действие в меню.', reply_markup=menu())
            return
        data, step = draft['data'], draft['step']
        if message.text and message.text.startswith('/'):
            await message.answer('Неизвестная команда. /start — продолжить, /cancel — отменить.')
            return
        if step == 'age':
            text = (message.text or '').strip()
            if not text.isascii() or not text.isdigit() or not 18 <= int(text) <= 100:
                await db.track('validation_error', uid, {'step': 'age'})
                await message.answer('Нужно число от 18 до 100. Бот только для совершеннолетних.')
                return
            data['age'] = int(text)
            next_step = 'photo'
        elif step == 'photo':
            if not message.photo:
                await db.track('validation_error', uid, {'step': 'photo'})
                await message.answer('Отправь фото через «Фото», не документом.')
                return
            data['photo_file_id'] = message.photo[-1].file_id
            next_step = 'description'
        elif step == 'description':
            description = (message.text or '').strip()
            if not 1 <= len(description) <= 500:
                await db.track('validation_error', uid, {'step': 'description'})
                await message.answer('Нужен текст от 1 до 500 символов.')
                return
            data['description'] = description
            next_step = 'goals'
        else:
            await prompt(message, draft)
            return
        draft = await db.save_draft(uid, next_step, data)
        await prompt(message, draft)

    return router
