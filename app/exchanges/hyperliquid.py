from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.config import Config
from app.exceptions import ExchangeConnectionError
from app.exchanges.base import ExchangeAdapter
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.position import Position, PositionSide
from app.models.trade import Trade

logger = logging.getLogger(__name__)

_MAINNET_URL = "https://api.hyperliquid.xyz"
_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

# Casovy limit jednoho HTTP dotazu na Hyperliquid API. SDK sam zadny nenastavuje,
# takze bez nej muze dotaz pri vypadku site viset donekonecna.
_REQUEST_TIMEOUT_SECONDS = 15.0

# Okno dotazu na denni svicky pro posledni cenu: zahrne vcerejsi i dnesni svicku,
# takze cena se najde i tesne po pulnoci UTC, kdy dnes jeste nebyl obchod.
_LAST_PRICE_WINDOW_MS = 2 * 24 * 60 * 60 * 1000

_HL_BUY = "B"
_HL_SELL = "A"

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.OPEN,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "marginCancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def _unix_ms_to_utc(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def _normalize_market(coin: str) -> str:
    """Normalize Hyperliquid coin name (e.g. 'BTC') to 'BTC-USDC' market pair format."""
    return f"{coin}-USDC"


def map_order(hl_order: dict, exchange: str) -> Order:
    """Map Hyperliquid open order dict to internal Order model.

    The SDK wraps the order in {"order": {...}, "status": "open"}.
    This function handles both the wrapped and unwrapped forms.
    """
    inner = hl_order.get("order", hl_order)
    side = OrderSide.BUY if inner.get("side") == _HL_BUY else OrderSide.SELL

    orig_sz = Decimal(str(inner.get("origSz") or inner["sz"]))
    remaining_sz = Decimal(str(inner["sz"]))
    filled_qty = orig_sz - remaining_sz if orig_sz > remaining_sz else Decimal("0")
    status = OrderStatus.PARTIAL_FILL if filled_qty > 0 else OrderStatus.OPEN

    price_raw = inner.get("limitPx")
    price = (
        Decimal(str(price_raw))
        if price_raw and str(price_raw) not in ("0", "0.0", "")
        else None
    )

    ts = inner.get("timestamp") or hl_order.get("statusTimestamp")
    created_at = _unix_ms_to_utc(ts) if ts else datetime.now(timezone.utc)

    return Order(
        id=str(inner["oid"]),
        exchange=exchange,
        market=_normalize_market(inner["coin"]),
        side=side,
        type=OrderType.LIMIT if price else OrderType.MARKET,
        price=price,
        qty=orig_sz,
        filled_qty=filled_qty,
        status=status,
        created_at=created_at,
        updated_at=None,
    )


def map_position(
    hl_asset_pos: dict,
    exchange: str,
    mark_price: Decimal | None = None,
    last_price: Decimal | None = None,
) -> Position:
    """Map Hyperliquid assetPosition dict to internal Position model.

    Hyperliquid v assetPositions mark ani posledni cenu neposila — adapter je
    nacita zvlast (viz parse_mark_prices, parse_last_price) a predava
    v *mark_price* a *last_price*.
    """
    pos = hl_asset_pos.get("position", hl_asset_pos)
    szi = Decimal(str(pos["szi"]))
    side = PositionSide.LONG if szi > 0 else PositionSide.SHORT
    size = abs(szi)

    leverage_info = pos.get("leverage") or {}
    leverage_val = leverage_info.get("value")
    leverage = Decimal(str(leverage_val)) if leverage_val is not None else None

    upnl_raw = pos.get("unrealizedPnl")
    unrealized_pnl = Decimal(str(upnl_raw)) if upnl_raw is not None else None

    return Position(
        market=_normalize_market(pos["coin"]),
        exchange=exchange,
        side=side,
        size=size,
        entry_price=Decimal(str(pos["entryPx"])),
        mark_price=mark_price,
        last_price=last_price,
        leverage=leverage,
        unrealized_pnl=unrealized_pnl,
        opened_at=None,
    )


