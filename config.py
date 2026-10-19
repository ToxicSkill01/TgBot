"""Настройки клуба и бота.

Секреты (токен, id админов, адреса) читаются из файла .env — см. .env.example.
Зоны, цены и правила бронирования настраиваются прямо здесь.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def _parse_ids(raw: str) -> frozenset[int]:
    try:
        return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)
    except ValueError:
        raise SystemExit(f"ADMIN_IDS должен быть списком чисел через запятую, сейчас: {raw!r}") from None


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


# --- Telegram и сеть -------------------------------------------------------

BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_IDS = _parse_ids(_env("ADMIN_IDS"))

# Публичный HTTPS-адрес бота. Пусто — бот сам поднимет туннель (см. TUNNEL).
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")
# Туннель для бота на домашнем ПК: auto | cloudflared | tuna | off.
# auto = tuna, если задан TUNA_TOKEN, иначе cloudflared.
TUNNEL = _env("TUNNEL", "auto").lower()
TUNA_TOKEN = _env("TUNA_TOKEN")
# Где лежит index.html (например, Vercel). Пусто — WebApp отдаёт сам бот по PUBLIC_URL.
WEBAPP_URL = _env("WEBAPP_URL").rstrip("/")

WEB_HOST = _env("WEB_HOST", "127.0.0.1")  # туннель подключается локально
WEB_PORT = int(_env("WEB_PORT", "8080"))
DB_PATH = Path(_env("DB_PATH") or BASE_DIR / "club.db")

# Режим разработки: API пускает без Telegram (тестовый пользователь DEV_USER_ID).
# Никогда не включайте его на боевом сервере.
DEV_MODE = _env_bool("DEV_MODE")
DEV_USER_ID = int(_env("DEV_USER_ID", "1"))

# Сколько живёт подпись initData из Telegram.
INIT_DATA_TTL_SEC = 24 * 60 * 60

# --- Клуб -------------------------------------------------------------------

CLUB_NAME = _env("CLUB_NAME", "Toxic Skill Club")
CLUB_TZ = ZoneInfo(_env("CLUB_TZ", "Europe/Moscow"))


@dataclass(frozen=True, slots=True)
class Zone:
    id: str
    name: str
    price_per_hour: int
    specs: str
    color: str


@dataclass(frozen=True, slots=True)
class Pc:
    id: int
    zone: Zone


# Зоны и номера ПК. Поменяйте цены, железо и диапазоны под свой клуб.
ZONES: tuple[Zone, ...] = (
    Zone("standard", "Standard", 150, "RTX 4060 · i5-13400F · 165 Гц", "#39ff14"),
    Zone("vip", "VIP", 250, "RTX 4070 Super · i7-14700F · 240 Гц", "#c44dff"),
)
ZONE_LAYOUT: dict[str, range] = {
    "standard": range(1, 16),
    "vip": range(16, 22),
}

_ZONES_BY_ID = {zone.id: zone for zone in ZONES}
PCS: dict[int, Pc] = {
    pc_id: Pc(pc_id, _ZONES_BY_ID[zone_id])
    for zone_id, pc_ids in ZONE_LAYOUT.items()
    for pc_id in pc_ids
}

# --- Правила бронирования --------------------------------------------------

MIN_BOOKING_MIN = 30          # минимальная бронь, минут
MAX_BOOKING_MIN = 12 * 60     # максимальная бронь, минут
BOOKING_STEP_MIN = 1          # шаг ползунков: 1 = поминутно
HORIZON_HOURS = 48            # насколько вперёд можно бронировать
START_GRACE_SEC = 5 * 60      # допустимое «опоздание» времени начала в запросе
MAX_ACTIVE_PER_USER = 3       # активных броней на клиента (у админов без лимита)
SOON_MIN = 60                 # «скоро занят», если бронь начнётся раньше чем через N минут
REMIND_BEFORE_START_MIN = 15
REMIND_BEFORE_END_MIN = 10
REMINDER_INTERVAL_SEC = 30
