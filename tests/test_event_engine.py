"""Unit tests for order and position diff / event detection logic.

Phase 3: covers SDK → internal model mapping functions.
Phase 5: order/position diff and race-condition handling tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from x10.perpetual.orders import OpenOrderModel
from x10.perpetual.orders import OrderSide as SdkOrderSide
from x10.perpetual.orders import OrderStatus as SdkOrderStatus
from x10.perpetual.orders import OrderType as SdkOrderType
from x10.perpetual.positions import PositionHistoryModel, PositionModel
from x10.perpetual.positions import PositionSide as SdkPositionSide

from app.exchanges.extended import map_order, map_position, map_position_history
from app.models.events import (
    OrderFilledEvent,
    OrderOpenedEvent,
    OrderUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
)
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.position import Position, PositionSide
from app.models.trade import Trade
from app.services.event_engine import (
    detect_closed_positions,
    detect_order_events,
    detect_position_events,
)

# ---------------------------------------------------------------------------
# Helpers — minimal valid SDK model instances
# ---------------------------------------------------------------------------

_TS_MS = 1746000000000  # 2026-04-30 UTC in milliseconds


def _sdk_order(**overrides) -> OpenOrderModel:
    defaults = dict(
        id=1,
        account_id=99,
        external_id="ext-1",
        market="BTC-USD",
        type=SdkOrderType.LIMIT,
        side=SdkOrderSide.BUY,
        status=SdkOrderStatus.NEW,
        status_reason=None,
        price=Decimal("60000"),
        average_price=None,
        qty=Decimal("0.1"),
        filled_qty=Decimal("0"),
        reduce_only=False,
        post_only=False,
        payed_fee=None,
        created_time=_TS_MS,
        updated_time=_TS_MS,
        expiry_time=None,
    )
    defaults.update(overrides)
    return OpenOrderModel(**defaults)


def _sdk_position(**overrides) -> PositionModel:
    defaults = dict(
        id=10,
        account_id=99,
        market="ETH-USD",
        side=SdkPositionSide.LONG,
        leverage=Decimal("5"),
        size=Decimal("2"),
        value=Decimal("6000"),
        open_price=Decimal("3000"),
        mark_price=Decimal("3050"),
        liquidation_price=Decimal("2500"),
        unrealised_pnl=Decimal("100"),
        realised_pnl=Decimal("0"),
        tp_price=None,
        sl_price=None,
        adl=None,
        created_at=_TS_MS,
        updated_at=_TS_MS,
    )
    defaults.update(overrides)
    return PositionModel(**defaults)


def _sdk_position_history(**overrides) -> PositionHistoryModel:
    defaults = dict(
        id=20,
        account_id=99,
        market="BTC-USD",
        side=SdkPositionSide.LONG,
        leverage=Decimal("10"),
        size=Decimal("0.25"),
        open_price=Decimal("63250.5"),
        exit_type=None,
        exit_price=Decimal("63880"),
        realised_pnl=Decimal("157.38"),
        created_time=_TS_MS,
        closed_time=_TS_MS + 6120000,  # ~102 minutes later
    )
    defaults.update(overrides)
    return PositionHistoryModel(**defaults)


# ---------------------------------------------------------------------------
# map_order tests
# ---------------------------------------------------------------------------

def test_map_order_basic_fields() -> None:
    order = map_order(_sdk_order(), exchange="Extended")
    assert order.id == "1"
    assert order.exchange == "Extended"
    assert order.market == "BTC-USD"
    assert order.side == OrderSide.BUY
    assert order.type == OrderType.LIMIT
    assert order.price == Decimal("60000")
    assert order.qty == Decimal("0.1")
    assert order.filled_qty == Decimal("0")


def test_map_order_status_mapping() -> None:
    cases = [
        (SdkOrderStatus.NEW, OrderStatus.OPEN),
        (SdkOrderStatus.UNTRIGGERED, OrderStatus.OPEN),
        (SdkOrderStatus.UNKNOWN, OrderStatus.OPEN),
        (SdkOrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIAL_FILL),
        (SdkOrderStatus.FILLED, OrderStatus.FILLED),
        (SdkOrderStatus.CANCELLED, OrderStatus.CANCELLED),
        (SdkOrderStatus.EXPIRED, OrderStatus.CANCELLED),
        (SdkOrderStatus.REJECTED, OrderStatus.REJECTED),
    ]
    for sdk_status, expected in cases:
        order = map_order(_sdk_order(status=sdk_status), exchange="Extended")
        assert order.status == expected, f"Expected {expected} for SDK status {sdk_status}"


def test_map_order_type_mapping() -> None:
    cases = [
        (SdkOrderType.LIMIT, OrderType.LIMIT),
        (SdkOrderType.MARKET, OrderType.MARKET),
        (SdkOrderType.CONDITIONAL, OrderType.CONDITIONAL),
        (SdkOrderType.TPSL, OrderType.TPSL),
    ]
    for sdk_type, expected in cases:
        order = map_order(_sdk_order(type=sdk_type), exchange="Extended")
        assert order.type == expected, f"Expected {expected} for SDK type {sdk_type}"


def test_map_order_timestamps_are_utc() -> None:
    order = map_order(_sdk_order(created_time=_TS_MS, updated_time=_TS_MS), exchange="Extended")
    assert order.created_at.tzinfo == timezone.utc
    assert order.updated_at is not None
    assert order.updated_at.tzinfo == timezone.utc


def test_map_order_sell_side() -> None:
    order = map_order(_sdk_order(side=SdkOrderSide.SELL), exchange="Extended")
    assert order.side == OrderSide.SELL


def test_map_order_partial_fill_qty() -> None:
    order = map_order(
        _sdk_order(status=SdkOrderStatus.PARTIALLY_FILLED, filled_qty=Decimal("0.05")),
        exchange="Extended",
    )
    assert order.filled_qty == Decimal("0.05")
    assert order.status == OrderStatus.PARTIAL_FILL


# ---------------------------------------------------------------------------
# map_position tests
# ---------------------------------------------------------------------------

def test_map_position_basic_fields() -> None:
    pos = map_position(_sdk_position(), exchange="Extended")
    assert pos.exchange == "Extended"
    assert pos.market == "ETH-USD"
    assert pos.side == PositionSide.LONG
    assert pos.size == Decimal("2")
    assert pos.entry_price == Decimal("3000")
    assert pos.mark_price == Decimal("3050")
    assert pos.leverage == Decimal("5")
    assert pos.unrealized_pnl == Decimal("100")


def test_map_position_short_side() -> None:
    pos = map_position(_sdk_position(side=SdkPositionSide.SHORT), exchange="Extended")
    assert pos.side == PositionSide.SHORT


def test_map_position_timestamp_is_utc() -> None:
    pos = map_position(_sdk_position(), exchange="Extended")
    assert pos.opened_at is not None
    assert pos.opened_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# map_position_history tests
# ---------------------------------------------------------------------------

def test_map_position_history_basic_fields() -> None:
    trade = map_position_history(_sdk_position_history(), exchange="Extended")
    assert trade.id == "20"
    assert trade.exchange == "Extended"
    assert trade.market == "BTC-USD"
    assert trade.side == PositionSide.LONG
    assert trade.size == Decimal("0.25")
    assert trade.entry_price == Decimal("63250.5")
    assert trade.exit_price == Decimal("63880")
    assert trade.realised_pnl == Decimal("157.38")


def test_map_position_history_timestamps_are_utc() -> None:
    trade = map_position_history(_sdk_position_history(), exchange="Extended")
    assert trade.closed_at.tzinfo == timezone.utc
    assert trade.opened_at is not None
    assert trade.opened_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Helpers for Phase 5 tests
# ---------------------------------------------------------------------------

_TS = datetime(2026, 5, 7, 13, 42, 11, tzinfo=timezone.utc)


def _order(id: str = "1001", status: OrderStatus = OrderStatus.OPEN, filled_qty: str = "0") -> Order:
    return Order(
        id=id,
        exchange="Extended",
        market="BTC-USD",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        price=Decimal("60000"),
        qty=Decimal("0.1"),
        filled_qty=Decimal(filled_qty),
        status=status,
        created_at=_TS,
        updated_at=_TS,
    )


def _position(market: str = "BTC-USD", size: str = "0.25") -> Position:
    return Position(
        market=market,
        exchange="Extended",
        side=PositionSide.LONG,
        size=Decimal(size),
        entry_price=Decimal("63250.5"),
        mark_price=Decimal("63500"),
        leverage=Decimal("10"),
        unrealized_pnl=Decimal("62.38"),
        opened_at=_TS,
    )


def _trade(id: str = "5001", pnl: str = "157.38") -> Trade:
    return Trade(
        id=id,
        exchange="Extended",
        market="BTC-USD",
        side=PositionSide.LONG,
        size=Decimal("0.25"),
        entry_price=Decimal("63250.5"),
        exit_price=Decimal("63880.0"),
        realised_pnl=Decimal(pnl),
        opened_at=_TS,
        closed_at=_TS,
    )


# ---------------------------------------------------------------------------
# detect_order_events — new orders
# ---------------------------------------------------------------------------

def test_detect_order_events_new_order() -> None:
    current = [_order("1")]
    events, pending = detect_order_events(previous={}, current=current, history=[])
    assert len(events) == 1
    assert isinstance(events[0], OrderOpenedEvent)
    assert events[0].order.id == "1"


def test_detect_order_events_no_event_when_unchanged() -> None:
    o = _order("1")
    events, pending = detect_order_events(
        previous={"1": o}, current=[o], history=[]
    )
    assert events == []
    assert pending == []


def test_detect_order_events_filled_event() -> None:
    prev = _order("1", status=OrderStatus.OPEN)
    curr = _order("1", status=OrderStatus.FILLED, filled_qty="0.1")
    events, pending = detect_order_events(
        previous={"1": prev}, current=[curr], history=[]
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderFilledEvent)


def test_detect_order_events_partial_fill_is_updated() -> None:
    prev = _order("1", status=OrderStatus.OPEN, filled_qty="0")
    curr = _order("1", status=OrderStatus.PARTIAL_FILL, filled_qty="0.05")
    events, pending = detect_order_events(
        previous={"1": prev}, current=[curr], history=[]
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderUpdatedEvent)


# ---------------------------------------------------------------------------
# detect_order_events — disappeared orders
# ---------------------------------------------------------------------------

def test_detect_order_events_disappeared_found_in_history_filled() -> None:
    prev = _order("1")
    hist = _order("1", status=OrderStatus.FILLED, filled_qty="0.1")
    events, pending = detect_order_events(
        previous={"1": prev}, current=[], history=[hist]
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderFilledEvent)
    assert pending == []


def test_detect_order_events_disappeared_found_in_history_cancelled() -> None:
    prev = _order("1")
    hist = _order("1", status=OrderStatus.CANCELLED)
    events, pending = detect_order_events(
        previous={"1": prev}, current=[], history=[hist]
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderUpdatedEvent)
    assert events[0].order.status == OrderStatus.CANCELLED


def test_detect_order_events_disappeared_not_in_history_goes_pending() -> None:
    prev = _order("1")
    events, pending = detect_order_events(
        previous={"1": prev}, current=[], history=[]
    )
    assert events == []
    assert "1" in pending


def test_detect_order_events_multiple_new_orders() -> None:
    current = [_order("1"), _order("2"), _order("3")]
    events, pending = detect_order_events(previous={}, current=current, history=[])
    assert len(events) == 3
    assert all(isinstance(e, OrderOpenedEvent) for e in events)


# ---------------------------------------------------------------------------
# detect_position_events
# ---------------------------------------------------------------------------

def test_detect_position_events_new_position() -> None:
    current = [_position("BTC-USD")]
    events = detect_position_events(previous={}, current=current)
    assert len(events) == 1
    assert isinstance(events[0], PositionOpenedEvent)


def test_detect_position_events_no_event_when_size_unchanged() -> None:
    pos = _position("BTC-USD", size="0.25")
    events = detect_position_events(previous={"BTC-USD": pos}, current=[pos])
    assert events == []


def test_detect_position_events_size_change_emits_updated() -> None:
    prev = _position("BTC-USD", size="0.25")
    curr = _position("BTC-USD", size="0.50")
    events = detect_position_events(previous={"BTC-USD": prev}, current=[curr])
    assert len(events) == 1
    assert isinstance(events[0], PositionUpdatedEvent)
    assert events[0].position.size == Decimal("0.50")
    assert events[0].previous.size == Decimal("0.25")


def test_detect_position_events_multiple_positions() -> None:
    current = [_position("BTC-USD"), _position("ETH-USD")]
    events = detect_position_events(previous={}, current=current)
    assert len(events) == 2
    assert all(isinstance(e, PositionOpenedEvent) for e in events)


def test_detect_position_events_only_size_triggers_update_not_pnl() -> None:
    prev = _position("BTC-USD", size="0.25")
    curr = Position(
        market="BTC-USD",
        exchange="Extended",
        side=PositionSide.LONG,
        size=Decimal("0.25"),      # same size
        entry_price=Decimal("63250.5"),
        mark_price=Decimal("99999.0"),  # mark price changed
        leverage=Decimal("10"),
        unrealized_pnl=Decimal("9000.00"),  # pnl changed dramatically
        opened_at=_TS,
    )
    events = detect_position_events(previous={"BTC-USD": prev}, current=[curr])
    assert events == []


# ---------------------------------------------------------------------------
# detect_closed_positions
# ---------------------------------------------------------------------------

def test_detect_closed_positions_returns_new_trades() -> None:
    trades = [_trade("5001"), _trade("5002")]
    events = detect_closed_positions(recent_history=trades, already_notified=set())
    assert len(events) == 2
    assert all(isinstance(e, PositionClosedEvent) for e in events)


def test_detect_closed_positions_skips_already_notified() -> None:
    trades = [_trade("5001"), _trade("5002")]
    already = {"position_closed:Extended:5001"}
    events = detect_closed_positions(recent_history=trades, already_notified=already)
    assert len(events) == 1
    assert events[0].trade.id == "5002"


def test_detect_closed_positions_all_notified_returns_empty() -> None:
    trades = [_trade("5001"), _trade("5002")]
    already = {"position_closed:Extended:5001", "position_closed:Extended:5002"}
    events = detect_closed_positions(recent_history=trades, already_notified=already)
    assert events == []


def test_detect_closed_positions_empty_history() -> None:
    events = detect_closed_positions(recent_history=[], already_notified=set())
    assert events == []
