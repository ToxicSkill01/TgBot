"""Toxic Skill Club — Telegram-бот бронирования ПК.

Один процесс: бот (polling) + HTTP API для WebApp + напоминания.
Запуск: python bot.py           (нужен .env, см. .env.example)
        python bot.py --no-polling  (только API, для локальной проверки)
"""

import argparse
import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import config
import handlers
from services import BookingService
from storage import Storage
from webapi import create_app

log = logging.getLogger("club")


async def on_startup(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть бронирование"),
        BotCommand(command="my", description="Мои брони"),
        BotCommand(command="status", description="Свободные ПК"),
        BotCommand(command="help", description="Помощь"),
    ])
    if config.WEBAPP_URL.startswith("https://"):
        # Кнопка «Бронь» рядом с полем ввода — WebApp всегда под рукой.
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎮 Бронь", web_app=WebAppInfo(url=handlers.webapp_url()))
        )
    me = await bot.get_me()
    log.info("Бот @%s запущен, WebApp: %s", me.username, handlers.webapp_url())


def check_config() -> None:
    if not config.BOT_TOKEN or ":" not in config.BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env (токен от @BotFather). Пример — .env.example")
    if not config.PUBLIC_URL:
        log.warning("PUBLIC_URL не задан: WebApp на Vercel не сможет достучаться до API бота")
    elif not config.PUBLIC_URL.startswith("https://"):
        log.warning("PUBLIC_URL должен начинаться с https:// — иначе Telegram не откроет WebApp")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS не задан: уведомления о бронях никому не придут")
    if config.DEV_MODE:
        log.warning("DEV_MODE включён: API пускает без Telegram. Не используйте это в работе!")


async def main(polling: bool) -> None:
    check_config()
    storage = Storage(config.DB_PATH)
    service = BookingService(storage)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = handlers.Notifier(bot, service)

    allowed_origins = {o for o in map(config.origin_of, (config.WEBAPP_URL, config.PUBLIC_URL)) if o}
    app = create_app(
        service, notifier, bot_token=config.BOT_TOKEN, dev_mode=config.DEV_MODE,
        allowed_origins=allowed_origins, index_path=config.BASE_DIR / "index.html",
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    log.info("API слушает http://%s:%s", config.WEB_HOST, config.WEB_PORT)

    reminders = asyncio.create_task(handlers.reminder_loop(service, notifier))
    try:
        if polling:
            dp = Dispatcher(service=service, notifier=notifier)
            dp.include_router(handlers.router)
            dp.startup.register(on_startup)
            await dp.start_polling(bot)
        else:
            log.info("Режим --no-polling: бот не подключается к Telegram, работает только API")
            await asyncio.Event().wait()
    finally:
        reminders.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders
        await runner.cleanup()
        await bot.session.close()
        storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-polling", action="store_true", help="запустить только API, без Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(polling=not args.no_polling))
