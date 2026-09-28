"""Unit tests for ExtendedAdapter — posledni obchodni cena u pozic, casovy limit
a opakovani dotazu.

SDK klient je nahrazeny fake objektem a prime HTTP dotazy obsluhuje lokalni
testovaci server — zadne dotazy na Extended.
"""
from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from x10.utils.http import ResponseStatus, WrappedApiResponse

from app.config import Config
from app.exceptions import ExchangeConnectionError
from app.exchanges import extended as extended_module
from app.exchanges.extended import ExtendedAdapter, parse_last_prices
from app.models.order import OrderStatus
from app.utils.retry import with_retry
from tests.test_event_engine import _sdk_position

# Limit session v testech misto 15 s — pomaly dotaz se overi rychle.
_TEST_TIMEOUT = aiohttp.ClientTimeout(total=0.2)
_SINCE = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _config() -> Config:
    """Konfigurace s vymyslenymi klici — testy nic neposilaji na Extended."""
    return Config(
        _env_file=None,
        extended_api_key="test-key",
        extended_public_key="0x1",
        extended_private_key="0x2",
        extended_vault="1",
        telegram_bot_token="0000:test",
        telegram_chat_id="-1001",
    )


def _market(name: str, last_price: str | None) -> dict:
    """Polozka odpovedi /info/markets — jen pole, ktera adapter cte."""
    return {"name": name, "marketStats": {"lastPrice": last_price, "markPrice": "1"}}


# ---------------------------------------------------------------------------
# parse_last_prices
# ---------------------------------------------------------------------------

def test_parse_last_prices() -> None:
    payload = {"status": "OK", "data": [_market("BTC-USD", "82917"), _market("1000PEPE-USD", "0.004131")]}
    assert parse_last_prices(payload) == {
        "BTC-USD": Decimal("82917"),
        "1000PEPE-USD": Decimal("0.004131"),
    }


def test_parse_last_prices_skips_market_without_price() -> None:
    payload = {"data": [_market("BTC-USD", None), {"name": "ETH-USD"}, _market("SOL-USD", "180.5")]}
    assert parse_last_prices(payload) == {"SOL-USD": Decimal("180.5")}


@pytest.mark.parametrize("payload", [None, [], "unexpected", {"status": "ERROR"}, {"data": None}])
def test_parse_last_prices_unexpected_response(payload) -> None:
    assert parse_last_prices(payload) == {}


# ---------------------------------------------------------------------------
# get_positions() — posledni cena z /info/markets
# ---------------------------------------------------------------------------

class _FakeAccount:
    """Nahrada client.account — vraci pozice nastavene testem."""

    def __init__(self) -> None:
        self.positions: list = []

    async def get_positions(self) -> WrappedApiResponse:
        return WrappedApiResponse(status=ResponseStatus.OK, data=self.positions)


@pytest.fixture
async def extended():
    """Adapter s fake SDK klientem; /info/markets obsluhuje lokalni server.

    Vraci (adapter, seznam trhu z kazdeho prijateho dotazu, odpoved serveru).
    Test nastavi pozice v adapter._client.account.positions a muze zmenit odpoved.
    """
    requests: list[list[str]] = []
    reply = {"status": 200, "json": {"status": "OK", "data": []}}

    async def handler(request: web.Request) -> web.Response:
        requests.append(request.query.getall("market", []))
        return web.json_response(reply["json"], status=reply["status"])

    app = web.Application()
    app.router.add_get("/api/v1/info/markets", handler)
    async with TestServer(app) as server:
        adapter = ExtendedAdapter(_config())
        adapter._client = SimpleNamespace(account=_FakeAccount())
        adapter._session = aiohttp.ClientSession()
        adapter._api_base_url = str(server.make_url("/api/v1"))
        yield adapter, requests, reply
        await adapter._session.close()


async def test_get_positions_fills_last_price(extended) -> None:
    adapter, requests, reply = extended
    adapter._client.account.positions = [
        _sdk_position(market="ETH-USD"),
        _sdk_position(id=11, market="BTC-USD"),
    ]
    reply["json"] = {"status": "OK", "data": [_market("BTC-USD", "82917"), _market("ETH-USD", "3051.5")]}

    positions = await adapter.get_positions()

    assert {p.market: p.last_price for p in positions} == {
        "ETH-USD": Decimal("3051.5"),
        "BTC-USD": Decimal("82917"),
    }
    assert positions[0].mark_price == Decimal("3050")  # mark cena zustava jako zaloha
    assert requests == [["BTC-USD", "ETH-USD"]]  # jeden dotaz pro vsechny trhy


