"""Unit tests for Telegram message formatting.

Tests the format_* functions directly — no network calls, no bot token needed.
Odeslani chybove zpravy o burze jde na lokalni testovaci server, ne na Telegram.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import Config
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
from app.notifiers import telegram
from app.notifiers.telegram import (
    ExchangePositions,
    TelegramNotifier,
    format_daily_summary,
    format_exchange_error,
    format_order_filled,
    format_order_opened,
    format_order_updated,
    format_position_closed,
    format_position_opened,
    format_position_updated,
    format_positions_report,
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
# DAILY SUMMARY
# ---------------------------------------------------------------------------

def test_daily_summary_with_positions() -> None:
    msg = format_daily_summary("Extended", "mainnet", [_position()])
    assert "DAILY POSITION SUMMARY" in msg
    assert "📊" in msg
    assert "Extended (mainnet)" in msg
    assert "BTC-USD" in msg
    assert "LONG" in msg
    assert "Open positions: 1" in msg
    assert "Total uPnL" in msg
    assert "+62.38 USDC" in msg


def test_daily_summary_no_positions() -> None:
    msg = format_daily_summary("Extended", "mainnet", [])
    assert "DAILY POSITION SUMMARY" in msg
    assert "No open positions." in msg
    assert "Total uPnL" not in msg


def test_daily_summary_total_upnl_sums_multiple() -> None:
    p1 = _position()  # uPnL = +62.38
    p2 = Position(
        market="ETH-USD",
        exchange="Extended",
        side=PositionSide.SHORT,
        size=Decimal("2.5"),
        entry_price=Decimal("2450.00"),
        unrealized_pnl=Decimal("30.00"),
    )
    msg = format_daily_summary("Extended", "mainnet", [p1, p2])
    assert "Open positions: 2" in msg
    assert "92.38" in msg  # 62.38 + 30.00


def test_daily_summary_no_invalid_html() -> None:
    _no_invalid_tags(format_daily_summary("Extended", "mainnet", [_position()]))


def test_daily_summary_layout() -> None:
    lines = format_daily_summary("Extended", "mainnet", [_position()]).split("\n")
    assert lines[:2] == ["📊 <b>DAILY POSITION SUMMARY</b>", "Exchange: Extended (mainnet)"]
    # lines[2] je aktualni cas
    assert lines[3:] == [
        "",
        "<b>BTC-USD</b> LONG 10x",
        "  Size: 0.25 | Entry: 63250.50",
        "  Mark: 63500.00 | uPnL: +62.38 USDC",
        "",
        "─────────────────────",
        "Total uPnL: <b>+62.38 USDC</b>",
        "Open positions: 1",
    ]


# ---------------------------------------------------------------------------
# /positions — odpoved na prikaz
# ---------------------------------------------------------------------------

def test_positions_report_lists_all_exchanges() -> None:
    msg = format_positions_report([
        ExchangePositions("Extended", "mainnet", [_position()]),
        ExchangePositions("Hyperliquid", "mainnet", []),
    ])
    assert msg.startswith("📊 <b>OPEN POSITIONS</b>")
    assert "Exchange: Extended (mainnet)" in msg
    assert "<b>BTC-USD</b> LONG 10x" in msg
    assert "Total uPnL: <b>+62.38 USDC</b>" in msg
    assert "Exchange: Hyperliquid (mainnet)" in msg
    assert "No open positions." in msg


def test_positions_report_marks_unavailable_exchange() -> None:
    msg = format_positions_report([ExchangePositions("Hyperliquid", "mainnet", None)])
    assert "Exchange: Hyperliquid (mainnet)" in msg
    assert "Data unavailable" in msg
    assert "No open positions." not in msg


def test_positions_report_no_invalid_html() -> None:
    _no_invalid_tags(
        format_positions_report([ExchangePositions("Extended", "mainnet", [_position()])])
    )


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


# ---------------------------------------------------------------------------
# Chyba pripojeni burzy
# ---------------------------------------------------------------------------

_TOKEN = "123456:SECRET-TOKEN"


def test_exchange_error_message_escapes_html_and_truncates() -> None:
    msg = format_exchange_error("Hyperliquid", "mainnet", "<Response [502]> " + "x" * 1000)
    assert msg.startswith("⚠️ <b>checkDEX — exchange connection failed</b>")
    assert "Exchange: Hyperliquid (mainnet)" in msg
    assert "&lt;Response [502]&gt;" in msg
    assert "<Response" not in msg
    assert "x" * 1000 not in msg  # dlouhy text je zkraceny
    _no_invalid_tags(msg)


@pytest.fixture
async def fake_send(monkeypatch):
    """Lokalni server misto api.telegram.org/sendMessage.

    Vraci (prijata tela requestu, HTTP stavy dalsich odpovedi — vychozi 200).
    """
    received: list[dict] = []
    statuses: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        received.append(await request.json())
        status = statuses.pop(0) if statuses else 200
        return web.json_response({"ok": status == 200}, status=status)

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", handler)
    async with TestServer(app) as server:
        url = f"http://{server.host}:{server.port}/bot{{token}}/sendMessage"
        monkeypatch.setattr(telegram, "_API_URL", url)
        yield received, statuses


@pytest.fixture
async def notifier():
    config = Config(_env_file=None, telegram_bot_token=_TOKEN, telegram_chat_id="-1001")
    # Chybova zprava se nededuplikuje, DB tedy neni potreba.
    instance = TelegramNotifier(config, db=None)
    await instance.connect()
    yield instance
    await instance.disconnect()


async def test_send_exchange_error_posts_html_without_token(fake_send, notifier) -> None:
    received, _ = fake_send
    sent = await notifier.send_exchange_error("Hyperliquid", "mainnet", f"url /bot{_TOKEN}/x failed")

    assert sent is True
    body = received[0]
    assert body["parse_mode"] == "HTML"
    assert body["chat_id"] == "-1001"
    assert "exchange connection failed" in body["text"]
    assert _TOKEN not in body["text"]


async def test_send_exchange_error_failure_returns_false_and_hides_token(
    fake_send, notifier, caplog
) -> None:
    _, statuses = fake_send
    statuses.append(400)

    with caplog.at_level("WARNING"):
        sent = await notifier.send_exchange_error("Hyperliquid", "mainnet", "boom")

    assert sent is False
    record = next(r for r in caplog.records if r.getMessage() == "Exchange error notification failed")
    # Chyba aiohttp obsahuje URL s tokenem — v logu musi byt zamaskovany.
    assert "400" in record.error
    assert "***" in record.error
    assert _TOKEN not in record.error
