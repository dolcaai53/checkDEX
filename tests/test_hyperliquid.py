"""Unit tests for HyperliquidAdapter mapper functions.

All tests use static dicts mirroring real Hyperliquid API responses.
No network calls, no SDK initialization required.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.exchanges.hyperliquid import (
    _normalize_market,
    map_fill_to_trade,
    map_order,
    map_position,
)
from app.models.order import OrderSide, OrderStatus, OrderType
from app.models.position import PositionSide

EXCHANGE = "Hyperliquid"


# ---------------------------------------------------------------------------
# _normalize_market
# ---------------------------------------------------------------------------

def test_normalize_market_btc():
    assert _normalize_market("BTC") == "BTC-USDC"


def test_normalize_market_eth():
    assert _normalize_market("ETH") == "ETH-USDC"


def test_normalize_market_sol():
    assert _normalize_market("SOL") == "SOL-USDC"


# ---------------------------------------------------------------------------
# map_order
# ---------------------------------------------------------------------------

def _open_order(
    coin="BTC",
    side="B",
    limit_px="63250.5",
    sz="0.01",
    orig_sz="0.01",
    oid=12345,
    ts=1715000000000,
) -> dict:
    return {
        "order": {
            "coin": coin,
            "side": side,
            "limitPx": limit_px,
            "sz": sz,
            "origSz": orig_sz,
            "oid": oid,
            "timestamp": ts,
        },
        "status": "open",
        "statusTimestamp": ts,
    }


def test_map_order_buy():
    order = map_order(_open_order(side="B"), EXCHANGE)
    assert order.side == OrderSide.BUY
    assert order.id == "12345"
    assert order.market == "BTC-USDC"
    assert order.exchange == EXCHANGE
    assert order.type == OrderType.LIMIT
    assert order.price == Decimal("63250.5")
    assert order.qty == Decimal("0.01")
    assert order.filled_qty == Decimal("0")
    assert order.status == OrderStatus.OPEN


def test_map_order_sell():
    order = map_order(_open_order(side="A"), EXCHANGE)
    assert order.side == OrderSide.SELL


def test_map_order_partial_fill():
    # origSz=0.02, remaining sz=0.01 → filled_qty=0.01
    order = map_order(_open_order(sz="0.01", orig_sz="0.02"), EXCHANGE)
    assert order.filled_qty == Decimal("0.01")
    assert order.qty == Decimal("0.02")
    assert order.status == OrderStatus.PARTIAL_FILL


def test_map_order_market_order():
    # Market orders have limitPx = "0"
    order = map_order(_open_order(limit_px="0"), EXCHANGE)
    assert order.price is None
    assert order.type == OrderType.MARKET


def test_map_order_id_as_string():
    order = map_order(_open_order(oid=99999), EXCHANGE)
    assert order.id == "99999"
    assert isinstance(order.id, str)


# ---------------------------------------------------------------------------
# map_position
# ---------------------------------------------------------------------------

def _asset_position(
    coin="ETH",
    szi="1.5",
    entry_px="3100.0",
    leverage_val=5,
    unrealized_pnl="75.0",
) -> dict:
    return {
        "position": {
            "coin": coin,
            "szi": szi,
            "entryPx": entry_px,
            "leverage": {"type": "cross", "value": leverage_val},
            "unrealizedPnl": unrealized_pnl,
            "positionValue": "4725.0",
        },
        "type": "oneWay",
    }


def test_map_position_long():
    pos = map_position(_asset_position(szi="1.5"), EXCHANGE)
    assert pos.side == PositionSide.LONG
    assert pos.size == Decimal("1.5")
    assert pos.market == "ETH-USDC"
    assert pos.exchange == EXCHANGE
    assert pos.entry_price == Decimal("3100.0")
    assert pos.leverage == Decimal("5")
    assert pos.unrealized_pnl == Decimal("75.0")


def test_map_position_short():
    pos = map_position(_asset_position(szi="-0.5"), EXCHANGE)
    assert pos.side == PositionSide.SHORT
    assert pos.size == Decimal("0.5")  # abs value


def test_map_position_no_leverage():
    data = _asset_position()
    data["position"]["leverage"] = {}
    pos = map_position(data, EXCHANGE)
    assert pos.leverage is None


def test_map_position_no_unrealized_pnl():
    data = _asset_position()
    del data["position"]["unrealizedPnl"]
    pos = map_position(data, EXCHANGE)
    assert pos.unrealized_pnl is None


# ---------------------------------------------------------------------------
# map_fill_to_trade
# ---------------------------------------------------------------------------

def _close_fill(
    coin="BTC",
    px="63880.0",
    sz="0.01",
    side="A",
    dir_str="Close Long",
    closed_pnl="6.295",
    ts=1715006100000,
    tid=789,
) -> dict:
    return {
        "coin": coin,
        "px": px,
        "sz": sz,
        "side": side,
        "time": ts,
        "dir": dir_str,
        "closedPnl": closed_pnl,
        "hash": "0xabc",
        "oid": 12345,
        "tid": tid,
        "fee": "0.0631",
    }


def test_map_fill_profit():
    trade = map_fill_to_trade(_close_fill(closed_pnl="6.295"), EXCHANGE)
    assert trade.realised_pnl == Decimal("6.295")
    assert trade.side == PositionSide.LONG
    assert trade.market == "BTC-USDC"
    assert trade.exchange == EXCHANGE
    assert trade.id == "789"
    assert trade.exit_price == Decimal("63880.0")
    assert trade.size == Decimal("0.01")


def test_map_fill_loss():
    trade = map_fill_to_trade(_close_fill(closed_pnl="-3.72"), EXCHANGE)
    assert trade.realised_pnl == Decimal("-3.72")


def test_map_fill_close_long_side():
    trade = map_fill_to_trade(_close_fill(dir_str="Close Long"), EXCHANGE)
    assert trade.side == PositionSide.LONG


def test_map_fill_close_short_side():
    trade = map_fill_to_trade(_close_fill(dir_str="Close Short", side="B"), EXCHANGE)
    assert trade.side == PositionSide.SHORT


def test_map_fill_id_uses_tid():
    trade = map_fill_to_trade(_close_fill(tid=42), EXCHANGE)
    assert trade.id == "42"


def test_map_fill_entry_price_equals_exit():
    # entry_price is approximated as exit_price (no entry in fill data)
    trade = map_fill_to_trade(_close_fill(px="63880.0"), EXCHANGE)
    assert trade.entry_price == trade.exit_price == Decimal("63880.0")
