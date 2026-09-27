"""Unit testy pro Telegram prikazy (/positions).

Bez site — Telegram notifier i burzy jsou nahrazene jednoduchymi fake tridami,
DB je skutecna SQLite v tmp_path.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import Config
from app.exceptions import TelegramAPIError
from app.models.position import Position, PositionSide
from app.notifiers import telegram
from app.notifiers.telegram import TelegramNotifier
from app.services import commands
from app.services.commands import POSITIONS_COMMAND, TelegramCommandListener, parse_command
from app.storage.database import Database

_CHAT_ID = "-5270000001"
_OTHER_CHAT_ID = "-5270000002"
_TOKEN = "123456:TEST-TOKEN"
_NOW = 1_790_000_000.0


class _Stop(BaseException):
    """Ukonci nekonecnou smycku listeneru v testu (neni to Exception)."""


def _update(
    update_id: int = 1,
    text: str = "/positions",
    chat_id: str = _CHAT_ID,
    date: float | None = None,
    kind: str = "message",
) -> dict:
    return {
        "update_id": update_id,
        kind: {
            "message_id": update_id,
            "date": int(date if date is not None else time.time()),
            "chat": {"id": int(chat_id), "type": "group"},
            "text": text,
        },
    }


def _position(market: str = "BTC-USD", exchange: str = "Extended") -> Position:
    return Position(
        market=market,
        exchange=exchange,
        side=PositionSide.LONG,
        size=Decimal("0.25"),
        entry_price=Decimal("63250.5"),
        unrealized_pnl=Decimal("62.38"),
    )


class FakeNotifier:
    """Vraci pripravene davky updatu; po posledni davce ukonci smycku."""

    def __init__(self, batches: list) -> None:
        self._batches = list(batches)
        self.offsets: list[int | None] = []
        self.reports: list = []

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        self.offsets.append(offset)
        if not self._batches:
            raise _Stop()
        item = self._batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def send_positions_report(self, sections: list) -> None:
        self.reports.append(sections)


class FakeExchange:
    def __init__(
        self,
        name: str,
        positions: list[Position] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.exchange_name = name
        self.network = "mainnet"
        self._positions = positions or []
        self._error = error
        self.calls = 0

    async def get_positions(self) -> list[Position]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._positions


@pytest.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
def cmd_config() -> Config:
    return Config(telegram_bot_token=_TOKEN, telegram_chat_id=_CHAT_ID)


async def _run(listener: TelegramCommandListener) -> None:
    with pytest.raises(_Stop):
        await listener.run()


# ---------------------------------------------------------------------------
# parse_command
# ---------------------------------------------------------------------------

def test_parse_command_positions_from_allowed_chat() -> None:
    assert parse_command(_update(date=_NOW), _CHAT_ID, _NOW) == POSITIONS_COMMAND


def test_parse_command_strips_bot_name_and_arguments() -> None:
    update = _update(text="/positions@CheckDexBot now", date=_NOW)
    assert parse_command(update, _CHAT_ID, _NOW) == POSITIONS_COMMAND


def test_parse_command_is_case_insensitive() -> None:
    assert parse_command(_update(text="/Positions", date=_NOW), _CHAT_ID, _NOW) == POSITIONS_COMMAND


def test_parse_command_ignores_other_chat() -> None:
    update = _update(chat_id=_OTHER_CHAT_ID, date=_NOW)
    assert parse_command(update, _CHAT_ID, _NOW) is None


def test_parse_command_ignores_stale_message() -> None:
    fresh = _update(date=_NOW - 299)
    stale = _update(date=_NOW - 301)
    assert parse_command(fresh, _CHAT_ID, _NOW) == POSITIONS_COMMAND
    assert parse_command(stale, _CHAT_ID, _NOW) is None


def test_parse_command_ignores_plain_text() -> None:
    assert parse_command(_update(text="hello", date=_NOW), _CHAT_ID, _NOW) is None


def test_parse_command_accepts_channel_post() -> None:
    update = _update(date=_NOW, kind="channel_post")
    assert parse_command(update, _CHAT_ID, _NOW) == POSITIONS_COMMAND


def test_parse_command_ignores_update_without_message() -> None:
    update = _update(date=_NOW, kind="edited_message")
    assert parse_command(update, _CHAT_ID, _NOW) is None


# ---------------------------------------------------------------------------
# TelegramCommandListener
# ---------------------------------------------------------------------------

async def test_positions_command_reports_all_exchanges(cmd_config, db) -> None:
    extended = FakeExchange("Extended", [_position()])
    hyperliquid = FakeExchange("Hyperliquid", [])
    notifier = FakeNotifier([[_update(update_id=10)]])
    listener = TelegramCommandListener(cmd_config, db, notifier, [extended, hyperliquid])

    await _run(listener)

    assert len(notifier.reports) == 1
    sections = notifier.reports[0]
    assert [s.exchange for s in sections] == ["Extended", "Hyperliquid"]
    assert sections[0].positions == [_position()]
    assert sections[1].positions == []
    assert await db.get_cursor("telegram_updates_offset") == "11"
    assert notifier.offsets == [None, 11]


async def test_multiple_commands_in_batch_send_one_reply(cmd_config, db) -> None:
    exchange = FakeExchange("Extended", [_position()])
    batch = [_update(update_id=1), _update(update_id=2), _update(update_id=3)]
    notifier = FakeNotifier([batch])
    listener = TelegramCommandListener(cmd_config, db, notifier, [exchange])

    await _run(listener)

    assert len(notifier.reports) == 1
    assert exchange.calls == 1
    assert await db.get_cursor("telegram_updates_offset") == "4"


async def test_other_chat_gets_no_reply_but_offset_advances(cmd_config, db) -> None:
    exchange = FakeExchange("Extended", [_position()])
    notifier = FakeNotifier([[_update(update_id=7, chat_id=_OTHER_CHAT_ID)]])
    listener = TelegramCommandListener(cmd_config, db, notifier, [exchange])

    await _run(listener)

    assert notifier.reports == []
    assert exchange.calls == 0
    assert await db.get_cursor("telegram_updates_offset") == "8"


async def test_failed_exchange_is_reported_as_unavailable(cmd_config, db) -> None:
    broken = FakeExchange("Extended", error=RuntimeError("API down"))
    healthy = FakeExchange("Hyperliquid", [_position("ETH-USDC", "Hyperliquid")])
    notifier = FakeNotifier([[_update(update_id=1)]])
    listener = TelegramCommandListener(cmd_config, db, notifier, [broken, healthy])

    await _run(listener)

    sections = notifier.reports[0]
    assert sections[0].positions is None
    assert sections[1].positions == [_position("ETH-USDC", "Hyperliquid")]


async def test_offset_is_restored_after_restart(cmd_config, db) -> None:
    await db.set_cursor("telegram_updates_offset", "42")
    notifier = FakeNotifier([])
    listener = TelegramCommandListener(cmd_config, db, notifier, [])

    await _run(listener)

    assert notifier.offsets == [42]


async def test_corrupted_offset_is_ignored(cmd_config, db) -> None:
    await db.set_cursor("telegram_updates_offset", "not-a-number")
    notifier = FakeNotifier([])
    listener = TelegramCommandListener(cmd_config, db, notifier, [])

    await _run(listener)

    assert notifier.offsets == [None]


async def test_listener_survives_errors_and_hides_token(cmd_config, db, monkeypatch, caplog) -> None:
    monkeypatch.setattr(commands, "_MIN_BACKOFF_SECONDS", 0)
    error = RuntimeError(f"409, url='https://api.telegram.org/bot{_TOKEN}/getUpdates'")
    exchange = FakeExchange("Extended", [_position()])
    notifier = FakeNotifier([error, [_update(update_id=5)]])
    listener = TelegramCommandListener(cmd_config, db, notifier, [exchange])

    with caplog.at_level(logging.WARNING, logger="app.services.commands"):
        await _run(listener)

    assert len(notifier.reports) == 1
    warnings = [r for r in caplog.records if r.getMessage() == "Telegram command handling failed"]
    assert len(warnings) == 1
    assert _TOKEN not in warnings[0].error
    assert "bot***/getUpdates" in warnings[0].error


# ---------------------------------------------------------------------------
# TelegramNotifier.get_updates — proti lokalnimu HTTP serveru (bez internetu)
# ---------------------------------------------------------------------------

@pytest.fixture
async def fake_telegram(monkeypatch):
    """Lokalni server misto api.telegram.org; vraci (prijate requesty, odpovedi)."""
    received: list[dict] = []
    responses: list[tuple[int, dict]] = []

    async def handler(request: web.Request) -> web.Response:
        received.append({"path": request.path, "body": await request.json()})
        status, body = responses.pop(0)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/bot{token}/getUpdates", handler)
    async with TestServer(app) as server:
        url = f"http://{server.host}:{server.port}/bot{{token}}/getUpdates"
        monkeypatch.setattr(telegram, "_UPDATES_URL", url)
        yield received, responses


async def test_get_updates_sends_offset_and_parses_result(cmd_config, db, fake_telegram) -> None:
    received, responses = fake_telegram
    responses.append((200, {"ok": True, "result": [_update(update_id=3)]}))
    notifier = TelegramNotifier(cmd_config, db)
    await notifier.connect()
    try:
        updates = await notifier.get_updates(offset=3, timeout=0)
    finally:
        await notifier.disconnect()

    assert [u["update_id"] for u in updates] == [3]
    assert received[0]["path"] == f"/bot{_TOKEN}/getUpdates"
    assert received[0]["body"] == {
        "timeout": 0,
        "allowed_updates": ["message", "channel_post"],
        "offset": 3,
    }


async def test_get_updates_error_raises_without_token(cmd_config, db, fake_telegram) -> None:
    _, responses = fake_telegram
    responses.append((409, {
        "ok": False,
        "error_code": 409,
        "description": "Conflict: can't use getUpdates method while webhook is active",
    }))
    notifier = TelegramNotifier(cmd_config, db)
    await notifier.connect()
    try:
        with pytest.raises(TelegramAPIError) as exc_info:
            await notifier.get_updates(offset=None, timeout=0)
    finally:
        await notifier.disconnect()

    assert "409" in str(exc_info.value)
    assert "webhook" in str(exc_info.value)
    assert _TOKEN not in str(exc_info.value)
