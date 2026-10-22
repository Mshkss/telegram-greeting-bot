"""Durable deletion of short-lived bot status notices; independent of update processing."""
import asyncio
import logging
from datetime import datetime,timedelta,timezone
from aiogram.exceptions import TelegramBadRequest,TelegramForbiddenError,TelegramRetryAfter

log=logging.getLogger(__name__)
NOTICE_SECONDS=15
SCHEMA='''CREATE TABLE bot_notices (
 chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,delete_at TIMESTAMPTZ NOT NULL,
 next_attempt_at TIMESTAMPTZ NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(chat_id,message_id)
);
CREATE INDEX bot_notices_due_idx ON bot_notices(next_attempt_at);'''


async def schedule_notice(db,message,seconds=NOTICE_SECONDS):
    # Telegram returns a Message. Lightweight handler mocks may have no sent message ID.
    message_id=getattr(message,'message_id',None)
    chat=getattr(message,'chat',None)
    if not isinstance(message_id,int) or chat is None: return
    at=datetime.now(timezone.utc)+timedelta(seconds=seconds)
    await db.pool.execute('''INSERT INTO bot_notices(chat_id,message_id,delete_at,next_attempt_at)
        VALUES($1,$2,$3,$3) ON CONFLICT DO NOTHING''',chat.id,message_id,at)


class NoticeCleaner:
    def __init__(self,db,bot):
        self.db,self.bot=db,bot
        self.stop=asyncio.Event()

    async def clean_once(self):
        rows=await self.db.pool.fetch('SELECT * FROM bot_notices WHERE next_attempt_at<=now() ORDER BY next_attempt_at LIMIT 50')
        for row in rows:
            retry=None
            try:
                await self.bot.delete_message(row['chat_id'],row['message_id'])
            except (TelegramBadRequest,TelegramForbiddenError) as error:
                # Already deleted, inaccessible or permanently undeletable.
                log.info('notice_delete_unavailable type=%s',type(error).__name__)
            except Exception as error:
                retry=error.retry_after if isinstance(error,TelegramRetryAfter) else min(3600,2**min(row['attempts']+1,12))
                log.warning('notice_delete_retry type=%s',type(error).__name__)
            if retry is not None:
                await self.db.pool.execute('''UPDATE bot_notices SET attempts=attempts+1,
                    next_attempt_at=now()+$3::double precision*interval '1 second'
                    WHERE chat_id=$1 AND message_id=$2''',row['chat_id'],row['message_id'],max(1,retry))
            else:
                await self.db.pool.execute('DELETE FROM bot_notices WHERE chat_id=$1 AND message_id=$2',row['chat_id'],row['message_id'])

    async def run(self):
        while not self.stop.is_set():
            try: await self.clean_once()
            except Exception as error: log.warning('notice_cleanup_failed type=%s',type(error).__name__)
            try: await asyncio.wait_for(self.stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass

    async def __aenter__(self):
        self.task=asyncio.create_task(self.run())
        return self

    async def __aexit__(self,*args):
        self.stop.set()
        await self.task
