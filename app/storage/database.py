from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

from app.models.order import Order
from app.models.position import Position

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_snapshots (
    order_id  TEXT NOT NULL,
    exchange  TEXT NOT NULL,
    data      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (order_id, exchange)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    market    TEXT NOT NULL,
    exchange  TEXT NOT NULL,
    data      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (market, exchange)
);

CREATE TABLE IF NOT EXISTS sent_notifications (
    notification_id TEXT PRIMARY KEY,
    sent_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_notifications_sent_at
    ON sent_notifications(sent_at);

CREATE TABLE IF NOT EXISTS disappeared_pending (
    order_id       TEXT NOT NULL,
    exchange       TEXT NOT NULL,
    data           TEXT NOT NULL,
    disappeared_at TEXT NOT NULL,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, exchange)
);

CREATE TABLE IF NOT EXISTS history_cursors (
    cursor_key   TEXT PRIMARY KEY,
    cursor_value TEXT,
    updated_at   TEXT NOT NULL
);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DisappearedOrder:
    order: Order
    disappeared_at: datetime
    retry_count: int


class Database:
    """SQLite persistence layer for checkDEX.

    All datetimes stored as ISO 8601 UTC strings.
    All model data stored as Pydantic JSON.
    """

    def __init__(self, db_path: str, dedup_ttl_days: int = 30) -> None:
        self._db_path = db_path
        self._dedup_ttl_days = dedup_ttl_days
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        deleted = await self._cleanup_old_notifications()
        logger.info(
            "Database connected",
            extra={"path": self._db_path, "dedup_cleaned": deleted},
        )

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Database disconnected")

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before use")
        return self._conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        await self._db().executescript(_DDL)
        async with self._db().execute("SELECT version FROM schema_version") as cur:
            row = await cur.fetchone()
        if row is None:
            await self._db().execute(
                "INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,)
            )
        await self._db().commit()

    # ------------------------------------------------------------------
    # Order snapshots
    # ------------------------------------------------------------------

    async def get_order_snapshots(self, exchange: str) -> dict[str, Order]:
        """Return {order_id: Order} snapshot for the given exchange."""
        async with self._db().execute(
            "SELECT order_id, data FROM order_snapshots WHERE exchange = ?", (exchange,)
        ) as cur:
            rows = await cur.fetchall()
        return {row["order_id"]: Order.model_validate_json(row["data"]) for row in rows}

    async def replace_order_snapshots(self, exchange: str, orders: list[Order]) -> None:
        """Atomically replace the full order snapshot for an exchange."""
        now = _now_utc()
        db = self._db()
        try:
            await db.execute("DELETE FROM order_snapshots WHERE exchange = ?", (exchange,))
            await db.executemany(
                "INSERT INTO order_snapshots(order_id, exchange, data, updated_at) VALUES (?,?,?,?)",
                [(o.id, exchange, o.model_dump_json(), now) for o in orders],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ------------------------------------------------------------------
    # Position snapshots
    # ------------------------------------------------------------------

    async def get_position_snapshots(self, exchange: str) -> dict[str, Position]:
        """Return {market: Position} snapshot for the given exchange."""
        async with self._db().execute(
            "SELECT market, data FROM position_snapshots WHERE exchange = ?", (exchange,)
        ) as cur:
            rows = await cur.fetchall()
        return {row["market"]: Position.model_validate_json(row["data"]) for row in rows}

    async def replace_position_snapshots(self, exchange: str, positions: list[Position]) -> None:
        """Atomically replace the full position snapshot for an exchange."""
        now = _now_utc()
        db = self._db()
        try:
            await db.execute("DELETE FROM position_snapshots WHERE exchange = ?", (exchange,))
            await db.executemany(
                "INSERT INTO position_snapshots(market, exchange, data, updated_at) VALUES (?,?,?,?)",
                [(p.market, exchange, p.model_dump_json(), now) for p in positions],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ------------------------------------------------------------------
    # Notification deduplication
    # ------------------------------------------------------------------

    async def is_notified(self, notification_id: str) -> bool:
        """Return True if this notification was already sent."""
        async with self._db().execute(
            "SELECT 1 FROM sent_notifications WHERE notification_id = ?", (notification_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_notified(self, notification_id: str) -> None:
        """Record that a notification was sent."""
        await self._db().execute(
            "INSERT OR IGNORE INTO sent_notifications(notification_id, sent_at) VALUES (?,?)",
            (notification_id, _now_utc()),
        )
        await self._db().commit()

    async def _cleanup_old_notifications(self) -> int:
        """Delete sent_notifications older than dedup_ttl_days. Returns deleted count."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._dedup_ttl_days)
        ).isoformat()
        async with self._db().execute(
            "SELECT COUNT(*) FROM sent_notifications WHERE sent_at < ?", (cutoff,)
        ) as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
        if count:
            await self._db().execute(
                "DELETE FROM sent_notifications WHERE sent_at < ?", (cutoff,)
            )
            await self._db().commit()
            logger.info("Cleaned up old notifications", extra={"deleted": count})
        return count

    # ------------------------------------------------------------------
    # Disappeared pending orders
    # ------------------------------------------------------------------

    async def get_disappeared_pending(self, exchange: str) -> list[DisappearedOrder]:
        """Return orders that disappeared from open_orders and are awaiting history lookup."""
        async with self._db().execute(
            "SELECT data, disappeared_at, retry_count FROM disappeared_pending WHERE exchange = ?",
            (exchange,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            DisappearedOrder(
                order=Order.model_validate_json(row["data"]),
                disappeared_at=datetime.fromisoformat(row["disappeared_at"]),
                retry_count=row["retry_count"],
            )
            for row in rows
        ]

    async def add_disappeared_pending(self, order: Order) -> None:
        await self._db().execute(
            "INSERT OR IGNORE INTO disappeared_pending(order_id, exchange, data, disappeared_at, retry_count)"
            " VALUES (?,?,?,?,0)",
            (order.id, order.exchange, order.model_dump_json(), _now_utc()),
        )
        await self._db().commit()

    async def increment_disappeared_retry(self, order_id: str, exchange: str) -> int:
        """Increment retry count and return the new value."""
        await self._db().execute(
            "UPDATE disappeared_pending SET retry_count = retry_count + 1 WHERE order_id = ? AND exchange = ?",
            (order_id, exchange),
        )
        await self._db().commit()
        async with self._db().execute(
            "SELECT retry_count FROM disappeared_pending WHERE order_id = ? AND exchange = ?",
            (order_id, exchange),
        ) as cur:
            row = await cur.fetchone()
        return row["retry_count"] if row else 0

    async def remove_disappeared_pending(self, order_id: str, exchange: str) -> None:
        await self._db().execute(
            "DELETE FROM disappeared_pending WHERE order_id = ? AND exchange = ?",
            (order_id, exchange),
        )
        await self._db().commit()

    # ------------------------------------------------------------------
    # History cursors
    # ------------------------------------------------------------------

    async def get_cursor(self, key: str) -> str | None:
        """Return stored cursor value for a history endpoint key, or None."""
        async with self._db().execute(
            "SELECT cursor_value FROM history_cursors WHERE cursor_key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["cursor_value"] if row else None

    async def set_cursor(self, key: str, value: str) -> None:
        await self._db().execute(
            "INSERT INTO history_cursors(cursor_key, cursor_value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(cursor_key) DO UPDATE SET cursor_value=excluded.cursor_value, updated_at=excluded.updated_at",
            (key, value, _now_utc()),
        )
        await self._db().commit()
