from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

from app.config import Config
from app.models.events import (
    OrderFilledEvent,
    OrderOpenedEvent,
    OrderUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
)
from app.storage.database import Database
from app.utils.pnl import calculate_pnl_pct, fmt_pct, fmt_pnl, pnl_label
from app.utils.retry import with_retry

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _utc(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _duration(opened_at: datetime | None, closed_at: datetime) -> str:
    if opened_at is None:
        return "—"
    delta = closed_at - opened_at
    total_minutes = int(delta.total_seconds() / 60)
    return f"{total_minutes // 60:02d}h {total_minutes % 60:02d}m"


# ---------------------------------------------------------------------------
# Message formatters — module-level functions for easy unit testing
# ---------------------------------------------------------------------------

def format_startup(exchange: str, network: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"🟡 <b>checkDEX started</b>\n"
        f"Exchange: {exchange} ({network})\n"
        f"Monitoring: orders, positions, trades\n"
        f"Started at: {now}"
    )


def format_order_opened(event: OrderOpenedEvent) -> str:
    o = event.order
    price = f"{o.price:.2f}" if o.price else "MARKET"
    return (
        f"📋 <b>ORDER OPENED</b>\n"
        f"Exchange: {o.exchange}\n"
        f"Market: {o.market}\n"
        f"Side: {o.side}\n"
        f"Type: {o.type}\n"
        f"Price: {price}\n"
        f"Qty: {o.qty}\n"
        f"Filled: {o.filled_qty}\n"
        f"Order ID: <code>{o.id}</code>\n"
        f"Time: {_utc(o.created_at)}"
    )


def format_order_updated(event: OrderUpdatedEvent) -> str:
    o = event.order
    price = f"{o.price:.2f}" if o.price else "MARKET"
    return (
        f"🔄 <b>ORDER UPDATED</b>\n"
        f"Exchange: {o.exchange}\n"
        f"Market: {o.market}\n"
        f"Side: {o.side}\n"
        f"Status: <b>{o.status}</b>\n"
        f"Price: {price}\n"
        f"Qty: {o.qty}\n"
        f"Filled: {o.filled_qty}\n"
        f"Order ID: <code>{o.id}</code>\n"
        f"Time: {_utc(o.updated_at or o.created_at)}"
    )


def format_order_filled(event: OrderFilledEvent) -> str:
    o = event.order
    price = f"{o.price:.2f}" if o.price else "MARKET"
    return (
        f"✅ <b>ORDER FILLED</b>\n"
        f"Exchange: {o.exchange}\n"
        f"Market: {o.market}\n"
        f"Side: {o.side}\n"
        f"Type: {o.type}\n"
        f"Price: {price}\n"
        f"Qty: {o.qty}\n"
        f"Filled: {o.filled_qty}\n"
        f"Order ID: <code>{o.id}</code>\n"
        f"Time: {_utc(o.updated_at or o.created_at)}"
    )


def format_position_opened(event: PositionOpenedEvent) -> str:
    p = event.position
    leverage = f"{p.leverage}x" if p.leverage else "—"
    return (
        f"📈 <b>POSITION OPENED</b>\n"
        f"Exchange: {p.exchange}\n"
        f"Market: {p.market}\n"
        f"Side: {p.side}\n"
        f"Size: {p.size}\n"
        f"Entry: {p.entry_price:.2f}\n"
        f"Leverage: {leverage}\n"
        f"Opened at: {_utc(p.opened_at)}"
    )


def format_position_updated(event: PositionUpdatedEvent) -> str:
    p = event.position
    prev = event.previous
    mark = f"{p.mark_price:.2f}" if p.mark_price else "—"
    upnl = fmt_pnl(p.unrealized_pnl) if p.unrealized_pnl is not None else "—"
    return (
        f"🔄 <b>POSITION UPDATED</b>\n"
        f"Exchange: {p.exchange}\n"
        f"Market: {p.market}\n"
        f"Side: {p.side}\n"
        f"Size: {p.size} (was {prev.size})\n"
        f"Entry: {p.entry_price:.2f}\n"
        f"Mark: {mark}\n"
        f"Unrealized PnL: {upnl}"
    )


def format_position_closed(event: PositionClosedEvent) -> str:
    t = event.trade
    pct = calculate_pnl_pct(t.realised_pnl, t.entry_price, t.size)
    emoji, label = pnl_label(t.realised_pnl)
    pnl_str = fmt_pnl(t.realised_pnl)
    pct_str = fmt_pct(pct)
    duration = _duration(t.opened_at, t.closed_at)

    lines = [
        f"{emoji} <b>POSITION CLOSED — {label}</b>",
        f"Exchange: {t.exchange}",
        f"Market: {t.market}",
        f"Side: {t.side}",
        f"Size: {t.size}",
        f"Entry: {t.entry_price:.2f}",
        f"Exit: {t.exit_price:.2f}",
        f"PnL: <b>{pnl_str}</b>",
    ]
    if pct_str:
        lines.append(f"PnL %: <b>{pct_str}</b>")
    lines += [
        f"Duration: {duration}",
        f"Closed at: {_utc(t.closed_at)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Sends formatted HTML messages to a Telegram chat via Bot API."""

    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        logger.info("Telegram notifier ready")

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("TelegramNotifier.connect() must be called before use")
        return self._session

    async def _post(self, text: str) -> None:
        url = _API_URL.format(token=self._config.telegram_bot_token)
        async with self._session_or_raise().post(
            url,
            json={
                "chat_id": self._config.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()

    async def _send(self, text: str, notification_id: str) -> None:
        if await self._db.is_notified(notification_id):
            logger.debug("Notification already sent", extra={"id": notification_id})
            return
        await with_retry(lambda: self._post(text), label=f"telegram:{notification_id[:40]}")
        await self._db.mark_notified(notification_id)
        logger.info("Telegram notification sent", extra={"id": notification_id})

    # ------------------------------------------------------------------
    # Public send methods — each checks its config toggle before sending
    # ------------------------------------------------------------------

    async def send_startup(self, exchange: str) -> None:
        if not self._config.enable_startup_notification:
            return
        text = format_startup(exchange, self._config.extended_network)
        await with_retry(lambda: self._post(text), label="telegram:startup")
        logger.info("Startup notification sent")

    async def send_order_opened(self, event: OrderOpenedEvent) -> None:
        if not self._config.enable_order_opened:
            return
        o = event.order
        nid = f"order_opened:{o.exchange}:{o.id}"
        await self._send(format_order_opened(event), nid)

    async def send_order_updated(self, event: OrderUpdatedEvent) -> None:
        if not self._config.enable_order_updated:
            return
        o = event.order
        nid = f"order_updated:{o.exchange}:{o.id}:{o.status}:{o.filled_qty}"
        await self._send(format_order_updated(event), nid)

    async def send_order_filled(self, event: OrderFilledEvent) -> None:
        if not self._config.enable_order_filled:
            return
        o = event.order
        nid = f"order_filled:{o.exchange}:{o.id}"
        await self._send(format_order_filled(event), nid)

    async def send_position_opened(self, event: PositionOpenedEvent) -> None:
        if not self._config.enable_position_opened:
            return
        p = event.position
        opened_ts = int(p.opened_at.timestamp()) if p.opened_at else 0
        nid = f"position_opened:{p.exchange}:{p.market}:{opened_ts}"
        await self._send(format_position_opened(event), nid)

    async def send_position_updated(self, event: PositionUpdatedEvent) -> None:
        if not self._config.enable_position_updated:
            return
        p = event.position
        nid = f"position_updated:{p.exchange}:{p.market}:{p.size}"
        await self._send(format_position_updated(event), nid)

    async def send_position_closed(self, event: PositionClosedEvent) -> None:
        if not self._config.enable_position_closed:
            return
        t = event.trade
        nid = f"position_closed:{t.exchange}:{t.id}"
        await self._send(format_position_closed(event), nid)
