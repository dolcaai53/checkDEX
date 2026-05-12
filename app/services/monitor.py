from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Union

from app.config import Config
from app.exchanges.base import ExchangeAdapter
from app.models.events import (
    OrderFilledEvent,
    OrderOpenedEvent,
    OrderUpdatedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
)
from app.models.position import Position
from app.notifiers.telegram import TelegramNotifier
from app.services.event_engine import EventEngine
from app.storage.database import Database

logger = logging.getLogger(__name__)

_HEALTHY_FILE = "/tmp/healthy"
_ORDERS_HISTORY_LOOKBACK_HOURS = 24

OrderEvent = Union[OrderOpenedEvent, OrderUpdatedEvent, OrderFilledEvent]
PositionEvent = Union[PositionOpenedEvent, PositionUpdatedEvent]


def _touch_healthy() -> None:
    try:
        with open(_HEALTHY_FILE, "w") as fh:
            fh.write("")
    except OSError:
        pass


class Monitor:
    """Main monitoring loop.

    Coordinates polling, state diffing, event detection, and notification dispatch.
    Runs three independent async loops with configurable intervals:
    - orders loop   (poll_interval_orders_seconds)
    - positions loop (poll_interval_positions_seconds)
    - history loop  (poll_interval_history_seconds)
    """

    def __init__(
        self,
        config: Config,
        exchange: ExchangeAdapter,
        db: Database,
        notifier: TelegramNotifier,
    ) -> None:
        self._config = config
        self._exchange = exchange
        self._db = db
        self._notifier = notifier
        self._engine = EventEngine(db)
        self._stop = asyncio.Event()
        self._current_positions: list[Position] = []

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._orders_loop(), name="orders_loop"),
            asyncio.create_task(self._positions_loop(), name="positions_loop"),
            asyncio.create_task(self._history_loop(), name="history_loop"),
        ]
        if self._config.enable_daily_summary:
            tasks.append(asyncio.create_task(self._daily_summary_loop(), name="daily_summary_loop"))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Polling loops
    # ------------------------------------------------------------------

    async def _orders_loop(self) -> None:
        exchange = self._exchange.exchange_name
        interval = self._config.poll_interval_orders_seconds
        logger.info("Orders loop started", extra={"interval": interval})

        while not self._stop.is_set():
            try:
                since = datetime.now(timezone.utc) - timedelta(hours=_ORDERS_HISTORY_LOOKBACK_HOURS)
                current = await self._exchange.get_open_orders()
                history = await self._exchange.get_orders_history(since)
                logger.debug(
                    "Orders polled",
                    extra={"open": len(current), "history": len(history), "exchange": exchange},
                )
                events = await self._engine.process_orders(exchange, current, history)
                for event in events:
                    await self._dispatch_order_event(event)
                _touch_healthy()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in orders loop", extra={"exchange": exchange})

            await self._interruptible_sleep(interval)

    async def _positions_loop(self) -> None:
        exchange = self._exchange.exchange_name
        interval = self._config.poll_interval_positions_seconds
        logger.info("Positions loop started", extra={"interval": interval})

        while not self._stop.is_set():
            try:
                current = await self._exchange.get_positions()
                self._current_positions = current
                logger.debug(
                    "Positions polled",
                    extra={"count": len(current), "exchange": exchange},
                )
                events = await self._engine.process_positions(exchange, current)
                for event in events:
                    await self._dispatch_position_event(event)
                _touch_healthy()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in positions loop", extra={"exchange": exchange})

            await self._interruptible_sleep(interval)

    async def _history_loop(self) -> None:
        exchange = self._exchange.exchange_name
        interval = self._config.poll_interval_history_seconds
        logger.info("History loop started", extra={"interval": interval})

        while not self._stop.is_set():
            try:
                since = datetime.now(timezone.utc) - timedelta(hours=_ORDERS_HISTORY_LOOKBACK_HOURS)
                trades = await self._exchange.get_positions_history(since)
                logger.debug(
                    "Position history polled",
                    extra={"count": len(trades), "exchange": exchange},
                )
                events = await self._engine.process_positions_history(exchange, trades)
                for event in events:
                    await self._notifier.send_position_closed(event)
                _touch_healthy()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in history loop", extra={"exchange": exchange})

            await self._interruptible_sleep(interval)

    async def _daily_summary_loop(self) -> None:
        summary_time = self._config.daily_summary_time
        h, m = map(int, summary_time.split(":"))
        logger.info("Daily summary loop started", extra={"scheduled_utc": summary_time})

        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)

            wait_seconds = int((target - now).total_seconds())
            logger.debug("Daily summary scheduled", extra={"in_seconds": wait_seconds})
            await self._interruptible_sleep(wait_seconds)

            if self._stop.is_set():
                break

            try:
                await self._notifier.send_daily_summary(
                    self._exchange.exchange_name,
                    self._exchange.network,
                    self._current_positions,
                )
                _touch_healthy()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error sending daily summary")

            # Sleep past the trigger minute to avoid double-fire on the same day.
            await self._interruptible_sleep(70)

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def _dispatch_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderOpenedEvent):
            await self._notifier.send_order_opened(event)
        elif isinstance(event, OrderFilledEvent):
            await self._notifier.send_order_filled(event)
        elif isinstance(event, OrderUpdatedEvent):
            await self._notifier.send_order_updated(event)

    async def _dispatch_position_event(self, event: PositionEvent) -> None:
        if isinstance(event, PositionOpenedEvent):
            await self._notifier.send_position_opened(event)
        elif isinstance(event, PositionUpdatedEvent):
            await self._notifier.send_position_updated(event)

    # ------------------------------------------------------------------
    # Interruptible sleep — wakes immediately when stop is requested
    # ------------------------------------------------------------------

    async def _interruptible_sleep(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=float(seconds))
        except asyncio.TimeoutError:
            pass
