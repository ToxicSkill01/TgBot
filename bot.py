"""Toxic Skill Club — Telegram-бот бронирования ПК.

Один процесс: бот (polling) + HTTP API для WebApp + туннель + напоминания.
Запуск: python bot.py              (первый запуск сам спросит токен и всё настроит)
        python bot.py --setup      (пройти настройку заново)
        python bot.py --no-polling (только API, для локальной проверки)
"""

import argparse
import asyncio
import contextlib
import importlib
import logging
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import config
import handlers
import tunnel
from services import BookingService
from storage import Storage
from webapi import create_app

log = logging.getLogger("club")
LINE = "═" * 62
PAGE_MARKER = 'name="tsc-app"'  # метка новой версии index.html


def check_config() -> None:
    if not config.BOT_TOKEN or ":" not in config.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Запустите `python bot.py --setup` или заполните .env (см. .env.example)")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS не задан: уведомления о бронях никому не придут")
    if config.DEV_MODE:
        log.warning("DEV_MODE включён: API пускает без Telegram. Не используйте это в работе!")


async def update_menu_button(bot: Bot) -> None:
    """Кнопка «🎮 Бронь» рядом с полем ввода — всегда с актуальным адресом."""
    if handlers.app_keyboard() is None:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎮 Бронь", web_app=WebAppInfo(url=handlers.webapp_url()))
        )
    except TelegramAPIError as exc:
        log.warning("Не удалось обновить кнопку меню: %s", exc)


async def open_tunnel(bot: Bot, notifier: handlers.Notifier, notes: list[str]) -> tunnel.TunnelKeeper | None:
    if config.PUBLIC_URL or config.TUNNEL == "off":
        return None
    provider = tunnel.resolve_provider()

    lost_reported = False

    async def on_lost() -> None:
        nonlocal lost_reported
        if not lost_reported:
            lost_reported = True
            await notifier.to_admins("⚠️ Пропала связь с интернетом у бота — переподключаюсь…")

    async def on_restored(url: str) -> None:
        nonlocal lost_reported
        config.PUBLIC_URL = url
        await update_menu_button(bot)
        async with aiohttp.ClientSession() as session:
            if await check_public_access(session) and lost_reported:
                lost_reported = False
                await notifier.to_admins("🟢 Связь восстановлена, кнопка «🎮 Бронь» обновлена.", handlers.app_keyboard())

    log.info("Открываю доступ из интернета через %s…", provider)
    keeper = tunnel.TunnelKeeper(provider, config.WEB_PORT, on_lost=on_lost, on_restored=on_restored)
    try:
        config.PUBLIC_URL = await keeper.start()
    except (tunnel.TunnelError, OSError) as exc:
        log.error("Не удалось открыть туннель: %s", exc)
        notes.append(f"⚠️ Туннель {provider} не запустился — клиенты не смогут открыть бронирование")
        return None
    log.info("Публичный адрес: %s", config.PUBLIC_URL)
    return keeper


async def check_public_access(session: aiohttp.ClientSession, notes: list[str] | None = None) -> bool:
    """Проверяет, что API доступно снаружи (новому адресу туннеля нужно несколько секунд)."""
    if not config.PUBLIC_URL:
        return False
    for _ in range(12):
        with contextlib.suppress(aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
            async with session.get(f"{config.PUBLIC_URL}/api/health", timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    log.info("Проверка доступа из интернета: OK")
                    return True
        await asyncio.sleep(3)
    log.warning("API не отвечает по адресу %s — проверьте туннель/интернет", config.PUBLIC_URL)
    if notes is not None:
        notes.append("⚠️ Бот не смог достучаться до себя из интернета — бронирование может не открываться")
    return False


async def choose_webapp_page(session: aiohttp.ClientSession, notes: list[str]) -> None:
    """Если на Vercel старая страница, показываем WebApp прямо с бота."""
    if not config.WEBAPP_URL or not config.PUBLIC_URL:
        return
    try:
        async with session.get(config.WEBAPP_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            fresh = r.status == 200 and PAGE_MARKER in await r.text()
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
        fresh = False
    if not fresh:
        log.warning("На %s старая версия страницы (или она недоступна) — показываю WebApp с адреса бота. "
                    "Чтобы работал Vercel, обновите там index.html (влейте ветку в main).", config.WEBAPP_URL)
        notes.append("ℹ️ На Vercel старая страница — пока показываю WebApp напрямую с компьютера")
        config.WEBAPP_URL = ""


async def on_startup(bot: Bot, notifier: handlers.Notifier, notes: list[str]) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть бронирование"),
        BotCommand(command="my", description="Мои брони"),
        BotCommand(command="status", description="Свободные ПК"),
        BotCommand(command="help", description="Помощь"),
    ])
    await update_menu_button(bot)
    me = await bot.get_me()
    print(f"\n{LINE}\n  ✅ Бот @{me.username} работает\n  🌐 Бронирование: {handlers.webapp_url()}"
          f"\n  Остановить: Ctrl+C (или закройте это окно)\n{LINE}\n", flush=True)
    text = "🟢 <b>Бот запущен</b>, бронирование открыто.\nКнопка «🎮 Бронь» — слева от поля ввода."
    if notes:
        text += "\n\n" + "\n".join(notes)
    await notifier.to_admins(text, handlers.app_keyboard())


async def main(polling: bool) -> None:
    check_config()
    storage = Storage(config.DB_PATH)
    service = BookingService(storage)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = handlers.Notifier(bot, service)
    notes: list[str] = []

    tun = await open_tunnel(bot, notifier, notes)
    allowed_origins = {o for o in map(config.origin_of, (config.WEBAPP_URL, config.PUBLIC_URL)) if o}
    app = create_app(
        service, notifier, bot_token=config.BOT_TOKEN, dev_mode=config.DEV_MODE,
        allowed_origins=allowed_origins, index_path=config.BASE_DIR / "index.html",
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    reminders: asyncio.Task | None = None
    try:
        await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
        log.info("API слушает http://%s:%s", config.WEB_HOST, config.WEB_PORT)
        async with aiohttp.ClientSession() as session:
            await check_public_access(session, notes)
            await choose_webapp_page(session, notes)
        if not config.PUBLIC_URL:
            notes.append("⚠️ Нет публичного адреса — запустите `python bot.py --setup`")

        reminders = asyncio.create_task(handlers.reminder_loop(service, notifier))
        if polling:
            dp = Dispatcher(service=service, notifier=notifier)
            dp.include_router(handlers.router)

            async def startup(bot: Bot) -> None:
                await on_startup(bot, notifier, notes)

            dp.startup.register(startup)
            await dp.start_polling(bot)
        else:
            log.info("Режим --no-polling: бот не подключается к Telegram, работает только API")
            await asyncio.Event().wait()
    finally:
        if reminders:
            reminders.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reminders
        await runner.cleanup()
        if tun:
            await tun.stop()
        await bot.session.close()
        storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="пройти настройку заново")
    parser.add_argument("--no-polling", action="store_true", help="запустить только API, без Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if args.setup or (not config.BOT_TOKEN and sys.stdin.isatty()):
        import setup_wizard

        try:
            setup_wizard.run()
        except (KeyboardInterrupt, EOFError):
            raise SystemExit("\nНастройка прервана") from None
        importlib.reload(config)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(polling=not args.no_polling))
