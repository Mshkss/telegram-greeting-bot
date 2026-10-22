"""A single Telegram-compatible, sanitized post representation for admin and delivery."""
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

TAGS={'b','i','u','s','blockquote','code','pre','tg-spoiler','a'}
ALIASES={'strong':'b','em':'i','strike':'s','del':'s'}


def safe_url(value,channel=False):
    u=urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:
        raise ValueError('Ссылка должна начинаться с http:// или https://')
    if channel and (u.scheme!='https' or u.hostname not in ('t.me','www.t.me') or not re.fullmatch(r'/(?:[A-Za-z][A-Za-z0-9_]{3,31}|c/[0-9]+)/[1-9][0-9]*/?',u.path)):
        raise ValueError('Укажите ссылку на конкретный пост: https://t.me/channel/123')
    return value


class PostParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.html=[];self.text=[];self.stack=[]
    def handle_starttag(self,tag,attrs):
        tag=ALIASES.get(tag,tag)
        if tag=='br': self.handle_data('\n');return
        if tag not in TAGS: raise ValueError('Неподдерживаемое форматирование: '+tag)
        if any(t in ('code','pre') for t in self.stack) or tag in ('code','pre') and self.stack:
            raise ValueError('Моноширинный текст должен быть без другого форматирования')
        if tag=='blockquote' and tag in self.stack: raise ValueError('Цитаты нельзя вкладывать друг в друга')
        if tag=='a':
            if tag in self.stack: raise ValueError('Ссылки нельзя вкладывать друг в друга')
            attrs=dict(attrs)
            url=safe_url(attrs.get('href',''))
            self.html.append('<a href="'+escape(url,quote=True)+'">')
        else: self.html.append('<'+tag+'>')
        self.stack.append(tag)
    def handle_endtag(self,tag):
        tag=ALIASES.get(tag,tag)
        if not self.stack or self.stack[-1]!=tag: raise ValueError('Некорректное форматирование поста')
        self.stack.pop();self.html.append('</'+tag+'>')
    def handle_data(self,data):
        self.html.append(escape(data));self.text.append(data)
    def handle_comment(self,data): raise ValueError('Комментарии в посте не поддерживаются')


def parse_post(value):
    if not isinstance(value,str) or len(value)>64000: raise ValueError('Пост слишком большой')
    p=PostParser();p.feed(value);p.close()
    if p.stack: raise ValueError('Незакрытое форматирование поста')
    text=''.join(p.text)
    # Conservative UTF-16 budget leaves room for status/date text in the bot detail view.
    if not text.strip() or len(text.encode('utf-16-le'))//2>3000:
        raise ValueError('Пост: от 1 до 3000 символов (эмодзи могут занимать два)')
    title=next(line.strip() for line in text.splitlines() if line.strip())[:120]
    return ''.join(p.html),text,title
