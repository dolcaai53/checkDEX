"""Unit tests for SQLite persistence layer.

All tests use a real SQLite file in a tmp_path — no mocking of the DB itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.position import Position, PositionSide
from app.storage.database import Database, DisappearedOrder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"), dedup_ttl_days=30)
    await database.connect()
    yield database
    await database.disconnect()


def _order(order_id: str = "order-1", exchange: str = "Extended") -> Order:
    return Order(
        id=order_id,
        exchange=exchange,
        market="BTC-USD",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        price=Decimal("60000"),
        qty=Decimal("0.1"),
        filled_qty=Decimal("0"),
        status=OrderStatus.OPEN,
        created_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )


def _position(market: str = "BTC-USD", exchange: str = "Extended") -> Position:
    return Position(
        market=market,
        exchange=exchange,
        side=PositionSide.LONG,
        size=Decimal("0.5"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("61000"),
        unrealized_pnl=Decimal("500"),
        opened_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Order snapshot tests
# ---------------------------------------------------------------------------

async def test_order_snapshots_empty_on_start(db: Database) -> None:
    result = await db.get_order_snapshots("Extended")
    assert result == {}


async def test_order_snapshots_roundtrip(db: Database) -> None:
    orders = [_order("o-1"), _order("o-2")]
    await db.replace_order_snapshots("Extended", orders)

    result = await db.get_order_snapshots("Extended")
    assert set(result.keys()) == {"o-1", "o-2"}
    assert result["o-1"].market == "BTC-USD"
    assert result["o-1"].price == Decimal("60000")


async def test_order_snapshots_replace_is_atomic(db: Database) -> None:
    await db.replace_order_snapshots("Extended", [_order("o-old")])
    await db.replace_order_snapshots("Extended", [_order("o-new")])

    result = await db.get_order_snapshots("Extended")
    assert list(result.keys()) == ["o-new"]


async def test_order_snapshots_isolated_per_exchange(db: Database) -> None:
    await db.replace_order_snapshots("Extended", [_order("o-1", exchange="Extended")])
    await db.replace_order_snapshots("Lighter", [_order("o-2", exchange="Lighter")])

    ext = await db.get_order_snapshots("Extended")
    light = await db.get_order_snapshots("Lighter")
    assert list(ext.keys()) == ["o-1"]
    assert list(light.keys()) == ["o-2"]


# ---------------------------------------------------------------------------
# Position snapshot tests
# ---------------------------------------------------------------------------

async def test_position_snapshots_empty_on_start(db: Database) -> None:
    result = await db.get_position_snapshots("Extended")
    assert result == {}


async def test_position_snapshots_roundtrip(db: Database) -> None:
    positions = [_position("BTC-USD"), _position("ETH-USD")]
    await db.replace_position_snapshots("Extended", positions)

    result = await db.get_position_snapshots("Extended")
    assert set(result.keys()) == {"BTC-USD", "ETH-USD"}
    assert result["BTC-USD"].size == Decimal("0.5")


async def test_position_snapshots_replace_clears_old(db: Database) -> None:
    await db.replace_position_snapshots("Extended", [_position("BTC-USD")])
    await db.replace_position_snapshots("Extended", [_position("ETH-USD")])

    result = await db.get_position_snapshots("Extended")
    assert "BTC-USD" not in result
    assert "ETH-USD" in result


# ---------------------------------------------------------------------------
# Notification deduplication tests
# ---------------------------------------------------------------------------

async def test_dedup_not_notified_initially(db: Database) -> None:
    assert not await db.is_notified("order_opened:extended:o-1")


async def test_dedup_mark_and_check(db: Database) -> None:
    nid = "order_opened:extended:o-1"
    await db.mark_notified(nid)
    assert await db.is_notified(nid)


async def test_dedup_different_ids_independent(db: Database) -> None:
    await db.mark_notified("event:a")
    assert not await db.is_notified("event:b")


async def test_dedup_mark_idempotent(db: Database) -> None:
    nid = "event:x"
    await db.mark_notified(nid)
    await db.mark_notified(nid)  # should not raise
    assert await db.is_notified(nid)


async def test_dedup_ttl_cleanup(tmp_path) -> None:
    db = Database(db_path=str(tmp_path / "ttl.db"), dedup_ttl_days=0)
    await db.connect()
    await db.mark_notified("old:event")
    # With ttl=0, cleanup at next connect should remove it
    await db.disconnect()

    db2 = Database(db_path=str(tmp_path / "ttl.db"), dedup_ttl_days=0)
    await db2.connect()
    # Record is older than 0 days — should be cleaned
    assert not await db2.is_notified("old:event")
    await db2.disconnect()


# ---------------------------------------------------------------------------
# Disappeared pending tests
# ---------------------------------------------------------------------------

async def test_disappeared_pending_empty_on_start(db: Database) -> None:
    result = await db.get_disappeared_pending("Extended")
    assert result == []


async def test_disappeared_pending_add_and_retrieve(db: Database) -> None:
    order = _order("o-gone")
    await db.add_disappeared_pending(order)

    result = await db.get_disappeared_pending("Extended")
    assert len(result) == 1
    assert isinstance(result[0], DisappearedOrder)
    assert result[0].order.id == "o-gone"
    assert result[0].retry_count == 0


async def test_disappeared_pending_increment_retry(db: Database) -> None:
    await db.add_disappeared_pending(_order("o-retry"))
    count1 = await db.increment_disappeared_retry("o-retry", "Extended")
    count2 = await db.increment_disappeared_retry("o-retry", "Extended")
    assert count1 == 1
    assert count2 == 2


async def test_disappeared_pending_remove(db: Database) -> None:
    await db.add_disappeared_pending(_order("o-rm"))
    await db.remove_disappeared_pending("o-rm", "Extended")
    result = await db.get_disappeared_pending("Extended")
    assert result == []


async def test_disappeared_pending_add_idempotent(db: Database) -> None:
    await db.add_disappeared_pending(_order("o-dup"))
    await db.add_disappeared_pending(_order("o-dup"))  # should not raise
    result = await db.get_disappeared_pending("Extended")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# History cursor tests
# ---------------------------------------------------------------------------

async def test_cursor_none_initially(db: Database) -> None:
    assert await db.get_cursor("orders_history:Extended") is None


async def test_cursor_set_and_get(db: Database) -> None:
    await db.set_cursor("orders_history:Extended", "12345")
    assert await db.get_cursor("orders_history:Extended") == "12345"


async def test_cursor_overwrite(db: Database) -> None:
    await db.set_cursor("key", "v1")
    await db.set_cursor("key", "v2")
    assert await db.get_cursor("key") == "v2"


async def test_cursor_isolated_per_key(db: Database) -> None:
    await db.set_cursor("orders_history:Extended", "aaa")
    assert await db.get_cursor("positions_history:Extended") is None