async def test_get_positions_without_last_price_when_fetch_fails(extended, caplog) -> None:
    adapter, _, reply = extended
    adapter._client.account.positions = [_sdk_position(market="ETH-USD")]
    reply["status"] = 503

    with caplog.at_level("WARNING"):
        positions = await adapter.get_positions()

    # Pozice se vrati dal, jen bez posledni ceny — zprava ukaze Mark.
    assert [p.market for p in positions] == ["ETH-USD"]
    assert positions[0].last_price is None
    assert positions[0].mark_price == Decimal("3050")
    assert "Extended last prices fetch failed" in caplog.text


async def test_get_positions_no_price_request_without_positions(extended) -> None:
    adapter, requests, _ = extended

    assert await adapter.get_positions() == []
    assert requests == []


# ---------------------------------------------------------------------------
# Casovy limit a opakovani dotazu
# ---------------------------------------------------------------------------

@pytest.fixture
def no_retry_delay(monkeypatch):
    """Opakovani dotazu bez pauzy mezi pokusy — test neceka na backoff."""
    monkeypatch.setattr(extended_module, "with_retry", functools.partial(with_retry, base_delay=0))


async def test_connect_sets_request_timeout() -> None:
    """Dotazy pres SDK i prime dotazy jdou pres session s limitem 15 s (SDK ma 500 s)."""
    adapter = ExtendedAdapter(_config())
    await adapter.connect()
    try:
        assert adapter._session.timeout.total == 15
        assert adapter._client.account._BaseModule__session is adapter._session
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_open_orders", ()),
        ("get_positions", ()),
        ("get_positions_history", (_SINCE,)),
        ("get_trades", (_SINCE,)),
    ],
)
async def test_sdk_call_timeout_raises_connection_error(extended, no_retry_delay, method, args) -> None:
    adapter, _, _ = extended
    calls: list[str] = []

    async def timeout(**kwargs):
        calls.append(method)
        raise asyncio.TimeoutError

    setattr(adapter._client.account, method, timeout)

    with pytest.raises(ExchangeConnectionError, match=f"{method} failed: TimeoutError"):
        await getattr(adapter, method)(*args)
    assert len(calls) == 4  # prvni pokus + 3 opakovani


def _raw_order(order_id: int) -> dict:
    """Polozka odpovedi /user/orders/history — jen pole, ktera adapter cte."""
    return {
        "id": order_id,
        "market": "BTC-USD",
        "side": "BUY",
        "type": "LIMIT",
        "price": "63250.5",
        "qty": "0.25",
        "filledQty": "0.25",
        "status": "FILLED",
        "createdTime": 1_790_000_000_000,
        "updatedTime": 1_790_000_060_000,
    }


@pytest.fixture
async def orders_history():
    """Adapter s limitem _TEST_TIMEOUT; /user/orders/history obsluhuje lokalni server.

    Vraci (adapter, state): prvnich state["slow"] dotazu dostane odpoved az po
    vyprseni limitu, state["requests"] pocita prijate dotazy.
    """
    state = {"slow": 0, "requests": 0}

    async def handler(request: web.Request) -> web.Response:
        state["requests"] += 1
        if state["requests"] <= state["slow"]:
            await asyncio.sleep(_TEST_TIMEOUT.total * 2)
        return web.json_response({"status": "OK", "data": [_raw_order(1)]})

    app = web.Application()
    app.router.add_get("/api/v1/user/orders/history", handler)
    async with TestServer(app) as server:
        adapter = ExtendedAdapter(_config())
        adapter._session = aiohttp.ClientSession(timeout=_TEST_TIMEOUT)
        adapter._api_base_url = str(server.make_url("/api/v1"))
        yield adapter, state
        await adapter._session.close()


async def test_get_orders_history_retries_after_timeout(orders_history, no_retry_delay) -> None:
    adapter, state = orders_history
    state["slow"] = 1

    orders = await adapter.get_orders_history(_SINCE)

    assert [(o.id, o.status) for o in orders] == [("1", OrderStatus.FILLED)]
    assert state["requests"] == 2  # po vyprseni limitu druhy pokus


async def test_get_orders_history_timeout_raises_connection_error(orders_history, no_retry_delay) -> None:
    adapter, state = orders_history
    state["slow"] = 99

    with pytest.raises(ExchangeConnectionError, match="get_orders_history failed: TimeoutError"):
        await adapter.get_orders_history(_SINCE)
    assert state["requests"] == 4  # prvni pokus + 3 opakovani
