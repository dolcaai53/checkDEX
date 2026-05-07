from __future__ import annotations

import asyncio
import logging
import signal

from app.config import Config
from app.exchanges.extended import ExtendedAdapter
from app.notifiers.telegram import TelegramNotifier
from app.services.monitor import Monitor
from app.storage.database import Database
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)

_monitor: Monitor | None = None


async def main() -> None:
    global _monitor

    config = Config()
    setup_logging(config.log_level, config.log_format)

    logger.info(
        "checkDEX starting",
        extra={"exchange": "Extended", "network": config.extended_network},
    )

    db = Database(config.state_db_path)
    await db.connect()
    logger.info("Database connected", extra={"path": config.state_db_path})

    exchange = ExtendedAdapter(config)
    await exchange.connect()
    logger.info("Exchange connected", extra={"exchange": exchange.exchange_name})

    notifier = TelegramNotifier(config, db)
    await notifier.connect()
    logger.info("Telegram notifier ready")

    await notifier.send_startup(exchange.exchange_name)

    _monitor = Monitor(config, exchange, db, notifier)

    try:
        await _monitor.run()
    finally:
        logger.info("Shutting down components...")
        await notifier.disconnect()
        await exchange.disconnect()
        await db.disconnect()
        logger.info("checkDEX stopped cleanly")


def _handle_signal(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received")
    if _monitor is not None:
        loop.create_task(_monitor.stop())
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
