"""Unit tests for Telegram message formatting.

Tests the format_* functions directly — no network calls, no bot token needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

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
from app.notifiers.telegram import (
    format_order_filled,
    format_order_opened,
    format_order_updated,
    format_position_closed,
    format_position_opened,
    format_position_updated,
)

_TS = datetime(2026, 5, 7, 13, 42, 11, tzinfo=timezone.utc)

_INVALID_HTML_TAGS = ["<div", "<span", "<p>", "<br>", "style=", "color:"]


def _order(status=OrderStatus.OPEN, filled_qty="0") -> Order:
    return Order(
        id="1001",
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


def _position(size="0.25") -> Position:
    return Position(
        market="BTC-USD",
        exchange="Extended",
        side=PositionSide.LONG,
        size=Decimal(size),
        entry_price=Decimal("63250.5"),
        mark_price=Decimal("63500"),
        leverage=Decimal("10"),
        unrealized_pnl=Decimal("62.38"),
        opened_at=_TS,
    )


def _trade(pnl: str) -> Trade:
    return Trade(
        id="5001",
        exchange="Extended",
        market="BTC-USD",
        side=PositionSide.LONG,
        size=Decimal("0.25"),
        entry_price=Decimal("63250.5"),
        exit_price=Decimal("63880.0"),
        realised_pnl=Decimal(pnl),
        opened_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        closed_at=_TS,
    )


def _no_invalid_tags(text: str) -> None:
    for tag in _INVALID_HTML_TAGS:
        assert tag not in text, f"Invalid HTML tag found: {tag!r} in:\n{text}"


# ---------------------------------------------------------------------------
# ORDER OPENED
# ---------------------------------------------------------------------------

def test_order_opened_contains_required_fields() -> None:
    msg = format_order_opened(OrderOpenedEvent(order=_order()))
    assert "ORDER OPENED" in msg
    assert "BTC-USD" in msg
    assert "BUY" in msg
    assert "LIMIT" in msg
    assert "60000" in msg
    assert "1001" in msg


def test_order_opened_no_invalid_html() -> None:
    _no_invalid_tags(format_order_opened(OrderOpenedEvent(order=_order())))


# ---------------------------------------------------------------------------
# ORDER UPDATED
# ---------------------------------------------------------------------------

def test_order_updated_contains_status() -> None:
    order = _order(status=OrderStatus.PARTIAL_FILL, filled_qty="0.05")
    msg = format_order_updated(OrderUpdatedEvent(order=order, previous=_order()))
    assert "ORDER UPDATED" in msg
    assert "PARTIAL_FILL" in msg


def test_order_updated_no_invalid_html() -> None:
    order = _order(status=OrderStatus.CANCELLED)
    _no_invalid_tags(format_order_updated(OrderUpdatedEvent(order=order, previous=_order())))


# ---------------------------------------------------------------------------
# ORDER FILLED
# ---------------------------------------------------------------------------

def test_order_filled_message() -> None:
    order = _order(status=OrderStatus.FILLED, filled_qty="0.1")
    msg = format_order_filled(OrderFilledEvent(order=order, previous=_order()))
    assert "ORDER FILLED" in msg
    assert "✅" in msg
    assert "0.1" in msg


def test_order_filled_no_invalid_html() -> None:
    order = _order(status=OrderStatus.FILLED, filled_qty="0.1")
    _no_invalid_tags(format_order_filled(OrderFilledEvent(order=order, previous=_order())))


# ---------------------------------------------------------------------------
# POSITION OPENED
# ---------------------------------------------------------------------------

def test_position_opened_message() -> None:
    msg = format_position_opened(PositionOpenedEvent(position=_position()))
    assert "POSITION OPENED" in msg
    assert "📈" in msg
    assert "BTC-USD" in msg
    assert "LONG" in msg
    assert "10x" in msg


def test_position_opened_no_invalid_html() -> None:
    _no_invalid_tags(format_position_opened(PositionOpenedEvent(position=_position())))


# ---------------------------------------------------------------------------
# POSITION UPDATED
# ---------------------------------------------------------------------------

def test_position_updated_shows_size_change() -> None:
    prev = _position(size="0.25")
    current = _position(size="0.50")
    msg = format_position_updated(PositionUpdatedEvent(position=current, previous=prev))
    assert "POSITION UPDATED" in msg
    assert "0.50" in msg
    assert "0.25" in msg  # previous size


def test_position_updated_no_invalid_html() -> None:
    prev = _position(size="0.25")
    current = _position(size="0.50")
    _no_invalid_tags(format_position_updated(PositionUpdatedEvent(position=current, previous=prev)))


# ---------------------------------------------------------------------------
# POSITION CLOSED — PROFIT
# ---------------------------------------------------------------------------

def test_position_closed_profit_emoji_and_label() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("157.38")))
    assert "🟢" in msg
    assert "PROFIT" in msg
    assert "POSITION CLOSED" in msg


def test_position_closed_profit_pnl_positive_sign() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("157.38")))
    assert "+157.38 USDC" in msg


def test_position_closed_profit_has_bold_pnl() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("157.38")))
    assert "<b>" in msg
    assert "+157.38" in msg


def test_position_closed_profit_has_pct() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("157.38")))
    assert "%" in msg
    assert "approx" in msg


# ---------------------------------------------------------------------------
# POSITION CLOSED — LOSS
# ---------------------------------------------------------------------------

def test_position_closed_loss_emoji_and_label() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("-92.14")))
    assert "🔴" in msg
    assert "LOSS" in msg


def test_position_closed_loss_pnl_negative_sign() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("-92.14")))
    assert "-92.14 USDC" in msg


# ---------------------------------------------------------------------------
# POSITION CLOSED — BREAKEVEN
# ---------------------------------------------------------------------------

def test_position_closed_breakeven() -> None:
    msg = format_position_closed(PositionClosedEvent(trade=_trade("0")))
    assert "⚪" in msg
    assert "BREAKEVEN" in msg


# ---------------------------------------------------------------------------
# Global HTML validity
# ---------------------------------------------------------------------------

def test_all_formats_no_invalid_html() -> None:
    trade_profit = _trade("157.38")
    trade_loss = _trade("-92.14")
    msgs = [
        format_position_closed(PositionClosedEvent(trade=trade_profit)),
        format_position_closed(PositionClosedEvent(trade=trade_loss)),
        format_position_opened(PositionOpenedEvent(position=_position())),
        format_position_updated(
            PositionUpdatedEvent(position=_position("0.5"), previous=_position("0.25"))
        ),
    ]
    for msg in msgs:
        _no_invalid_tags(msg)
