"""Мастер первой настройки: за пару минут создаёт файл .env.

Запускается сам при первом запуске бота, повторно — `python bot.py --setup`.
"""

import asyncio
import os
import platform
import subprocess

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError

import config
import tunnel

ENV_PATH = config.BASE_DIR / ".env"
DEFAULT_WEBAPP = "https://toxic-skill.vercel.app"
LINE = "─" * 60


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    value = input(f"{prompt}{hint}: ").strip()
    return value or default


def merge_env(text: str, values: dict[str, str]) -> str:
    """Обновляет ключи в тексте .env, сохраняя остальные строки и комментарии."""
    lines, seen = [], set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{key}={value}" for key, value in values.items() if key not in seen]
    return "\n".join(lines) + "\n"


async def check_token(token: str) -> str | None:
    """Имя бота или None, если Telegram не принял токен. Нет связи — исключение."""
    try:
        bot = Bot(token)
    except TokenValidationError:
        return None
    try:
        return (await bot.get_me()).username
    except TelegramUnauthorizedError:
        return None
    finally:
        await bot.session.close()


async def wait_for_admin(token: str, timeout: int = 300) -> tuple[int, str] | None:
    """Ждёт первое сообщение боту и возвращает id и имя отправителя."""
    bot = Bot(token)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        offset = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for update in await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"]):
                offset = update.update_id + 1
                user = update.message.from_user if update.message else None
                if user and not user.is_bot:
                    await bot.send_message(
                        user.id, "✅ Готово! Вы — администратор клуба.\nДосматриваю настройку на компьютере…"
                    )
                    # Подтверждаем это сообщение, иначе бот после запуска ответит на него ещё раз.
                    await bot.get_updates(offset=offset, timeout=0)
                    return user.id, user.full_name
        return None
    finally:
        await bot.session.close()


def ask_token() -> tuple[str, str]:
    say("Шаг 1/4. Токен бота")
    say("  Откройте в Telegram @BotFather → /mybots → ваш бот → API Token")
    say("  (нового бота создаёт команда /newbot). Скопируйте токен сюда.")
    while True:
        token = ask("  Токен").replace(" ", "")
        if ":" not in token:
            say("  ✖ Токен выглядит как 123456789:AAH... — попробуйте ещё раз")
            continue
        say("  Проверяю…")
        try:
            username = asyncio.run(check_token(token))
        except TelegramNetworkError:
            say("  ✖ Нет связи с Telegram. Проверьте интернет и нажмите Enter, чтобы повторить.")
            input()
            continue
        if username:
            say(f"  ✔ Бот @{username} найден")
            return token, username
        say("  ✖ Telegram не принял токен. Проверьте, что скопировали его целиком.")


def ask_admin(token: str, username: str) -> str:
    say()
    say("Шаг 2/4. Администратор")
    say(f"  Откройте https://t.me/{username} на телефоне и нажмите «Старт» (или отправьте /start).")
    say("  Жду ваше сообщение… (до 5 минут)")
    try:
        found = asyncio.run(wait_for_admin(token))
    except TelegramAPIError as exc:
        say(f"  ✖ Не получилось: {exc}")
        found = None
    if found:
        say(f"  ✔ Администратор: {found[1]} (id {found[0]})")
        return str(found[0])
    say("  Сообщение не пришло. Свой id можно узнать у @userinfobot.")
    return ask("  Telegram id администратора (можно несколько через запятую, Enter — пропустить)")


def ask_tunnel() -> dict[str, str]:
    say()
    say("Шаг 3/4. Как клиенты попадут к боту из интернета")
    say("  1 — Cloudflare: бесплатно и без регистрации (всё скачается само).")
    say("      Минус: в России Cloudflare бывает замедлен провайдерами.")
    say("  2 — tuna.am: серверы в России, нужна бесплатная регистрация на tuna.am.")
    say("  3 — у меня свой https-адрес (VPS, домен, ngrok и т. п.).")
    choice = ask("  Ваш выбор", "1")
    if choice == "2":
        say("  Зарегистрируйтесь на https://tuna.am и скопируйте токен со страницы https://my.tuna.am/token")
        token = ask("  Токен tuna")
        if tunnel.find_binary("tuna") is None:
            install_tuna()
        return {"TUNNEL": "tuna", "TUNA_TOKEN": token, "PUBLIC_URL": ""}
    if choice == "3":
        url = ask("  Ваш https-адрес (например https://bot.example.ru)").rstrip("/")
        return {"TUNNEL": "off", "PUBLIC_URL": url}
    return {"TUNNEL": "cloudflared", "PUBLIC_URL": ""}


YES = {"д", "да", "y", "yes"}


def install_tuna() -> None:
    question = "  Программа tuna не установлена. Установить сейчас через winget? (д/н)"
    if platform.system() == "Windows" and ask(question, "д").lower() in YES:
        result = subprocess.run(["winget", "install", "--id", "yuccastream.tuna", "-e"], check=False)
        if result.returncode == 0:
            say("  ✔ tuna установлена. Если бот её не найдёт — перезапустите start.bat")
            return
    say(f"  ⚠ {tunnel.TUNA_INSTALL_HINT}")


def ask_webapp() -> str:
    say()
    say("Шаг 4/4. Где лежит страница бронирования (WebApp)")
    say("  Enter — на Vercel (если там уже новая версия, бот проверит сам и при необходимости")
    say("  покажет страницу со своего адреса). «-» — всегда показывать страницу с компьютера.")
    url = ask("  Адрес WebApp", DEFAULT_WEBAPP)
    return "" if url == "-" else url.rstrip("/")


def run() -> None:
    say(LINE)
    say(f"  🎮 Настройка бота {config.CLUB_NAME}")
    say(LINE)
    token, username = ask_token()
    values = {"BOT_TOKEN": token, "ADMIN_IDS": ask_admin(token, username)}
    values |= ask_tunnel()
    values["WEBAPP_URL"] = ask_webapp()

    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    ENV_PATH.write_text(merge_env(existing, values), encoding="utf-8")
    os.environ.update(values)
    say()
    say(LINE)
    say(f"  ✔ Настройки сохранены в {ENV_PATH.name}. Запускаю бота…")
    say(LINE)


if __name__ == "__main__":
    run()
