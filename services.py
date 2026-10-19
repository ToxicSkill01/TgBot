"""Бизнес-логика бронирования: общая для WebApp API и кнопок бота."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import config
from storage import ACTIVE, CANCELLED, FINISHED, Booking, Storage


class BookingError(Exception):
    """Ошибка, которую можно показать клиенту как есть."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    username: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.id in config.ADMIN_IDS

    @property
    def label(self) -> str:
        return f"{self.name} (@{self.username})" if self.username else self.name


@dataclass(frozen=True, slots=True)
class PcStatus:
    state: str              # free | soon | busy
    busy_until: int | None  # конец текущей сессии (с учётом броней встык)
    free_until: int | None  # начало ближайшей брони, если ПК свободен
    current: Booking | None


# --- форматирование --------------------------------------------------------

def price_for(pc: config.Pc, minutes: int) -> int:
    return math.ceil(pc.zone.price_per_hour * minutes / 60)


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.CLUB_TZ).strftime("%H:%M")


def fmt_when(ts: int, now: int) -> str:
    day = datetime.fromtimestamp(ts, config.CLUB_TZ).date()
    today = datetime.fromtimestamp(now, config.CLUB_TZ).date()
    diff = (day - today).days
    label = {-1: "вчера", 0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(diff, day.strftime("%d.%m"))
    return f"{label} {fmt_time(ts)}"


def fmt_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    return f"{hours} ч" if hours else f"{mins} мин"


def fmt_range(booking: Booking, now: int) -> str:
    return f"{fmt_when(booking.start_at, now)} → {fmt_time(booking.end_at)} ({fmt_duration(booking.minutes)})"


def pc_status(slots: list[Booking], now: int) -> PcStatus:
    """Статус ПК по его активным броням, отсортированным по началу."""
    current = next((b for b in slots if b.start_at <= now < b.end_at), None)
    if current:
        until = current.end_at
        for b in slots:  # брони встык продлевают занятость
            if b.start_at <= until < b.end_at:
                until = b.end_at
        return PcStatus("busy", until, None, current)
    upcoming = next((b for b in slots if b.start_at > now), None)
    if upcoming and upcoming.start_at - now <= config.SOON_MIN * 60:
        return PcStatus("soon", None, upcoming.start_at, None)
    return PcStatus("free", None, upcoming.start_at if upcoming else None, None)


# --- сервис ----------------------------------------------------------------

class BookingService:
    def __init__(self, storage: Storage, clock: Callable[[], float] = time.time) -> None:
        self.storage = storage
        self._clock = clock

    def now(self) -> int:
        return int(self._clock())

    @staticmethod
    def get_pc(pc_id: int) -> config.Pc:
        pc = config.PCS.get(pc_id)
        if pc is None:
            raise BookingError("Такого ПК нет", 404)
        return pc

    def _owned(self, user: User, booking_id: int) -> Booking:
        booking = self.storage.get(booking_id)
        if booking is None or (booking.user_id != user.id and not user.is_admin):
            raise BookingError("Бронь не найдена", 404)
        if booking.status != ACTIVE or booking.end_at <= self.now():
            raise BookingError("Эта бронь уже завершена", 409)
        return booking

    def _conflict_error(self, other: Booking) -> BookingError:
        now = self.now()
        return BookingError(
            f"ПК {other.pc_id} уже занят: {fmt_when(other.start_at, now)}–{fmt_time(other.end_at)}", 409
        )

    def book(self, user: User, pc_id: int, start_at: int, minutes: int) -> Booking:
        pc = self.get_pc(pc_id)
        now = self.now()
        if not config.MIN_BOOKING_MIN <= minutes <= config.MAX_BOOKING_MIN:
            raise BookingError(
                f"Длительность — от {fmt_duration(config.MIN_BOOKING_MIN)}"
                f" до {fmt_duration(config.MAX_BOOKING_MIN)}"
            )
        if minutes % config.BOOKING_STEP_MIN:
            raise BookingError(f"Длительность должна быть кратна {config.BOOKING_STEP_MIN} мин")
        start_at -= start_at % 60
        if start_at < now - config.START_GRACE_SEC:
            raise BookingError("Время начала уже прошло — выберите заново")
        if start_at > now + config.HORIZON_HOURS * 3600:
            raise BookingError(f"Бронировать можно не дальше чем на {config.HORIZON_HOURS} ч вперёд")
        if not user.is_admin and len(self.storage.user_active(user.id, now)) >= config.MAX_ACTIVE_PER_USER:
            raise BookingError(f"У вас уже {config.MAX_ACTIVE_PER_USER} активные брони — это максимум", 429)

        end_at = start_at + minutes * 60
        if other := self.storage.find_overlap(pc.id, start_at, end_at):
            raise self._conflict_error(other)
        return self.storage.insert(
            pc_id=pc.id, user_id=user.id, user_name=user.name, username=user.username,
            start_at=start_at, end_at=end_at, created_at=now,
        )

    def extend(self, user: User, booking_id: int, minutes: int) -> Booking:
        booking = self._owned(user, booking_id)
        if not 1 <= minutes <= config.MAX_BOOKING_MIN or minutes % config.BOOKING_STEP_MIN:
            raise BookingError("Некорректное время продления")
        new_end = booking.end_at + minutes * 60
        if not user.is_admin and (new_end - booking.start_at) // 60 > config.MAX_BOOKING_MIN:
            raise BookingError(f"Одна бронь — не больше {fmt_duration(config.MAX_BOOKING_MIN)}")
        if other := self.storage.find_overlap(booking.pc_id, booking.end_at, new_end, exclude_id=booking.id):
            free = (other.start_at - booking.end_at) // 60
            hint = f" Можно продлить максимум на {fmt_duration(free)}." if free > 0 else ""
            raise BookingError(f"Дальше ПК занят с {fmt_time(other.start_at)}.{hint}", 409)
        self.storage.set_end(booking.id, new_end)
        return self.storage.get(booking.id)

    def release(self, user: User, booking_id: int) -> Booking:
        """Отменяет будущую бронь или досрочно завершает идущую сессию."""
        booking = self._owned(user, booking_id)
        now = self.now()
        if booking.start_at > now:
            self.storage.close_booking(booking.id, CANCELLED)
        else:
            self.storage.close_booking(booking.id, FINISHED, end_at=max(now, booking.start_at + 60))
        return self.storage.get(booking.id)

    def user_bookings(self, user_id: int) -> list[Booking]:
        return self.storage.user_active(user_id, self.now())

    def slots_by_pc(self, now: int) -> dict[int, list[Booking]]:
        slots: dict[int, list[Booking]] = {pc_id: [] for pc_id in config.PCS}
        for b in self.storage.active_between(now, now + config.HORIZON_HOURS * 3600):
            slots.setdefault(b.pc_id, []).append(b)
        return slots

    def snapshot(self, user: User | None) -> dict:
        """Состояние клуба для WebApp. Чужие имена видят только админы."""
        now = self.now()
        is_admin = bool(user and user.is_admin)
        offset = datetime.fromtimestamp(now, config.CLUB_TZ).utcoffset()

        def slot(b: Booking) -> dict:
            data = {"start": b.start_at, "end": b.end_at, "mine": bool(user and b.user_id == user.id)}
            if data["mine"] or is_admin:
                data["id"] = b.id
            if is_admin:
                data["who"] = f"{b.user_name} (@{b.username})" if b.username else b.user_name
            return data

        slots = self.slots_by_pc(now)
        return {
            "now": now,
            "tz_offset": int(offset.total_seconds() // 60) if offset else 0,
            "club": config.CLUB_NAME,
            "rules": {
                "min": config.MIN_BOOKING_MIN,
                "max": config.MAX_BOOKING_MIN,
                "step": config.BOOKING_STEP_MIN,
                "horizon_hours": config.HORIZON_HOURS,
                "soon": config.SOON_MIN,
                "max_active": config.MAX_ACTIVE_PER_USER,
            },
            "zones": [
                {"id": z.id, "name": z.name, "price": z.price_per_hour, "specs": z.specs, "color": z.color}
                for z in config.ZONES
            ],
            "pcs": [
                {"id": pc.id, "zone": pc.zone.id, "slots": [slot(b) for b in slots[pc.id]]}
                for pc in config.PCS.values()
            ],
            "me": None if user is None else {
                "id": user.id,
                "name": user.name,
                "is_admin": is_admin,
                "bookings": [self.booking_json(b) for b in self.storage.user_active(user.id, now)],
            },
        }

    @staticmethod
    def booking_json(b: Booking) -> dict:
        pc = config.PCS.get(b.pc_id)
        return {
            "id": b.id,
            "pc_id": b.pc_id,
            "zone": pc.zone.id if pc else None,
            "start": b.start_at,
            "end": b.end_at,
            "status": b.status,
            "price": price_for(pc, b.minutes) if pc else 0,
        }
