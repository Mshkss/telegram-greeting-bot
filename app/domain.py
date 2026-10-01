GOALS = {
    'dating': '💘 отношения / свидание',
    'friends': '🫂 новых друзей',
    'company': '🍻 компанию куда-нибудь сходить',
    'project': '💻 людей для проекта',
    'interests': '🎮 людей по интересам',
}
GENDERS = {'male': 'Парень', 'female': 'Девушка'}


def validate_profile(data):
    if data.get('gender') not in GENDERS:
        raise ValueError('Выберите пол кнопкой.')
    if type(data.get('age')) is not int or not 18 <= data['age'] <= 100:
        raise ValueError('Возраст должен быть от 18 до 100 лет. Бот только для совершеннолетних.')
    if not data.get('photo_file_id'):
        raise ValueError('Отправьте фотографию.')
    if not 1 <= len(data.get('description', '').strip()) <= 500:
        raise ValueError('Описание: от 1 до 500 символов.')
    goals = data.get('goals', [])
    if not goals or len(set(goals)) != len(goals) or any(g not in GOALS for g in goals):
        raise ValueError('Выберите хотя бы одну цель знакомства.')
    if not data.get('name') or len(data['name']) > 64:
        raise ValueError('Имя: от 1 до 64 символов.')


def caption(profile):
    return (f"{profile['name']}, {profile['age']}\n"
            f"{GENDERS[profile['gender']]}\n\n{profile['description']}\n\n"
            'Ищу:\n' + '\n'.join(GOALS[g] for g in profile['goals']))
