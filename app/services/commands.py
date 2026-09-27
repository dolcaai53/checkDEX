from __future__ import annotations

import asyncio
import logging
import time

from app.config import Config
from app.exchanges.base import ExchangeAdapter
from app.models.position import Position
from app.notifiers.telegram import ExchangePositions, TelegramNotifier
from app.storage.database import Database

logger = logging.getLogger(__name__)

POSITIONS_COMMAND = "/positions"

_OFFSET_CURSOR_KEY = "telegram_updates_offset"
_LONG_POLL_TIMEOUT_SECONDS = 30
# Prikazy starsi nez tento limit (napr. poslane behem vypadku bota) se ignoruji,
# aby po restartu neprisla zaplava odpovedi.
_MAX_COMMAND_AGE_SECONDS = 300
_MIN_BACKOFF_SECONDS = 5
_MAX_BACKOFF_SECONDS = 300


def parse_command(update: dict, chat_id: str, now_ts: float) -> str | None:
    """Vrati prikaz (napr. "/positions") z Telegram update, jinak None.

    Prijima jen zpravy z chatu *chat_id* (TELEGRAM_CHAT_ID) — cizi chaty se
    ignoruji, aby se k datum uctu nedostal nikdo dalsi. Ignoruje i prikazy
    starsi nez _MAX_COMMAND_AGE_SECONDS.
    """
    msg = update.get("message") or update.get("channel_post")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    if str(chat.get("id")) != chat_id:
        return None
    if now_ts - msg.get("date", 0) > _MAX_COMMAND_AGE_SECONDS:
        return None
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    # "/positions@MujBot cokoliv" -> "/positions"
    return text.split()[0].split("@")[0].lower()


class TelegramCommandListener:
    """Odpovida na prikazy poslane botovi v Telegram chatu (pouze cteni).

    Pouziva long polling (getUpdates) — neni potreba webhook ani otevreny port.
    Prikaz /positions posle aktualni otevrene pozice ze vsech sledovanych burz.
    Offset zpracovanych zprav se uklada do DB, takze se po restartu zadny
    prikaz nezpracuje dvakrat.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        notifier: TelegramNotifier,
        exchanges: list[ExchangeAdapter],
    ) -> None:
        self._chat_id = config.telegram_chat_id.strip()
        self._token = config.telegram_bot_token
        self._db = db
        self._notifier = notifier
        self._exchanges = exchanges
        self._offset: int | None = None

    async def run(self) -> None:
        """Bezi, dokud neni task zrusen (cancel) pri shutdownu aplikace."""
        self._offset = await self._load_offset()
        logger.info("Telegram command listener started", extra={"commands": [POSITIONS_COMMAND]})
        backoff = _MIN_BACKOFF_SECONDS
        while True:
            try:
                await self._poll_once()
                backoff = _MIN_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Telegram command handling failed",
                    extra={"error": self._safe_error(exc), "retry_in_seconds": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    async def _poll_once(self) -> None:
        updates = await self._notifier.get_updates(self._offset, _LONG_POLL_TIMEOUT_SECONDS)
        if not updates:
            return
        # Offset posunout a ulozit hned — pri chybe odpovedi se davka neopakuje.
        self._offset = max(int(u["update_id"]) for u in updates) + 1
        await self._db.set_cursor(_OFFSET_CURSOR_KEY, str(self._offset))

        now_ts = time.time()
        commands = {parse_command(u, self._chat_id, now_ts) for u in updates}
        # Vice /positions v jedne davce -> jedna odpoved.
        if POSITIONS_COMMAND in commands:
            logger.info("Telegram command received", extra={"command": POSITIONS_COMMAND})
            await self._reply_positions()

    async def _reply_positions(self) -> None:
        results = await asyncio.gather(*(self._fetch_positions(ex) for ex in self._exchanges))
        sections = [
            ExchangePositions(ex.exchange_name, ex.network, positions)
            for ex, positions in zip(self._exchanges, results)
        ]
        await self._notifier.send_positions_report(sections)

    async def _fetch_positions(self, exchange: ExchangeAdapter) -> list[Position] | None:
        """Cerstve pozice primo z burzy; None pri chybe (ve zprave bude varovani)."""
        try:
            return await exchange.get_positions()
        except Exception:
            logger.exception(
                "Positions fetch for Telegram command failed",
                extra={"exchange": exchange.exchange_name},
            )
            return None

    async def _load_offset(self) -> int | None:
        """Nacte ulozeny offset; pri chybejici nebo poskozene hodnote vrati None."""
        try:
            raw = await self._db.get_cursor(_OFFSET_CURSOR_KEY)
            return int(raw) if raw is not None else None
        except Exception:
            logger.warning("Stored Telegram offset unreadable, starting without it", exc_info=True)
            return None

    def _safe_error(self, exc: Exception) -> str:
        """Popis chyby do logu bez bot tokenu (chyby aiohttp obsahuji URL s tokenem)."""
        text = f"{type(exc).__name__}: {exc}"
        return text.replace(self._token, "***") if self._token else text