def map_fill_to_trade(hl_fill: dict, exchange: str) -> Trade:
    """Map a closing Hyperliquid fill to an internal Trade (closed position).

    NOTE: entry_price is not available in fill data. We use exit_price as a
    stand-in so that PnL % will be marked as approximate in notifications.
    """
    dir_str = hl_fill.get("dir", "")
    if "Long" in dir_str:
        side = PositionSide.LONG
    elif "Short" in dir_str:
        side = PositionSide.SHORT
    else:
        # Fallback: infer from fill side field
        side = PositionSide.LONG if hl_fill.get("side") == _HL_BUY else PositionSide.SHORT

    exit_price = Decimal(str(hl_fill["px"]))
    closed_pnl = Decimal(str(hl_fill.get("closedPnl", "0")))
    fill_id = str(hl_fill.get("tid") or hl_fill.get("oid") or "0")

    return Trade(
        id=fill_id,
        exchange=exchange,
        market=_normalize_market(hl_fill["coin"]),
        side=side,
        size=Decimal(str(hl_fill["sz"])),
        entry_price=exit_price,  # approximation — entry not in fill data
        exit_price=exit_price,
        realised_pnl=closed_pnl,
        opened_at=None,
        closed_at=_unix_ms_to_utc(hl_fill["time"]),
    )


def parse_user_role(response: object) -> tuple[str | None, str | None]:
    """Z odpovedi Hyperliquid userRole vrati (role, adresa hlavniho uctu).

    Role je napr. "user" (bezny ucet), "agent" (API penezenka) nebo "missing"
    (adresa, kterou Hyperliquid nezna). Adresu hlavniho uctu vraci API jen
    u role "agent" v data.user.
    """
    if not isinstance(response, dict):
        return None, None
    role = response.get("role")
    data = response.get("data")
    main_account = data.get("user") if role == "agent" and isinstance(data, dict) else None
    return (str(role) if role else None), (str(main_account) if main_account else None)


def parse_mark_prices(response: object) -> dict[str, Decimal]:
    """Z odpovedi Hyperliquid metaAndAssetCtxs vrati {coin: mark cena}.

    Odpoved je [meta, assetCtxs]; meta["universe"][i] a assetCtxs[i] patri ke
    stejnemu trhu. Trh bez platne ceny se vynecha, necekany tvar odpovedi
    vrati prazdny slovnik.
    """
    if not isinstance(response, list) or len(response) < 2:
        return {}
    meta, ctxs = response[0], response[1]
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(ctxs, list):
        return {}

    prices: dict[str, Decimal] = {}
    for asset, ctx in zip(universe, ctxs):
        try:
            prices[str(asset["name"])] = Decimal(str(ctx["markPx"]))
        except (KeyError, TypeError, ArithmeticError):
            continue
    return prices


def parse_last_price(candles: object) -> Decimal | None:
    """Z odpovedi Hyperliquid candleSnapshot vrati posledni obchodni cenu.

    Svicky jsou serazene od nejstarsi; zaviraci cena ("c") nejnovejsi svicky
    je cena posledniho obchodu. Prazdna nebo necekana odpoved vrati None.
    """
    if not isinstance(candles, list) or not candles:
        return None
    try:
        return Decimal(str(candles[-1]["c"]))
    except (KeyError, TypeError, ArithmeticError):
        return None


async def _fills_by_time(info, address: str, start_ms: int) -> list[dict]:
    """Fetch fills since start_ms. Falls back to user_fills() if SDK lacks time filtering."""
    try:
        return await asyncio.to_thread(info.user_fills_by_time, address, start_ms)
    except AttributeError:
        all_fills: list[dict] = await asyncio.to_thread(info.user_fills, address)
        return [f for f in all_fills if f.get("time", 0) >= start_ms]


