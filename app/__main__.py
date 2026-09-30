import asyncio
import logging
import os
from urllib.request import getproxies
from contextlib import asynccontextmanager, suppress

import uvicorn
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from .bot import build_dispatcher, worker
from .config import Settings
from .db import Database
from .provider import YooKassa
from .service import Service
from .web import create_app


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    settings = Settings.load()
    settings.validate()
    db = Database(settings.database_path)
    db.migrate()
    db.seed(settings.events_file)
    provider = YooKassa(settings)
    service = Service(db,settings,provider)
    proxy = os.getenv("TELEGRAM_PROXY_URL") or getproxies().get("https")
    bot = Bot(settings.bot_token, session=AiohttpSession(proxy=proxy, timeout=20))
    dp = build_dispatcher(service)

    @asynccontextmanager
    async def lifespan(app):
        me = await bot.get_me()
        settings.bot_username = me.username
        await bot.delete_webhook(drop_pending_updates=False)
        tasks = [asyncio.create_task(dp.start_polling(bot,handle_signals=False,close_bot_session=False)),
                 asyncio.create_task(worker(bot,service))]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await bot.session.close()
            await provider.close()

    app = create_app(service,lifespan)
    # Access logs would contain secret ticket URLs. Keep application events only.
    uvicorn.run(app,host=settings.host,port=settings.port,access_log=False)


if __name__ == "__main__":
    main()
