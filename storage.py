"""Хранилище броней на SQLite.

Все запросы короткие и выполняются в одном потоке event loop, поэтому проверка
пересечений и вставка брони не могут перемешаться с другим запросом.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

ACTIVE = "active"
CANCELLED = "cancelled"
FINISHED = "finished"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pc_id          INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    user_name      TEXT    NOT NULL,
    username       TEXT,
    start_at       INTEGER NOT NULL,
    end_at         INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'active',
    created_at     INTEGER NOT NULL,
    reminded_start INTEGER NOT NULL DEFAULT 0,
    reminded_end   INTEGER NOT NULL DEFAULT 0,
    CHECK (end_at > start_at)
);
CREATE INDEX IF NOT EXISTS idx_bookings_status_end ON bookings (status, end_at);
CREATE INDEX IF NOT EXISTS idx_bookings_pc_start ON bookings (pc_id, start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id, status);
"""


@dataclass(slots=True)
class Booking:
    id: int
    pc_id: int
    user_id: int
    user_name: str
    username: str | None
    start_at: int
    end_at: int
    status: str
    created_at: int
    reminded_start: bool
    reminded_end: bool

    @property
    def minutes(self) -> int:
        return (self.end_at - self.start_at) // 60

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Booking":
        return cls(**dict(row))


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, params: tuple = ()) -> Booking | None:
        row = self.conn.execute(sql, params).fetchone()
        return Booking.from_row(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[Booking]:
        return [Booking.from_row(row) for row in self.conn.execute(sql, params)]

    def get(self, booking_id: int) -> Booking | None:
        return self._one("SELECT * FROM bookings WHERE id = ?", (booking_id,))

    def insert(
        self, *, pc_id: int, user_id: int, user_name: str, username: str | None,
        start_at: int, end_at: int, created_at: int,
    ) -> Booking:
        cur = self.conn.execute(
            "INSERT INTO bookings (pc_id, user_id, user_name, username, start_at, end_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pc_id, user_id, user_name, username, start_at, end_at, created_at),
        )
        return self.get(cur.lastrowid)

    def find_overlap(self, pc_id: int, start_at: int, end_at: int, exclude_id: int | None = None) -> Booking | None:
        return self._one(
            "SELECT * FROM bookings WHERE pc_id = ? AND status = ? AND start_at < ? AND end_at > ?"
            " AND id IS NOT ? ORDER BY start_at LIMIT 1",
            (pc_id, ACTIVE, end_at, start_at, exclude_id),
        )

    def active_between(self, start_at: int, end_at: int) -> list[Booking]:
        return self._all(
            "SELECT * FROM bookings WHERE status = ? AND start_at < ? AND end_at > ? ORDER BY pc_id, start_at",
            (ACTIVE, end_at, start_at),
        )

    def user_active(self, user_id: int, now: int) -> list[Booking]:
        return self._all(
            "SELECT * FROM bookings WHERE user_id = ? AND status = ? AND end_at > ? ORDER BY start_at",
            (user_id, ACTIVE, now),
        )

    def set_end(self, booking_id: int, end_at: int) -> None:
        self.conn.execute(
            "UPDATE bookings SET end_at = ?, reminded_end = 0 WHERE id = ?", (end_at, booking_id)
        )

    def close_booking(self, booking_id: int, status: str, end_at: int | None = None) -> None:
        self.conn.execute(
            "UPDATE bookings SET status = ?, end_at = COALESCE(?, end_at) WHERE id = ?",
            (status, end_at, booking_id),
        )

    def due_start_reminders(self, now: int, before_sec: int) -> list[Booking]:
        # Не напоминаем о брони, созданной уже внутри окна напоминания.
        return self._all(
            "SELECT * FROM bookings WHERE status = ? AND reminded_start = 0"
            " AND start_at > ? AND start_at <= ? AND start_at - created_at > ?",
            (ACTIVE, now, now + before_sec, before_sec),
        )

    def due_end_reminders(self, now: int, before_sec: int) -> list[Booking]:
        return self._all(
            "SELECT * FROM bookings WHERE status = ? AND reminded_end = 0"
            " AND start_at <= ? AND end_at > ? AND end_at <= ?",
            (ACTIVE, now, now, now + before_sec),
        )

    def mark_reminded(self, booking_id: int, *, start: bool = False, end: bool = False) -> None:
        if start:
            self.conn.execute("UPDATE bookings SET reminded_start = 1 WHERE id = ?", (booking_id,))
        if end:
            self.conn.execute("UPDATE bookings SET reminded_end = 1 WHERE id = ?", (booking_id,))