class HyperliquidAdapter(ExchangeAdapter):
    """Exchange adapter for Hyperliquid DEX.

    Authentication: read-only endpoints require only a wallet address (0x…).
    No private key is needed for monitoring. It must be the main account, not an
    API (agent) wallet — connect() checks this and names the main account.

    Sync SDK: hyperliquid-python-sdk uses synchronous requests internally. All
    SDK calls are dispatched via asyncio.to_thread() to avoid blocking the loop.

    Cancelled orders: Hyperliquid has no bulk cancelled-order history endpoint.
    Orders that disappear without appearing in fills will exhaust the
    disappeared_pending retry queue and be reported as DISAPPEARED_UNKNOWN.

    Entry price in closed trades: fill data contains only the exit price.
    entry_price is set to exit_price as an approximation; PnL % is therefore
    an estimate based on (realised_pnl / (exit_price * size)).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._info = None

    @property
    def exchange_name(self) -> str:
        return "Hyperliquid"

    @property
    def network(self) -> str:
        return "testnet" if self._config.hyperliquid_testnet else "mainnet"

    async def connect(self) -> None:
        try:
            from hyperliquid.info import Info  # type: ignore[import]
        except ImportError as exc:
            raise ExchangeConnectionError(
                "hyperliquid-python-sdk is not installed; add it to requirements.txt"
            ) from exc

        base_url = _TESTNET_URL if self._config.hyperliquid_testnet else _MAINNET_URL
        try:
            # Konstruktor Info stahuje metadata trhu blokujicim HTTP — proto ve vlakne.
            info = await asyncio.to_thread(
                Info, base_url, skip_ws=True, timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except Exception as exc:
            raise ExchangeConnectionError(f"Hyperliquid client init failed: {exc}") from exc

        await self._check_wallet_role(info)
        self._info = info
        logger.info(
            "Hyperliquid adapter initialised",
            extra={"network": "testnet" if self._config.hyperliquid_testnet else "mainnet"},
        )

    async def _check_wallet_role(self, info) -> None:
        """Odmitne API (agent) penezenku v HYPERLIQUID_WALLET_ADDRESS.

        API penezenka nema vlastni pozice ani ordery — patri hlavnimu uctu,
        takze by monitoring nic nevidel. Chyba rovnou uvede adresu hlavniho uctu.
        Kdyz se roli nepodari zjistit, jen se zaloguje varovani a monitoring
        pokracuje (kontrola nesmi zablokovat jinak funkcni pripojeni).
        """
        address = self._address()
        try:
            response = await asyncio.to_thread(
                info.post, "/info", {"type": "userRole", "user": address}
            )
        except Exception as exc:
            logger.warning(
                "Hyperliquid wallet role check failed, continuing",
                extra={"address": address, "error": str(exc)},
            )
            return

        role, main_account = parse_user_role(response)
        logger.info("Hyperliquid wallet role", extra={"address": address, "role": role})
        if role == "agent":
            # Adresa az na konci a bez tecky, aby sla z Telegramu primo zkopirovat.
            fix = (
                f"Set HYPERLIQUID_WALLET_ADDRESS to the main account address: {main_account}"
                if main_account
                else "Set HYPERLIQUID_WALLET_ADDRESS to the main account address shown on hyperliquid.xyz"
            )
            raise ExchangeConnectionError(
                f"HYPERLIQUID_WALLET_ADDRESS {address} is an API (agent) wallet "
                f"without positions or orders of its own. {fix}"
            )
        if role == "missing":
            logger.warning(
                "Hyperliquid does not know HYPERLIQUID_WALLET_ADDRESS (no account activity), "
                "check the address",
                extra={"address": address},
            )

    async def disconnect(self) -> None:
        self._info = None
        logger.info("Hyperliquid adapter disconnected")

    def _info_or_raise(self):
        if self._info is None:
            raise ExchangeConnectionError(
                "HyperliquidAdapter not connected — call connect() first"
            )
        return self._info

    def _address(self) -> str:
        addr = self._config.hyperliquid_wallet_address
        if not addr:
            raise ExchangeConnectionError("HYPERLIQUID_WALLET_ADDRESS is not configured")
        return addr

    async def get_open_orders(self) -> list[Order]:
        info = self._info_or_raise()
        address = self._address()
        try:
            raw: list[dict] = await asyncio.to_thread(info.open_orders, address)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_open_orders failed: {exc}") from exc

        orders: list[Order] = []
        for item in raw:
            try:
                orders.append(map_order(item, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable open order",
                    extra={"item": str(item)[:120]},
                )
        logger.debug("Fetched open orders", extra={"count": len(orders)})
        return orders

    async def get_positions(self) -> list[Position]:
        info = self._info_or_raise()
        address = self._address()
        try:
            state: dict = await asyncio.to_thread(info.user_state, address)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_positions failed: {exc}") from exc

        asset_positions = state.get("assetPositions", [])
        coins = [
            ap["position"]["coin"] for ap in asset_positions if ap.get("position", {}).get("coin")
        ]
        mark_prices: dict[str, Decimal] = {}
        last_prices: dict[str, Decimal] = {}
        if coins:
            # Ceny se nacitaji jen kdyz jsou otevrene pozice — jinak neni co doplnit.
            mark_prices, last_prices = await asyncio.gather(
                self._get_mark_prices(info), self._get_last_prices(info, coins)
            )

        positions: list[Position] = []
        for asset_pos in asset_positions:
            pos = asset_pos.get("position", {})
            if Decimal(str(pos.get("szi", "0"))) == 0:
                continue
            coin = pos.get("coin")
            try:
                positions.append(
                    map_position(
                        asset_pos, self.exchange_name, mark_prices.get(coin), last_prices.get(coin)
                    )
                )
            except Exception:
                logger.debug(
                    "Skipping unparseable position",
                    extra={"coin": pos.get("coin")},
                )
        logger.debug("Fetched positions", extra={"count": len(positions)})
        return positions

    async def _get_mark_prices(self, info) -> dict[str, Decimal]:
        """Aktualni mark ceny vsech trhu (verejny dotaz metaAndAssetCtxs).

        Cena je ve zpravach jen informativni — kdyz dotaz selze, zaloguje se
        varovani a pozice se vrati bez ni (sledovani pozic se nezastavi).
        """
        try:
            response = await asyncio.to_thread(info.meta_and_asset_ctxs)
        except Exception as exc:
            logger.warning("Hyperliquid mark prices fetch failed", extra={"error": str(exc)})
            return {}
        return parse_mark_prices(response)

    async def _get_last_prices(self, info, coins: list[str]) -> dict[str, Decimal]:
        """Posledni obchodni ceny trhu s otevrenou pozici (verejny dotaz candleSnapshot).

        Hyperliquid posledni cenu primo neposila — bere se zaviraci cena nejnovejsi
        denni svicky. Jeden dotaz na trh, dotazy bezi soubezne. Trh, u ktereho
        dotaz selze, zustane bez ceny a zprava ukaze mark cenu.
        """
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        request = {"interval": "1d", "startTime": end_ms - _LAST_PRICE_WINDOW_MS, "endTime": end_ms}
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    info.post, "/info", {"type": "candleSnapshot", "req": {"coin": coin, **request}}
                )
                for coin in coins
            ),
            return_exceptions=True,
        )

        prices: dict[str, Decimal] = {}
        errors: dict[str, str] = {}
        for coin, result in zip(coins, results):
            if isinstance(result, BaseException):
                errors[coin] = f"{type(result).__name__}: {result}"
                continue
            price = parse_last_price(result)
            if price is not None:
                prices[coin] = price
        if errors:
            logger.warning("Hyperliquid last prices fetch failed", extra={"errors": errors})
        return prices

    async def get_orders_history(self, since: datetime) -> list[Order]:
        """Return recently filled orders reconstructed from user fills.

        Cancelled orders are not available via bulk history. They will be
        handled by the disappeared_pending retry logic in the event engine
        and ultimately reported as DISAPPEARED_UNKNOWN if not found here.
        """
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_orders_history failed: {exc}") from exc

        orders: list[Order] = []
        seen_oids: set[str] = set()
        for fill in fills:
            oid = str(fill.get("oid", ""))
            if not oid or oid in seen_oids:
                continue
            seen_oids.add(oid)
            try:
                side = OrderSide.BUY if fill.get("side") == _HL_BUY else OrderSide.SELL
                ts = fill.get("time")
                orders.append(
                    Order(
                        id=oid,
                        exchange=self.exchange_name,
                        market=_normalize_market(fill["coin"]),
                        side=side,
                        type=OrderType.LIMIT,
                        price=Decimal(str(fill["px"])),
                        qty=Decimal(str(fill["sz"])),
                        filled_qty=Decimal(str(fill["sz"])),
                        status=OrderStatus.FILLED,
                        created_at=_unix_ms_to_utc(ts) if ts else datetime.now(timezone.utc),
                        updated_at=_unix_ms_to_utc(ts) if ts else None,
                    )
                )
            except Exception:
                logger.debug(
                    "Skipping unparseable fill in orders_history",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched orders history (from fills)", extra={"count": len(orders)})
        return orders

    async def get_positions_history(self, since: datetime) -> list[Trade]:
        """Return closed position trades: fills where closedPnl != 0."""
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_positions_history failed: {exc}") from exc

        trades: list[Trade] = []
        for fill in fills:
            if Decimal(str(fill.get("closedPnl", "0"))) == 0:
                continue
            try:
                trades.append(map_fill_to_trade(fill, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable position close fill",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched positions history", extra={"count": len(trades)})
        return trades

    async def get_trades(self, since: datetime) -> list[Trade]:
        """Return all individual fill events since the given time."""
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_trades failed: {exc}") from exc

        trades: list[Trade] = []
        for fill in fills:
            try:
                trades.append(map_fill_to_trade(fill, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable fill in get_trades",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched trades", extra={"count": len(trades)})
        return trades
