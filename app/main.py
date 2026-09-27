from __future__ import annotations

import asyncio
import logging
import signal

from app.config import Config
from app.exchanges.base import ExchangeAdapter
from app.exchanges.extended import ExtendedAdapter
from app.exchanges.hyperliquid import HyperliquidAdapter
from app.notifiers.telegram import TelegramNotifier
from app.services.commands import TelegramCommandListener
from app.services.monitor import Monitor
from app.storage.database import Database
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)

# Neuspesne pripojeni burzy se opakuje po 30 s, 60 s, 120 s ... nejvyse po 10 min.
_CONNECT_RETRY_MIN_SECONDS = 30.0
_CONNECT_RETRY_MAX_SECONDS = 600.0


def _create_exchange_adapter(config: Config, exchange_id: str) -> ExchangeAdapter:
    if exchange_id == "hyperliquid":
        return HyperliquidAdapter(config)
    return ExtendedAdapter(config)


async def main(shutdown: asyncio.Event) -> None:
    config = Config()
    setup_logging(config.log_level, config.log_format)

    logger.info(
        "checkDEX starting",
        extra={"exchanges": config.active_exchanges},
    )

    db = Database(config.state_db_path)
    notifier = TelegramNotifier(config, db)
    exchanges = [_create_exchange_adapter(config, ex_id) for ex_id in config.active_exchanges]
    runner_tasks: list[asyncio.Task[None]] = []
    listener_task: asyncio.Task[None] | None = None

    # Vse od pripojeni DB je v try — pri jakekoli chybe se DB zavre a proces
    # skonci (Docker ho restartuje), misto aby visel jako "unhealthy".
    try:
        await db.connect()
        logger.info("Database connected", extra={"path": config.state_db_path})

        await notifier.connect()
        logger.info("Telegram notifier ready")

        if config.enable_telegram_commands:
            # Listener dostane vsechny burzy — nepripojena hlasi v /positions "Data unavailable".
            listener = TelegramCommandListener(config, db, notifier, exchanges)
            listener_task = asyncio.create_task(listener.run(), name="telegram_commands")

        # Kazda burza bezi samostatne — kdyz se jedna nepripoji, ostatni sleduji dal.
        runner_tasks = [
            asyncio.create_task(
                run_exchange(config, exchange, db, notifier, shutdown),
                name=f"exchange:{exchange.exchange_name}",
            )
            for exchange in exchanges
        ]
        await asyncio.gather(*runner_tasks)
    finally:
        logger.info("Shutting down components...")
        for task in runner_tasks:
            task.cancel()
        await asyncio.gather(*runner_tasks, return_exceptions=True)
        # Listener nema co dokoncovat — zrusit ho driv, nez se zavre session a DB.
        if listener_task is not None:
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
        await notifier.disconnect()
        for exchange in exchanges:
            try:
                await exchange.disconnect()
            except Exception:
                logger.exception(
                    "Exchange disconnect failed", extra={"exchange": exchange.exchange_name}
                )
        await db.disconnect()
        logger.info("checkDEX stopped cleanly")


async def run_exchange(
    config: Config,
    exchange: ExchangeAdapter,
    db: Database,
    notifier: TelegramNotifier,
    shutdown: asyncio.Event,
) -> None:
    """Pripoji jednu burzu (s opakovanim) a sleduje ji az do shutdownu."""
    name = exchange.exchange_name
    connected = await _connect_with_retry(exchange, notifier, shutdown)
    if not connected or shutdown.is_set():
        return  # vypinani prislo driv, nez monitoring zacal
    logger.info("Exchange connected", extra={"exchange": name})

    try:
        await notifier.send_startup(name, exchange.network)
    except Exception as exc:
        # Nedorucena startup zprava nesmi zastavit monitoring.
        logger.warning(
            "Startup notification failed",
            extra={"exchange": name, "error": notifier.safe_error(exc)},
        )

    monitor = Monitor(config, exchange, db, notifier)
    stopper = asyncio.create_task(_stop_on_shutdown(monitor, shutdown), name=f"stop:{name}")
    try:
        await monitor.run()
    finally:
        stopper.cancel()


async def _connect_with_retry(
    exchange: ExchangeAdapter,
    notifier: TelegramNotifier,
    shutdown: asyncio.Event,
) -> bool:
    """Opakuje pripojeni burzy, dokud se nepovede. False = prisel shutdown.

    Kazdy neuspech se zaloguje; do Telegramu jde chyba jen jednou (kdyz se
    zpravu nepodari odeslat, zkusi se to znovu pri dalsim neuspechu).
    """
    name = exchange.exchange_name
    delay = _CONNECT_RETRY_MIN_SECONDS
    attempt = 0
    error_reported = False
    while not shutdown.is_set():
        attempt += 1
        try:
            await exchange.connect()
            return True
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            logger.error(
                "Exchange connection failed",
                extra={
                    "exchange": name,
                    "attempt": attempt,
                    "error": error,
                    "retry_in_seconds": delay,
                },
                exc_info=attempt == 1,  # traceback jen u prvniho pokusu
            )
            if not error_reported:
                error_reported = await notifier.send_exchange_error(name, exchange.network, error)
        await _sleep_unless_shutdown(shutdown, delay)
        delay = min(delay * 2, _CONNECT_RETRY_MAX_SECONDS)
    return False


async def _sleep_unless_shutdown(shutdown: asyncio.Event, seconds: float) -> None:
    """Pocka *seconds* sekund; pri shutdownu se probudi okamzite."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _stop_on_shutdown(monitor: Monitor, shutdown: asyncio.Event) -> None:
    await shutdown.wait()
    await monitor.stop()


def _handle_signal(shutdown: asyncio.Event) -> None:
    logger.info("Shutdown signal received")
    shutdown.set()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, shutdown)

    try:
        loop.run_until_complete(main(shutdown))
    finally:
        loop.close()
