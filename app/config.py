import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_base: str
    proxy_secret: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    admin_user: str
    admin_password: str

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / '.env')
        def required(key):
            value = os.getenv(key, '').strip()
            if not value:
                raise ValueError(f'Заполните {key} в .env')
            return value
        api_base = required('TELEGRAM_API_BASE').rstrip('/')
        url = urlsplit(api_base)
        if (url.scheme != 'https' or not url.hostname or url.username or
                url.password or url.path or url.query or url.fragment):
            raise ValueError('TELEGRAM_API_BASE: нужен HTTPS-адрес без пути')
        password = required('ADMIN_PASSWORD')
        if len(password) < 16:
            raise ValueError('ADMIN_PASSWORD должен содержать не менее 16 символов')
        return cls(required('BOT_TOKEN'), api_base, required('TELEGRAM_PROXY_SECRET'),
                   os.getenv('POSTGRES_HOST', 'localhost'), int(os.getenv('POSTGRES_PORT', '5432')),
                   os.getenv('POSTGRES_DB', 'dating'), os.getenv('POSTGRES_USER', 'dating'),
                   required('POSTGRES_PASSWORD'), os.getenv('ADMIN_USER', 'admin'), password)
