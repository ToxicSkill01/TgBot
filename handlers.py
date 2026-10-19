"""Команды бота, кнопки под сообщениями, уведомления и напоминания."""

import asyncio
import html
import logging
from collections.abc import Coroutine
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

import config
from services import (
    BookingError,
    BookingService,
    User,
    fmt_duration,
    fmt_range,
    fmt_time,
    fmt_when,
    pc_status,
    price_for,
)
from storage import ACTIVE, CANCELLED, Booking

log = logging.getLogger(__name__)
router = Router(name="club")
# Кнопки WebApp в сообщениях работают только в личке с ботом.
router.message.filter(F.chat.type == "private")


class BookingAction(CallbackData, prefix="bk"):
    action: str  # release | extend
    id: int
    minutes: int = 0


def webapp_url() -> str:
    """Адрес WebApp. Страница на Vercel узнаёт адрес API из параметра ?api=."""
    page = config.WEBAPP_URL or config.PUBLIC_URL
    if config.PUBLIC_URL and config.origin_of(config.PUBLIC_URL) != config.origin_of(page):
        return f"{page}/?api={quote(config.PUBLIC_URL, safe='')}"
    return f"{page}/"


def app_keyboard(text: str = "🎮 Открыть бронирование") -> InlineKeyboardMarkup | None:
    url = webapp_url()
    if not url.startswith("https://"):
        return None  # Telegram принимает WebApp только по https
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


def is_admin(message: Message) -> bool:
    return message.from_user.id in config.ADMIN_IDS


