"""External URL prefix, scoped to the current request (including proxy-stripped paths)."""
import re
from contextvars import ContextVar
from aiohttp import web

_BASE = ContextVar('admin_base_path', default='')


def normalize_base_path(value):
    value = value.rstrip('/')
    if value and not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', value):
        raise ValueError('ADMIN_BASE_PATH: нужен путь вроде /admin или пустая строка')
    return value


def base_path():
    return _BASE.get()


def admin_url(path):
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Expected an internal absolute path')
    return base_path()+path


def prefix_middleware(prefix):
    @web.middleware
    async def middleware(request, handler):
        token = _BASE.set(prefix)
        try:
            return await handler(request)
        finally:
            _BASE.reset(token)
    return middleware
