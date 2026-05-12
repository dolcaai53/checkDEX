from __future__ import annotations

import asyncio
import logging
import signal

from app.config import Config
from app.exchanges.base import ExchangeAdapter
from app.exchanges.extended import ExtendedAdapter
from app.exchanges.hyperliquid import HyperliquidAdapter
from app.notifiers.telegram import TelegramNotifier
from app.services.monitor import Monitor
from app.storage.database import Database
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)

_monitors: list[Monitor] = []


def _create_exchange_adapter(config: Config, exchange_id: str) -> ExchangeAdapter:
    if exchange_id == "hyperliquid":
        return HyperliquidAdapter(config)
    return ExtendedAdapter(config)


async def main() -> None:
    global _monitors

    config = Config()
    setup_logging(config.log_level, config.log_format)

    logger.info(
        "checkDEX starting",
        extra={"exchanges": config.active_exchanges},
    )

    db = Database(config.state_db_path)
    await db.connect()
    logger.info("Database connected", extra={"path": config.state_db_path})

    notifier = TelegramNotifier(config, db)
    await notifier.connect()
    logger.info("Telegram notifier ready")

    exchanges: list[ExchangeAdapter] = []
    for exchange_id in config.active_exchanges:
        exchange = _create_exchange_adapter(config, exchange_id)
        await exchange.connect()
        logger.info("Exchange connected", extra={"exchange": exchange.exchange_name})
        await notifier.send_startup(exchange.exchange_name, exchange.network)
        exchanges.append(exchange)

    _monitors = [Monitor(config, ex, db, notifier) for ex in exchanges]

    try:
        await asyncio.gather(*[m.run() for m in _monitors])
    finally:
        logger.info("Shutting down components...")
        await notifier.disconnect()
        for exchange in exchanges:
            await exchange.disconnect()
        await db.disconnect()
        logger.info("checkDEX stopped cleanly")


def _handle_signal(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received")
    if _monitors:
        for m in _monitors:
            loop.create_task(m.stop())
    else:
        loop.stop()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _handle_signal(loop))

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
