from __future__ import annotations

import logging
from typing import Union

from app.models.events import (
    OrderFilledEvent,
    OrderOpenedEvent,
    OrderUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
)
from app.models.order import Order, OrderStatus
from app.models.position import Position
from app.models.trade import Trade
from app.storage.database import Database

logger = logging.getLogger(__name__)

OrderEvent = Union[OrderOpenedEvent, OrderUpdatedEvent, OrderFilledEvent]
PositionEvent = Union[PositionOpenedEvent, PositionUpdatedEvent]

# Retry limit before an order is marked DISAPPEARED_UNKNOWN
_DISAPPEARED_MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Pure detection functions (no I/O — easy to unit test)
# ---------------------------------------------------------------------------

def detect_order_events(
    previous: dict[str, Order],
    current: list[Order],
    history: list[Order],
) -> tuple[list[OrderEvent], list[str]]:
    """Diff previous snapshot against current open orders.

    Returns:
        events            — list of order events to dispatch
        to_mark_pending   — order_ids not found in current OR history (need retry)
    """
    current_by_id: dict[str, Order] = {o.id: o for o in current}
    history_by_id: dict[str, Order] = {o.id: o for o in history}
    events: list[OrderEvent] = []
    to_mark_pending: list[str] = []

    # New orders (appeared in current but were not in previous)
    for order in current:
        if order.id not in previous:
            events.append(OrderOpenedEvent(order=order))

    # Changed orders (present in both previous and current)
    for order in current:
        if order.id in previous:
            prev = previous[order.id]
            if _order_changed(prev, order):
                if order.status == OrderStatus.FILLED:
                    events.append(OrderFilledEvent(order=order, previous=prev))
                else:
                    events.append(OrderUpdatedEvent(order=order, previous=prev))

    # Disappeared orders (were in previous but not in current)
    for order_id, prev_order in previous.items():
        if order_id not in current_by_id:
            if order_id in history_by_id:
                hist = history_by_id[order_id]
                if hist.status == OrderStatus.FILLED:
                    events.append(OrderFilledEvent(order=hist, previous=prev_order))
                else:
                    events.append(OrderUpdatedEvent(order=hist, previous=prev_order))
            else:
                to_mark_pending.append(order_id)

    return events, to_mark_pending


def _order_changed(prev: Order, current: Order) -> bool:
    return prev.status != current.status or prev.filled_qty != current.filled_qty


def detect_position_events(
    previous: dict[str, Position],
    current: list[Position],
) -> list[PositionEvent]:
    """Diff previous snapshot against current positions.

    POSITION_UPDATED is emitted only on size change — not on unrealized PnL
    fluctuation (mark price changes constantly and would create spam).
    """
    events: list[PositionEvent] = []
    for pos in current:
        if pos.market not in previous:
            events.append(PositionOpenedEvent(position=pos))
        elif pos.size != previous[pos.market].size:
            events.append(PositionUpdatedEvent(position=pos, previous=previous[pos.market]))
    return events


def detect_closed_positions(
    recent_history: list[Trade],
    already_notified: set[str],
) -> list[PositionClosedEvent]:
    """Return PositionClosedEvent for trades not yet notified.

    *already_notified* is a set of notification IDs of the form
    'position_closed:{exchange}:{trade_id}'.
    """
    events: list[PositionClosedEvent] = []
    for trade in recent_history:
        nid = f"position_closed:{trade.exchange}:{trade.id}"
        if nid not in already_notified:
            events.append(PositionClosedEvent(trade=trade))
    return events


# ---------------------------------------------------------------------------
# EventEngine — orchestrates detection with DB persistence
# ---------------------------------------------------------------------------

class EventEngine:
    """Coordinates snapshot diffing, disappeared-order handling, and DB state."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def process_orders(
        self,
        exchange: str,
        current: list[Order],
        history: list[Order],
    ) -> list[OrderEvent]:
        """Diff current open orders against stored snapshot. Update snapshot."""
        previous = await self._db.get_order_snapshots(exchange)

        if not previous:
            # First run — populate snapshot silently; no notifications for pre-existing orders
            logger.info(
                "First run: populating order snapshot silently",
                extra={"exchange": exchange, "count": len(current)},
            )
            await self._db.replace_order_snapshots(exchange, current)
            return []

        events, to_mark_pending = detect_order_events(previous, current, history)

        # Queue newly disappeared orders for retry
        disappeared = await self._db.get_disappeared_pending(exchange)
        pending_ids = {d.order.id for d in disappeared}
        for order_id in to_mark_pending:
            if order_id not in pending_ids and order_id in previous:
                await self._db.add_disappeared_pending(previous[order_id])
                logger.debug(
                    "Order queued as disappeared_pending",
                    extra={"order_id": order_id, "exchange": exchange},
                )

        # Process existing disappeared-pending orders
        current_ids = {o.id for o in current}
        for dis in disappeared:
            oid = dis.order.id
            if oid in current_ids:
                # Re-appeared — remove from pending (order is back in open_orders)
                await self._db.remove_disappeared_pending(oid, exchange)
                continue

            hist_match = next((o for o in history if o.id == oid), None)
            if hist_match:
                if hist_match.status == OrderStatus.FILLED:
                    events.append(OrderFilledEvent(order=hist_match, previous=dis.order))
                else:
                    events.append(OrderUpdatedEvent(order=hist_match, previous=dis.order))
                await self._db.remove_disappeared_pending(oid, exchange)
            else:
                new_count = await self._db.increment_disappeared_retry(oid, exchange)
                logger.debug(
                    "Disappeared order retry",
                    extra={"order_id": oid, "retry": new_count},
                )
                if new_count >= _DISAPPEARED_MAX_RETRIES:
                    unknown = dis.order.model_copy(
                        update={"status": OrderStatus.DISAPPEARED_UNKNOWN}
                    )
                    events.append(OrderUpdatedEvent(order=unknown, previous=dis.order))
                    await self._db.remove_disappeared_pending(oid, exchange)
                    logger.warning(
                        "Order marked DISAPPEARED_UNKNOWN after retries",
                        extra={"order_id": oid, "exchange": exchange},
                    )

        await self._db.replace_order_snapshots(exchange, current)
        return events

    async def process_positions(
        self,
        exchange: str,
        current: list[Position],
    ) -> list[PositionEvent]:
        """Diff current positions against stored snapshot. Update snapshot."""
        previous = await self._db.get_position_snapshots(exchange)

        if not previous:
            logger.info(
                "First run: populating position snapshot silently",
                extra={"exchange": exchange, "count": len(current)},
            )
            await self._db.replace_position_snapshots(exchange, current)
            return []

        events = detect_position_events(previous, current)
        await self._db.replace_position_snapshots(exchange, current)
        return events

    async def process_positions_history(
        self,
        exchange: str,
        recent_history: list[Trade],
    ) -> list[PositionClosedEvent]:
        """Detect newly closed positions from history. Skips already-notified trades."""
        events: list[PositionClosedEvent] = []
        for trade in recent_history:
            nid = f"position_closed:{exchange}:{trade.id}"
            if not await self._db.is_notified(nid):
                events.append(PositionClosedEvent(trade=trade))
        return events
