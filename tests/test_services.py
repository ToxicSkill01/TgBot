"""Тесты логики бронирования и API. Запуск: python -m unittest discover -s tests"""

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from aiohttp.test_utils import AioHTTPTestCase

import config
from services import BookingError, BookingService, User, pc_status
from storage import CANCELLED, FINISHED, Storage
from webapi import create_app, user_from_init_data

TOKEN = "123456:TEST"
NOW = 1_760_000_000 - 1_760_000_000 % 60  # ровная минута
ADMIN_ID = 999


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def sign(user: dict, auth_date: int, token: str = TOKEN) -> str:
    fields = {"auth_date": str(auth_date), "query_id": "AAA", "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._admins = config.ADMIN_IDS
        config.ADMIN_IDS = frozenset({ADMIN_ID})
        self.clock = Clock(NOW)
        self.storage = Storage(":memory:")
        self.service = BookingService(self.storage, clock=self.clock)
        self.alice = User(1, "Alice", "alice")
        self.bob = User(2, "Bob")
        self.admin = User(ADMIN_ID, "Admin")

    def tearDown(self) -> None:
        config.ADMIN_IDS = self._admins
        self.storage.close()


class BookingTests(ServiceTestCase):
    def test_book_and_conflict(self) -> None:
        b = self.service.book(self.alice, 1, NOW, 90)
        self.assertEqual(b.end_at - b.start_at, 90 * 60)
        with self.assertRaises(BookingError) as ctx:
            self.service.book(self.bob, 1, NOW + 30 * 60, 60)
        self.assertEqual(ctx.exception.status, 409)
        # другой ПК свободен
        self.service.book(self.bob, 2, NOW + 30 * 60, 60)

    def test_back_to_back_is_not_conflict(self) -> None:
        self.service.book(self.alice, 3, NOW, 60)
        self.service.book(self.bob, 3, NOW + 60 * 60, 60)
        with self.assertRaises(BookingError):
            self.service.book(self.bob, 3, NOW + 59 * 60, 30)

    def test_minute_precision(self) -> None:
        b = self.service.book(self.alice, 4, NOW + 17 * 60 + 25, 47)
        self.assertEqual(b.start_at, NOW + 17 * 60)  # секунды отбрасываются
        self.assertEqual(b.minutes, 47)

    def test_validation(self) -> None:
        cases = [
            (99, NOW, 60, 404),                                     # нет такого ПК
            (1, NOW, config.MIN_BOOKING_MIN - 1, 400),              # слишком коротко
            (1, NOW, config.MAX_BOOKING_MIN + 1, 400),              # слишком долго
            (1, NOW - config.START_GRACE_SEC - 60, 60, 400),        # в прошлом
            (1, NOW + config.HORIZON_HOURS * 3600 + 60, 60, 400),   # слишком далеко
        ]
        for pc_id, start, minutes, status in cases:
            with self.subTest(pc_id=pc_id, start=start, minutes=minutes):
                with self.assertRaises(BookingError) as ctx:
                    self.service.book(self.alice, pc_id, start, minutes)
                self.assertEqual(ctx.exception.status, status)

    def test_small_grace_in_past_allowed(self) -> None:
        self.service.book(self.alice, 5, NOW - 60, 60)

    def test_active_limit(self) -> None:
        for pc_id in range(1, config.MAX_ACTIVE_PER_USER + 1):
            self.service.book(self.alice, pc_id, NOW, 60)
        with self.assertRaises(BookingError) as ctx:
            self.service.book(self.alice, 10, NOW, 60)
        self.assertEqual(ctx.exception.status, 429)
        for pc_id in range(11, 11 + config.MAX_ACTIVE_PER_USER + 1):
            self.service.book(self.admin, pc_id, NOW, 60)  # админ без лимита

    def test_extend(self) -> None:
        b = self.service.book(self.alice, 6, NOW, 60)
        self.service.book(self.bob, 6, NOW + 100 * 60, 60)
        b = self.service.extend(self.alice, b.id, 30)
        self.assertEqual(b.minutes, 90)
        with self.assertRaises(BookingError) as ctx:
            self.service.extend(self.alice, b.id, 30)
        self.assertIn("10 мин", ctx.exception.message)
        with self.assertRaises(BookingError):
            self.service.extend(self.bob, b.id, 5)  # чужая бронь

    def test_release_future_and_live(self) -> None:
        future = self.service.book(self.alice, 7, NOW + 3600, 60)
        self.assertEqual(self.service.release(self.alice, future.id).status, CANCELLED)
        live = self.service.book(self.alice, 8, NOW, 120)
        self.clock.now = NOW + 30 * 60
        released = self.service.release(self.alice, live.id)
        self.assertEqual(released.status, FINISHED)
        self.assertEqual(released.end_at, self.clock.now)
        self.service.book(self.bob, 8, NOW + 31 * 60, 60)  # ПК снова свободен

    def test_admin_can_release_any(self) -> None:
        b = self.service.book(self.alice, 9, NOW + 600, 60)
        with self.assertRaises(BookingError):
            self.service.release(self.bob, b.id)
        self.service.release(self.admin, b.id)

    def test_status_busy_until_merges_back_to_back(self) -> None:
        self.service.book(self.alice, 12, NOW, 60)
        self.service.book(self.bob, 12, NOW + 3600, 45)
        status = pc_status(self.service.slots_by_pc(NOW)[12], NOW + 10)
        self.assertEqual(status.state, "busy")
        self.assertEqual(status.busy_until, NOW + 105 * 60)

    def test_status_soon_and_free(self) -> None:
        self.service.book(self.alice, 13, NOW + 30 * 60, 60)
        self.service.book(self.alice, 14, NOW + 5 * 3600, 60)
        slots = self.service.slots_by_pc(NOW)
        self.assertEqual(pc_status(slots[13], NOW).state, "soon")
        free = pc_status(slots[14], NOW)
        self.assertEqual((free.state, free.free_until), ("free", NOW + 5 * 3600))
        self.assertEqual(pc_status(slots[15], NOW).state, "free")

    def test_snapshot_hides_other_users(self) -> None:
        self.service.book(self.alice, 16, NOW, 60)
        snap = self.service.snapshot(self.bob)
        slot = next(p for p in snap["pcs"] if p["id"] == 16)["slots"][0]
        self.assertEqual(slot, {"start": NOW, "end": NOW + 3600, "mine": False})
        admin_slot = next(p for p in self.service.snapshot(self.admin)["pcs"] if p["id"] == 16)["slots"][0]
        self.assertEqual(admin_slot["who"], "Alice (@alice)")
        self.assertEqual(self.service.snapshot(self.alice)["me"]["bookings"][0]["price"], 250)

    def test_reminders(self) -> None:
        early = self.service.book(self.alice, 17, NOW + 3600, 60)
        self.service.book(self.alice, 18, NOW + 5 * 60, 60)  # создана внутри окна — без напоминания
        self.clock.now = NOW + 3600 - 10 * 60
        due = self.storage.due_start_reminders(self.clock.now, config.REMIND_BEFORE_START_MIN * 60)
        self.assertEqual([b.id for b in due], [early.id])
        self.storage.mark_reminded(early.id, start=True)
        self.assertEqual(self.storage.due_start_reminders(self.clock.now, 900), [])
        self.clock.now = NOW + 3600 + 55 * 60
        ends = self.storage.due_end_reminders(self.clock.now, config.REMIND_BEFORE_END_MIN * 60)
        self.assertEqual([b.id for b in ends], [early.id])


class InitDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = {"id": 42, "first_name": "Иван", "username": "ivan"}

    def test_valid(self) -> None:
        user = user_from_init_data(TOKEN, sign(self.user, int(time.time())))
        self.assertEqual((user.id, user.name, user.username), (42, "Иван", "ivan"))

    def test_wrong_token(self) -> None:
        with self.assertRaises(BookingError):
            user_from_init_data(TOKEN, sign(self.user, int(time.time()), token="1:OTHER"))

    def test_tampered(self) -> None:
        data = sign(self.user, int(time.time())).replace("ivan", "admin")
        with self.assertRaises(BookingError):
            user_from_init_data(TOKEN, data)

    def test_expired(self) -> None:
        old = int(time.time()) - config.INIT_DATA_TTL_SEC - 60
        with self.assertRaises(BookingError):
            user_from_init_data(TOKEN, sign(self.user, old))


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[str] = []

    def spawn(self, coro) -> None:
        coro.close()

    async def booking_created(self, booking, user) -> None:
        self.events.append("created")

    async def booking_extended(self, booking, user, minutes) -> None:
        self.events.append("extended")

    async def booking_released(self, booking, user) -> None:
        self.events.append("released")


class ApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.storage = Storage(":memory:")
        self.clock = Clock(int(time.time()) // 60 * 60)
        self.service = BookingService(self.storage, clock=self.clock)
        return create_app(self.service, FakeNotifier(), bot_token=TOKEN, allowed_origins={"https://club.example"})

    def auth(self, user_id: int = 42) -> dict:
        return {"Authorization": "tma " + sign({"id": user_id, "first_name": "Test"}, int(time.time()))}

    async def test_state_anonymous(self) -> None:
        resp = await self.client.get("/api/state")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIsNone(data["me"])
        self.assertEqual(len(data["pcs"]), len(config.PCS))

    async def test_booking_flow(self) -> None:
        now = self.clock.now
        resp = await self.client.post("/api/bookings", json={"pc_id": 1, "start": now, "minutes": 75}, headers=self.auth())
        self.assertEqual(resp.status, 201)
        booking = (await resp.json())["booking"]
        self.assertEqual(booking["end"] - booking["start"], 75 * 60)

        resp = await self.client.post("/api/bookings", json={"pc_id": 1, "start": now, "minutes": 30}, headers=self.auth(43))
        self.assertEqual(resp.status, 409)
        self.assertIn("уже занят", (await resp.json())["error"])

        resp = await self.client.post(f"/api/bookings/{booking['id']}/extend", json={"minutes": 15}, headers=self.auth())
        self.assertEqual((await resp.json())["booking"]["end"], booking["end"] + 15 * 60)

        resp = await self.client.delete(f"/api/bookings/{booking['id']}", headers=self.auth(43))
        self.assertEqual(resp.status, 404)
        resp = await self.client.delete(f"/api/bookings/{booking['id']}", headers=self.auth())
        self.assertEqual(resp.status, 200)

    async def test_requires_auth_and_valid_body(self) -> None:
        resp = await self.client.post("/api/bookings", json={"pc_id": 1, "start": 0, "minutes": 60})
        self.assertEqual(resp.status, 401)
        resp = await self.client.post("/api/bookings", data="not json", headers=self.auth())
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/api/bookings", json={"pc_id": "1", "start": 0, "minutes": 60}, headers=self.auth())
        self.assertEqual(resp.status, 400)

    async def test_cors(self) -> None:
        resp = await self.client.options("/api/bookings", headers={"Origin": "https://club.example"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "https://club.example")
        resp = await self.client.get("/api/state", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)


if __name__ == "__main__":
    unittest.main()