def booking_keyboard(booking: Booking, now: int, *, extend: bool = True) -> InlineKeyboardMarkup:
    started = booking.start_at <= now
    row = [
        InlineKeyboardButton(
            text="🏁 Освободить ПК" if started else "❌ Отменить",
            callback_data=BookingAction(action="release", id=booking.id).pack(),
        )
    ]
    if extend:
        row.append(
            InlineKeyboardButton(
                text="➕ 30 мин", callback_data=BookingAction(action="extend", id=booking.id, minutes=30).pack()
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def to_user(tg_user) -> User:
    name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or "Гость"
    return User(id=tg_user.id, name=name, username=tg_user.username)


def booking_card(booking: Booking, now: int, title: str) -> str:
    pc = config.PCS.get(booking.pc_id)
    zone = pc.zone.name if pc else "—"
    price = f"\n💰 {price_for(pc, booking.minutes)} ₽ — оплата на месте" if pc else ""
    return (
        f"{title}\n\n"
        f"🖥 <b>ПК {booking.pc_id:02d}</b> · {zone}\n"
        f"🕒 {fmt_range(booking, now)}"
        f"{price}"
    )


# --- уведомления -----------------------------------------------------------

class Notifier:
    def __init__(self, bot: Bot, service: BookingService) -> None:
        self.bot = bot
        self.service = service
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine) -> None:
        """Уведомление уходит в фоне: ответ WebApp не ждёт Telegram."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)

    async def to_admins(self, text: str, markup: InlineKeyboardMarkup | None = None, skip: int | None = None) -> None:
        await asyncio.gather(*(self.send(a, text, markup) for a in config.ADMIN_IDS if a != skip))

    async def booking_created(self, booking: Booking, by: User) -> None:
        now = self.service.now()
        await self.send(
            booking.user_id,
            booking_card(booking, now, f"✅ <b>Бронь #{booking.id} подтверждена</b>"),
            booking_keyboard(booking, now, extend=False),
        )
        await self.to_admins(
            booking_card(booking, now, f"🆕 <b>Новая бронь #{booking.id}</b>\n👤 {html.escape(by.label)}"),
            booking_keyboard(booking, now, extend=False),
            skip=by.id,
        )

    async def booking_extended(self, booking: Booking, by: User, minutes: int) -> None:
        now = self.service.now()
        await self.to_admins(
            booking_card(booking, now, f"➕ <b>Бронь #{booking.id} продлена на {fmt_duration(minutes)}</b>\n"
                                       f"👤 {html.escape(by.label)}"),
            skip=by.id,
        )

    async def booking_released(self, booking: Booking, by: User) -> None:
        now = self.service.now()
        verb = "отменена" if booking.status == CANCELLED else "завершена досрочно"
        if by.id != booking.user_id:
            await self.send(booking.user_id, booking_card(booking, now, f"ℹ️ <b>Администратор: бронь #{booking.id} {verb}</b>"))
        await self.to_admins(
            booking_card(booking, now, f"🗑 <b>Бронь #{booking.id} {verb}</b>\n👤 {html.escape(by.label)}"),
            skip=by.id,
        )


async def reminder_loop(service: BookingService, notifier: Notifier) -> None:
    """Напоминает о начале и скором конце сессии."""
    while True:
        try:
            now = service.now()
            for b in service.storage.due_start_reminders(now, config.REMIND_BEFORE_START_MIN * 60):
                service.storage.mark_reminded(b.id, start=True)
                await notifier.send(
                    b.user_id,
                    booking_card(b, now, f"⏰ <b>Через {fmt_duration((b.start_at - now + 59) // 60)} ваша бронь</b>"),
                    booking_keyboard(b, now, extend=False),
                )
            for b in service.storage.due_end_reminders(now, config.REMIND_BEFORE_END_MIN * 60):
                service.storage.mark_reminded(b.id, end=True)
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"➕ {label}", callback_data=BookingAction(
                        action="extend", id=b.id, minutes=minutes).pack())
                    for label, minutes in (("30 мин", 30), ("1 час", 60), ("2 часа", 120))
                ]])
                await notifier.send(
                    b.user_id,
                    f"⌛ <b>Сессия на ПК {b.pc_id:02d} заканчивается в {fmt_time(b.end_at)}</b>\n"
                    f"Осталось {fmt_duration(max(1, (b.end_at - now) // 60))}. Продлить?",
                    markup,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(config.REMINDER_INTERVAL_SEC)


# --- команды ---------------------------------------------------------------

HELP = (
    "<b>Команды</b>\n"
    "/start — открыть бронирование\n"
    "/my — мои брони\n"
    "/status — какие ПК свободны и до скольки заняты\n"
    "/help — эта подсказка"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    keyboard = app_keyboard()
    text = (
        f"🎮 <b>{html.escape(config.CLUB_NAME)}</b>\n\n"
        "Бронируй ПК прямо в Telegram:\n"
        "• видно, какие ПК свободны и <b>до скольки заняты</b>\n"
        "• время выбирается <b>с точностью до минуты</b>\n"
        "• напомню о начале и о конце сессии, продлить можно в один тап\n\n"
    )
    text += "Жми кнопку 👇" if keyboard else "⚠️ Бронирование временно недоступно — администратор уже чинит"
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    extra = "\n/bookings — все брони (админ)" if is_admin(message) else ""
    await message.answer(HELP + extra, reply_markup=app_keyboard())


@router.message(Command("my"))
async def cmd_my(message: Message, service: BookingService) -> None:
    bookings = service.user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас нет активных броней.", reply_markup=app_keyboard("🎮 Забронировать ПК"))
        return
    now = service.now()
    for b in bookings:
        title = "🟢 <b>Идёт сейчас</b>" if b.start_at <= now else f"📅 <b>Бронь #{b.id}</b>"
        await message.answer(booking_card(b, now, title), reply_markup=booking_keyboard(b, now))


@router.message(Command("status"))
async def cmd_status(message: Message, service: BookingService) -> None:
    now = service.now()
    slots = service.slots_by_pc(now)
    free, lines = [], []
    for pc in config.PCS.values():
        status = pc_status(slots[pc.id], now)
        if status.state == "busy":
            lines.append(f"🔴 ПК {pc.id:02d} — занят до {fmt_when(status.busy_until, now)}")
        elif status.state == "soon":
            free.append(f"{pc.id:02d}")
            lines.append(f"🟡 ПК {pc.id:02d} — свободен до {fmt_time(status.free_until)}")
        else:
            free.append(f"{pc.id:02d}")
    text = (
        f"🖥 <b>{html.escape(config.CLUB_NAME)}</b> · сейчас {fmt_time(now)}\n"
        f"Свободно <b>{len(free)}</b> из {len(config.PCS)}\n\n"
    )
    if free:
        text += f"🟢 Свободны: {', '.join(free)}\n"
    text += "\n".join(lines)
    await message.answer(text, reply_markup=app_keyboard("🎮 Забронировать ПК"))


@router.message(Command("bookings"), is_admin)
async def cmd_bookings(message: Message, service: BookingService) -> None:
    now = service.now()
    bookings = [b for pc_slots in service.slots_by_pc(now).values() for b in pc_slots]
    if not bookings:
        await message.answer("Активных броней нет.")
        return
    bookings.sort(key=lambda b: b.start_at)
    lines = [
        f"#{b.id} · ПК {b.pc_id:02d} · {fmt_when(b.start_at, now)}–{fmt_time(b.end_at)}"
        f" · {html.escape(b.user_name)}{f' @{b.username}' if b.username else ''}"
        for b in bookings[:50]
    ]
    await message.answer("📋 <b>Брони на ближайшие {} ч</b>\n\n{}".format(config.HORIZON_HOURS, "\n".join(lines)))


@router.callback_query(BookingAction.filter())
async def on_booking_action(
    callback: CallbackQuery, callback_data: BookingAction, service: BookingService, notifier: Notifier
) -> None:
    user = to_user(callback.from_user)
    try:
        if callback_data.action == "extend":
            booking = service.extend(user, callback_data.id, callback_data.minutes)
            notifier.spawn(notifier.booking_extended(booking, user, callback_data.minutes))
            title = f"➕ <b>Продлено на {fmt_duration(callback_data.minutes)}</b>"
            await callback.answer(f"Готово! Теперь до {fmt_time(booking.end_at)}")
        else:
            booking = service.release(user, callback_data.id)
            notifier.spawn(notifier.booking_released(booking, user))
            title = "❌ <b>Бронь отменена</b>" if booking.status == CANCELLED else "🏁 <b>ПК освобождён</b>"
            await callback.answer("Готово")
    except BookingError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    now = service.now()
    active = booking.status == ACTIVE and booking.end_at > now
    if isinstance(callback.message, Message):  # старые сообщения Telegram не даёт редактировать
        try:
            await callback.message.edit_text(
                booking_card(booking, now, title),
                reply_markup=booking_keyboard(booking, now) if active else None,
            )
        except TelegramAPIError:
            pass


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Я бот бронирования ПК 🎮\nНажми кнопку ниже или посмотри /help", reply_markup=app_keyboard()
    )
