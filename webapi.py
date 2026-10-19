"""HTTP API для WebApp (aiohttp) с проверкой подписи Telegram initData."""

import gzip
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import config
from services import BookingError, BookingService, User

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

CORS_HEADERS = "Authorization, Content-Type, ngrok-skip-browser-warning"
WRITE_LIMIT_PER_MIN = 20
_dumps = partial(json.dumps, ensure_ascii=False, separators=(",", ":"))
HTTP_ERRORS = {404: "Не найдено", 405: "Метод не поддерживается", 413: "Слишком большой запрос"}


def user_from_init_data(token: str, init_data: str, now: float | None = None) -> User:
    """Проверяет подпись initData (HMAC от токена бота) и её свежесть."""
    try:
        data = safe_parse_webapp_init_data(token, init_data)
    except ValueError:
        raise BookingError("Не удалось проверить вход через Telegram", 401) from None
    age = (now or time.time()) - data.auth_date.timestamp()
    if age > config.INIT_DATA_TTL_SEC:
        raise BookingError("Сессия устарела — перезапустите приложение", 401)
    if data.user is None:
        raise BookingError("Нет данных пользователя Telegram", 401)
    name = " ".join(filter(None, [data.user.first_name, data.user.last_name])) or "Гость"
    return User(id=data.user.id, name=name, username=data.user.username)


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _json_error(message: str, status: int) -> web.Response:
    return _json({"error": message}, status)


def make_cors_middleware(allowed_origins: set[str], allow_any: bool):
    @web.middleware
    async def cors(request: web.Request, handler: Handler) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)
        if origin and (allow_any or origin in allowed_origins):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = CORS_HEADERS
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
        return response

    return cors


@web.middleware
async def errors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except BookingError as exc:
        return _json_error(exc.message, exc.status)
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            return _json_error(HTTP_ERRORS.get(exc.status, exc.reason), exc.status)
        raise
    except Exception:
        log.exception("Ошибка обработки %s %s", request.method, request.path)
        return _json_error("Внутренняя ошибка сервера", 500)


class Api:
    def __init__(self, service: BookingService, notifier, *, bot_token: str, dev_mode: bool) -> None:
        self.service = service
        self.notifier = notifier
        self.bot_token = bot_token
        self.dev_mode = dev_mode
        self._writes: dict[int, deque[float]] = defaultdict(deque)

    def user(self, request: web.Request) -> User | None:
        header = request.headers.get("Authorization", "")
        if header.startswith("tma ") and len(header) > 4:
            return user_from_init_data(self.bot_token, header[4:])
        if self.dev_mode:
            return User(id=config.DEV_USER_ID, name="Dev", username="dev")
        return None

    def require_user(self, request: web.Request) -> User:
        user = self.user(request)
        if user is None:
            raise BookingError("Откройте приложение через Telegram-бота", 401)
        self._rate_limit(user)
        return user

    def _rate_limit(self, user: User) -> None:
        now = time.monotonic()
        hits = self._writes[user.id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= WRITE_LIMIT_PER_MIN:
            raise BookingError("Слишком много действий, подождите минуту", 429)
        hits.append(now)

    @staticmethod
    async def _body(request: web.Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BookingError("Некорректный запрос") from None
        if not isinstance(body, dict):
            raise BookingError("Некорректный запрос")
        return body

    @staticmethod
    def _int(body: dict, key: str) -> int:
        value = body.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise BookingError(f"Поле {key} должно быть целым числом")
        return value

    @staticmethod
    def _booking_id(request: web.Request) -> int:
        try:
            return int(request.match_info["booking_id"])
        except ValueError:
            raise BookingError("Бронь не найдена", 404) from None

    async def health(self, request: web.Request) -> web.Response:
        return _json({"ok": True, "now": self.service.now()})

    async def state(self, request: web.Request) -> web.Response:
        response = _json(self.service.snapshot(self.user(request)))
        response.enable_compression()
        return response

    async def create_booking(self, request: web.Request) -> web.Response:
        user = self.require_user(request)
        body = await self._body(request)
        booking = self.service.book(user, self._int(body, "pc_id"), self._int(body, "start"), self._int(body, "minutes"))
        log.info("Бронь #%s: ПК %s, %s, %s мин", booking.id, booking.pc_id, user.label, booking.minutes)
        self.notifier.spawn(self.notifier.booking_created(booking, user))
        return _json({"booking": self.service.booking_json(booking)}, status=201)

    async def extend_booking(self, request: web.Request) -> web.Response:
        user = self.require_user(request)
        body = await self._body(request)
        minutes = self._int(body, "minutes")
        booking = self.service.extend(user, self._booking_id(request), minutes)
        self.notifier.spawn(self.notifier.booking_extended(booking, user, minutes))
        return _json({"booking": self.service.booking_json(booking)})

    async def release_booking(self, request: web.Request) -> web.Response:
        user = self.require_user(request)
        booking = self.service.release(user, self._booking_id(request))
        self.notifier.spawn(self.notifier.booking_released(booking, user))
        return _json({"booking": self.service.booking_json(booking)})


def create_app(
    service: BookingService, notifier, *, bot_token: str, dev_mode: bool = False,
    allowed_origins: set[str] | None = None, index_path: Path | None = None,
) -> web.Application:
    app = web.Application(
        middlewares=[make_cors_middleware(allowed_origins or set(), dev_mode), errors_middleware],
        client_max_size=16 * 1024,
    )
    api = Api(service, notifier, bot_token=bot_token, dev_mode=dev_mode)

    app.router.add_get("/api/health", api.health)
    app.router.add_get("/api/state", api.state)
    app.router.add_post("/api/bookings", api.create_booking)
    app.router.add_post("/api/bookings/{booking_id}/extend", api.extend_booking)
    app.router.add_delete("/api/bookings/{booking_id}", api.release_booking)

    if index_path is not None:
        app.router.add_get("/", make_index_handler(index_path))
    return app


def make_index_handler(index_path: Path) -> Handler:
    """Отдаёт только index.html (не весь каталог — там .env и база), сжатым и с ETag."""
    page = index_path.read_bytes()
    page_gz = gzip.compress(page, 9)
    etag = '"' + hashlib.sha1(page).hexdigest()[:16] + '"'
    headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding"}

    async def index(request: web.Request) -> web.Response:
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=page_gz, headers={**headers, "Content-Encoding": "gzip"})
        return web.Response(body=page, headers=headers)

    return index
