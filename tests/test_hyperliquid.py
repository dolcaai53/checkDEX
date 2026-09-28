"""Unit tests for HyperliquidAdapter mapper functions.

All tests use static dicts mirroring real Hyperliquid API responses.
No network calls, no SDK initialization required.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config
from app.exceptions import ExchangeConnectionError
from app.exchanges.hyperliquid import (
    HyperliquidAdapter,
    _normalize_market,
    map_fill_to_trade,
    map_order,
    map_position,
    parse_last_price,
    parse_mark_prices,
    parse_user_role,
)
from app.models.order import OrderSide, OrderStatus, OrderType
from app.models.position import PositionSide

EXCHANGE = "Hyperliquid"


# ---------------------------------------------------------------------------
# _normalize_market
# ---------------------------------------------------------------------------

def test_normalize_market_btc():
    assert _normalize_market("BTC") == "BTC-USDC"


def test_normalize_market_eth():
    assert _normalize_market("ETH") == "ETH-USDC"


def test_normalize_market_sol():
    assert _normalize_market("SOL") == "SOL-USDC"


# ---------------------------------------------------------------------------
# map_order
# ---------------------------------------------------------------------------

def _open_order(
    coin="BTC",
    side="B",
    limit_px="63250.5",
    sz="0.01",
    orig_sz="0.01",
    oid=12345,
    ts=1715000000000,
) -> dict:
    return {
        "order": {
            "coin": coin,
            "side": side,
            "limitPx": limit_px,
            "sz": sz,
            "origSz": orig_sz,
            "oid": oid,
            "timestamp": ts,
        },
        "status": "open",
        "statusTimestamp": ts,
    }


def test_map_order_buy():
    order = map_order(_open_order(side="B"), EXCHANGE)
    assert order.side == OrderSide.BUY
    assert order.id == "12345"
    assert order.market == "BTC-USDC"
    assert order.exchange == EXCHANGE
    assert order.type == OrderType.LIMIT
    assert order.price == Decimal("63250.5")
    assert order.qty == Decimal("0.01")
    assert order.filled_qty == Decimal("0")
    assert order.status == OrderStatus.OPEN


def test_map_order_sell():
    order = map_order(_open_order(side="A"), EXCHANGE)
    assert order.side == OrderSide.SELL


def test_map_order_partial_fill():
    # origSz=0.02, remaining sz=0.01 → filled_qty=0.01
    order = map_order(_open_order(sz="0.01", orig_sz="0.02"), EXCHANGE)
    assert order.filled_qty == Decimal("0.01")
    assert order.qty == Decimal("0.02")
    assert order.status == OrderStatus.PARTIAL_FILL


def test_map_order_market_order():
    # Market orders have limitPx = "0"
    order = map_order(_open_order(limit_px="0"), EXCHANGE)
    assert order.price is None
    assert order.type == OrderType.MARKET


def test_map_order_id_as_string():
    order = map_order(_open_order(oid=99999), EXCHANGE)
    assert order.id == "99999"
    assert isinstance(order.id, str)


# ---------------------------------------------------------------------------
# map_position
# ---------------------------------------------------------------------------

def _asset_position(
    coin="ETH",
    szi="1.5",
    entry_px="3100.0",
    leverage_val=5,
    unrealized_pnl="75.0",
) -> dict:
    return {
        "position": {
            "coin": coin,
            "szi": szi,
            "entryPx": entry_px,
            "leverage": {"type": "cross", "value": leverage_val},
            "unrealizedPnl": unrealized_pnl,
            "positionValue": "4725.0",
        },
        "type": "oneWay",
    }


def test_map_position_long():
    pos = map_position(_asset_position(szi="1.5"), EXCHANGE)
    assert pos.side == PositionSide.LONG
    assert pos.size == Decimal("1.5")
    assert pos.market == "ETH-USDC"
    assert pos.exchange == EXCHANGE
    assert pos.entry_price == Decimal("3100.0")
    assert pos.leverage == Decimal("5")
    assert pos.unrealized_pnl == Decimal("75.0")


def test_map_position_short():
    pos = map_position(_asset_position(szi="-0.5"), EXCHANGE)
    assert pos.side == PositionSide.SHORT
    assert pos.size == Decimal("0.5")  # abs value


def test_map_position_no_leverage():
    data = _asset_position()
    data["position"]["leverage"] = {}
    pos = map_position(data, EXCHANGE)
    assert pos.leverage is None


def test_map_position_no_unrealized_pnl():
    data = _asset_position()
    del data["position"]["unrealizedPnl"]
    pos = map_position(data, EXCHANGE)
    assert pos.unrealized_pnl is None


def test_map_position_mark_price():
    pos = map_position(_asset_position(), EXCHANGE, mark_price=Decimal("3150.5"))
    assert pos.mark_price == Decimal("3150.5")


def test_map_position_without_mark_price():
    assert map_position(_asset_position(), EXCHANGE).mark_price is None


def test_map_position_last_price():
    pos = map_position(_asset_position(), EXCHANGE, last_price=Decimal("3149.9"))
    assert pos.last_price == Decimal("3149.9")


def test_map_position_without_last_price():
    assert map_position(_asset_position(), EXCHANGE).last_price is None


# ---------------------------------------------------------------------------
# parse_mark_prices
# ---------------------------------------------------------------------------

def _meta_and_ctxs(**mark_prices: str | None) -> list:
    """Odpoved metaAndAssetCtxs: universe a assetCtxs ve stejnem poradi."""
    universe = [{"name": coin, "szDecimals": 2} for coin in mark_prices]
    ctxs = [{"markPx": px, "midPx": px, "oraclePx": px} for px in mark_prices.values()]
    return [{"universe": universe}, ctxs]


def test_parse_mark_prices():
    response = _meta_and_ctxs(BTC="83096.0", DOGE="0.093")
    assert parse_mark_prices(response) == {"BTC": Decimal("83096.0"), "DOGE": Decimal("0.093")}


def test_parse_mark_prices_skips_market_without_price():
    response = _meta_and_ctxs(BTC="83096.0", NEW=None)
    assert parse_mark_prices(response) == {"BTC": Decimal("83096.0")}


@pytest.mark.parametrize(
    "response",
    [None, "unexpected", {}, [], [{}], [{"universe": "x"}, []], [{"universe": []}, None]],
)
def test_parse_mark_prices_unexpected_response(response):
    assert parse_mark_prices(response) == {}


# ---------------------------------------------------------------------------
# parse_last_price
# ---------------------------------------------------------------------------

_DAY_MS = 24 * 60 * 60 * 1000


def _candle(close: str | None, t: int = 1_790_000_000_000) -> dict:
    """Denni svicka z candleSnapshot; "c" je zaviraci (u bezici svicky posledni) cena."""
    return {"t": t, "T": t + _DAY_MS - 1, "s": "BTC", "i": "1d", "o": "1", "c": close, "n": 10}


def test_parse_last_price_takes_latest_candle():
    t = 1_790_000_000_000
    candles = [_candle("82100.0", t=t), _candle("82926.0", t=t + _DAY_MS)]
    assert parse_last_price(candles) == Decimal("82926.0")


@pytest.mark.parametrize(
    "response",
    [None, "unexpected", {}, [], [{"t": 1}], [_candle(None)], [_candle("abc")]],
)
def test_parse_last_price_unexpected_response(response):
    assert parse_last_price(response) is None


# ---------------------------------------------------------------------------
# map_fill_to_trade
# ---------------------------------------------------------------------------

def _close_fill(
    coin="BTC",
    px="63880.0",
    sz="0.01",
    side="A",
    dir_str="Close Long",
    closed_pnl="6.295",
    ts=1715006100000,
    tid=789,
) -> dict:
    return {
        "coin": coin,
        "px": px,
        "sz": sz,
        "side": side,
        "time": ts,
        "dir": dir_str,
        "closedPnl": closed_pnl,
        "hash": "0xabc",
        "oid": 12345,
        "tid": tid,
        "fee": "0.0631",
    }


def test_map_fill_profit():
    trade = map_fill_to_trade(_close_fill(closed_pnl="6.295"), EXCHANGE)
    assert trade.realised_pnl == Decimal("6.295")
    assert trade.side == PositionSide.LONG
    assert trade.market == "BTC-USDC"
    assert trade.exchange == EXCHANGE
    assert trade.id == "789"
    assert trade.exit_price == Decimal("63880.0")
    assert trade.size == Decimal("0.01")


def test_map_fill_loss():
    trade = map_fill_to_trade(_close_fill(closed_pnl="-3.72"), EXCHANGE)
    assert trade.realised_pnl == Decimal("-3.72")


def test_map_fill_close_long_side():
    trade = map_fill_to_trade(_close_fill(dir_str="Close Long"), EXCHANGE)
    assert trade.side == PositionSide.LONG


def test_map_fill_close_short_side():
    trade = map_fill_to_trade(_close_fill(dir_str="Close Short", side="B"), EXCHANGE)
    assert trade.side == PositionSide.SHORT


def test_map_fill_id_uses_tid():
    trade = map_fill_to_trade(_close_fill(tid=42), EXCHANGE)
    assert trade.id == "42"


def test_map_fill_entry_price_equals_exit():
    # entry_price is approximated as exit_price (no entry in fill data)
    trade = map_fill_to_trade(_close_fill(px="63880.0"), EXCHANGE)
    assert trade.entry_price == trade.exit_price == Decimal("63880.0")


# ---------------------------------------------------------------------------
# parse_user_role
# ---------------------------------------------------------------------------

_WALLET = "0x" + "a" * 40
_MAIN_ACCOUNT = "0x" + "b" * 40


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"role": "agent", "data": {"user": _MAIN_ACCOUNT}}, ("agent", _MAIN_ACCOUNT)),
        ({"role": "agent"}, ("agent", None)),
        ({"role": "user"}, ("user", None)),
        ({"role": "missing"}, ("missing", None)),
        ({"role": "vault"}, ("vault", None)),
        ({"role": "subAccount", "data": {"master": _MAIN_ACCOUNT}}, ("subAccount", None)),
        (None, (None, None)),
        ("unexpected", (None, None)),
        ([], (None, None)),
    ],
)
def test_parse_user_role(response, expected):
    assert parse_user_role(response) == expected


# ---------------------------------------------------------------------------
# connect() — SDK klient nahrazeny fake tridou, bez site
# ---------------------------------------------------------------------------

def _fake_info_class(
    role_response=None,
    init_error=None,
    post_error=None,
    asset_positions=(),
    ctxs_response=None,
    ctxs_error=None,
    candles=None,
    candles_error=None,
):
    """Vytvori nahradu hyperliquid.info.Info; instance jsou v FakeInfo.created.

    *candles* je {coin: odpoved candleSnapshot}; trh, ktery v nem neni, nema svicky.
    """
    created = []

    class FakeInfo:
        def __init__(self, base_url, skip_ws=False, timeout=None):
            if init_error is not None:
                raise init_error
            self.base_url = base_url
            self.skip_ws = skip_ws
            self.timeout = timeout
            self.posts = []
            self.ctxs_calls = 0
            created.append(self)

        def post(self, path, payload):
            self.posts.append((path, payload))
            if payload["type"] == "candleSnapshot":
                if candles_error is not None:
                    raise candles_error
                return (candles or {}).get(payload["req"]["coin"], [])
            if post_error is not None:
                raise post_error
            return role_response

        def user_state(self, address):
            return {"assetPositions": list(asset_positions)}

        def meta_and_asset_ctxs(self):
            self.ctxs_calls += 1
            if ctxs_error is not None:
                raise ctxs_error
            return ctxs_response

    FakeInfo.created = created
    return FakeInfo


def _adapter() -> HyperliquidAdapter:
    config = Config(
        _env_file=None,
        active_exchanges=["hyperliquid"],
        hyperliquid_wallet_address=_WALLET,
        telegram_bot_token="0000:test",
        telegram_chat_id="-1001",
    )
    return HyperliquidAdapter(config)


async def test_connect_rejects_agent_wallet_and_names_main_account(monkeypatch):
    fake = _fake_info_class(role_response={"role": "agent", "data": {"user": _MAIN_ACCOUNT}})
    monkeypatch.setattr("hyperliquid.info.Info", fake)
    adapter = _adapter()

    with pytest.raises(ExchangeConnectionError) as exc_info:
        await adapter.connect()

    message = str(exc_info.value)
    assert "API (agent) wallet" in message
    assert message.endswith(_MAIN_ACCOUNT)  # adresa na konci — jde zkopirovat
    assert fake.created[0].posts == [("/info", {"type": "userRole", "user": _WALLET})]
    with pytest.raises(ExchangeConnectionError, match="not connected"):
        await adapter.get_positions()


async def test_connect_main_account_uses_request_timeout(monkeypatch):
    fake = _fake_info_class(role_response={"role": "user"})
    monkeypatch.setattr("hyperliquid.info.Info", fake)
    adapter = _adapter()

    await adapter.connect()

    info = fake.created[0]
    assert info.timeout == 15.0
    assert info.skip_ws is True
    assert info.base_url == "https://api.hyperliquid.xyz"
    assert await adapter.get_positions() == []


async def test_connect_continues_when_role_check_fails(monkeypatch):
    fake = _fake_info_class(post_error=TimeoutError("read timed out"))
    monkeypatch.setattr("hyperliquid.info.Info", fake)
    adapter = _adapter()

    await adapter.connect()  # kontrola role nesmi zablokovat pripojeni

    assert await adapter.get_positions() == []


async def test_connect_warns_about_unknown_address(monkeypatch, caplog):
    fake = _fake_info_class(role_response={"role": "missing"})
    monkeypatch.setattr("hyperliquid.info.Info", fake)
    adapter = _adapter()

    with caplog.at_level("WARNING"):
        await adapter.connect()

    assert "does not know HYPERLIQUID_WALLET_ADDRESS" in caplog.text


async def test_connect_wraps_client_init_error(monkeypatch):
    fake = _fake_info_class(init_error=OSError("network down"))
    monkeypatch.setattr("hyperliquid.info.Info", fake)

    with pytest.raises(ExchangeConnectionError, match="network down"):
        await _adapter().connect()


# ---------------------------------------------------------------------------
# get_positions() — mark cena z metaAndAssetCtxs
# ---------------------------------------------------------------------------

async def _connected_adapter(monkeypatch, **fake_kwargs):
    """Pripojeny adapter s fake Info (hlavni ucet); vraci (adapter, info)."""
    fake = _fake_info_class(role_response={"role": "user"}, **fake_kwargs)
    monkeypatch.setattr("hyperliquid.info.Info", fake)
    adapter = _adapter()
    await adapter.connect()
    return adapter, fake.created[0]


async def test_get_positions_fills_mark_price(monkeypatch):
    adapter, info = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH"), _asset_position(coin="DOGE", szi="-100")],
        ctxs_response=_meta_and_ctxs(BTC="83096.0", ETH="2648.7", DOGE="0.093"),
    )

    positions = await adapter.get_positions()

    assert {p.market: p.mark_price for p in positions} == {
        "ETH-USDC": Decimal("2648.7"),
        "DOGE-USDC": Decimal("0.093"),
    }
    assert info.ctxs_calls == 1  # jeden dotaz pro vsechny pozice


async def test_get_positions_without_mark_price_when_fetch_fails(monkeypatch, caplog):
    adapter, _ = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH")],
        ctxs_error=TimeoutError("read timed out"),
    )

    with caplog.at_level("WARNING"):
        positions = await adapter.get_positions()

    # Pozice se vrati dal, jen bez ceny — sledovani se nezastavi.
    assert [p.market for p in positions] == ["ETH-USDC"]
    assert positions[0].mark_price is None
    assert "Hyperliquid mark prices fetch failed" in caplog.text


async def test_get_positions_market_missing_in_prices(monkeypatch):
    adapter, _ = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH")],
        ctxs_response=_meta_and_ctxs(BTC="83096.0"),
    )

    positions = await adapter.get_positions()

    assert positions[0].mark_price is None


async def test_get_positions_no_price_request_without_positions(monkeypatch):
    adapter, info = await _connected_adapter(monkeypatch)

    assert await adapter.get_positions() == []
    assert info.ctxs_calls == 0
    assert _candle_requests(info) == []


# ---------------------------------------------------------------------------
# get_positions() — posledni cena z candleSnapshot
# ---------------------------------------------------------------------------

def _candle_requests(info) -> list[dict]:
    """Obsah "req" vsech dotazu candleSnapshot, ktere adapter poslal."""
    return [payload["req"] for _, payload in info.posts if payload["type"] == "candleSnapshot"]


async def test_get_positions_fills_last_price(monkeypatch):
    adapter, info = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH"), _asset_position(coin="DOGE", szi="-100")],
        ctxs_response=_meta_and_ctxs(ETH="2648.7", DOGE="0.093"),
        candles={"ETH": [_candle("2640.0"), _candle("2650.1")], "DOGE": [_candle("0.09312")]},
    )

    positions = await adapter.get_positions()

    assert {p.market: p.last_price for p in positions} == {
        "ETH-USDC": Decimal("2650.1"),
        "DOGE-USDC": Decimal("0.09312"),
    }
    assert positions[0].mark_price == Decimal("2648.7")  # mark cena zustava jako zaloha
    requests = _candle_requests(info)
    assert sorted(r["coin"] for r in requests) == ["DOGE", "ETH"]  # jeden dotaz na trh
    assert all(r["interval"] == "1d" for r in requests)
    assert all(r["endTime"] - r["startTime"] == 2 * _DAY_MS for r in requests)


async def test_get_positions_without_last_price_when_fetch_fails(monkeypatch, caplog):
    adapter, _ = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH")],
        ctxs_response=_meta_and_ctxs(ETH="2648.7"),
        candles_error=TimeoutError("read timed out"),
    )

    with caplog.at_level("WARNING"):
        positions = await adapter.get_positions()

    # Pozice se vrati dal s mark cenou, jen bez posledni ceny — zprava ukaze Mark.
    assert positions[0].last_price is None
    assert positions[0].mark_price == Decimal("2648.7")
    assert "Hyperliquid last prices fetch failed" in caplog.text


async def test_get_positions_market_without_candles(monkeypatch):
    adapter, _ = await _connected_adapter(
        monkeypatch,
        asset_positions=[_asset_position(coin="ETH")],
        candles={"BTC": [_candle("82926.0")]},
    )

    positions = await adapter.get_positions()

    assert positions[0].last_price is None
