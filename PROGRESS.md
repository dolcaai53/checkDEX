# checkDEX — Progress Log

## Stav projektu

**Aktualni stav:** ✅ Projekt bezi v produkci na mainnetu (Extended) — Faze 1–7 hotove, runtime opravy overeny live. Pozdeji pridano: denni souhrn pozic, Hyperliquid adapter, vice DEXu soucasne, Telegram prikaz /positions, mark cena u Hyperliquid pozic, posledni cena (Last) u pozic a presnost cen — 232 testu prochazi. Nasazeni techto novych funkci na server neovereno.

---

## Dokončeno

### Fáze 1 — Docker základ + projekt skeleton (2026-05-07)
- `Dockerfile` — python:3.12-slim, non-root user (appuser), libgmp-dev + build-essential, file-based healthcheck
- `docker-compose.yml` — `app` service (restart: unless-stopped) + `test` service (profile: test)
- `.dockerignore`, `.gitignore`, `.env.example`, `.env.test`
- `requirements.txt` — x10-python-trading, pydantic, aiosqlite, aiohttp, python-json-logger, pytest
- `pytest.ini` — asyncio_mode=auto
- `app/config.py` — pydantic-settings, všechny env proměnné
- `app/main.py` — asyncio entry point, SIGTERM/SIGINT handlery
- `app/utils/logging.py` — JSON / text logging (python-json-logger)
- `app/models/` — Order, OrderSide, OrderType, OrderStatus, Position, PositionSide, Trade, všechny eventy
- `app/exchanges/base.py` — ExchangeAdapter ABC (multi-exchange interface)

### Fáze 3 — Extended polling connector ✅ (2026-05-07)
- `app/exceptions.py` — CheckDEXError, ExchangeAPIError, ExchangeConnectionError, ExchangeRateLimitError
- `app/utils/retry.py` — exponential backoff, HTTP 429 handling (30s wait), retryable network exceptions
- `app/exchanges/extended.py` — plná implementace: PerpetualTradingClient init, connect/disconnect, get_open_orders, get_positions, get_orders_history, get_positions_history, get_trades
- Mapování SDK → interní modely: map_order, map_position, map_position_history, map_trade
- 11 unit testů pro mapping funkce (vše bez live API)
- Zjištěno: pydantic v2 + StrEnum ukládá hodnoty jako string → mapy používají str klíče + str() konverze
- Zjištěno: Extended SDK nepodporuje time-based filtering — history vždy fetchuje prvních 50 záznamů, dedup řeší event engine

### Fáze 4 — Storage + deduplikace ✅ (2026-05-07)
- `app/storage/database.py` — aiosqlite, WAL mode, schema (schema_version tabulka)
- Tabulky: order_snapshots, position_snapshots, sent_notifications, disappeared_pending, history_cursors
- TTL cleanup sent_notifications při connect()
- 20 unit testů v `tests/test_storage.py`
- Zjištěno: `async with aiosqlite.Connection` na otevřeném spojení restartuje thread → použít execute+commit+rollback

### Fáze 5 — Event engine ✅ (2026-05-07)
- `app/utils/pnl.py` — calculate_pnl_pct (approx vzorec), pnl_label (🟢/🔴/⚪), fmt_pnl, fmt_pct
- `app/services/event_engine.py` — pure funkce: detect_order_events, detect_position_events, detect_closed_positions
- EventEngine class: process_orders, process_positions, process_positions_history
- First-run: snapshoty se tichce naplní (bez notifikací)
- Race condition: disappeared_pending + 2 retry → DISAPPEARED_UNKNOWN
- 19 unit testů pro detekční logiku + 15 testů pro PnL utils

### Fáze 6 — Telegram notifier ✅ (2026-05-07)
- `app/notifiers/telegram.py` — format_* funkce + TelegramNotifier class
- Startup notifikace, šablony pro všechny eventy (ORDER_OPENED, ORDER_UPDATED, ORDER_FILLED, POSITION_OPENED, POSITION_UPDATED, POSITION_CLOSED)
- Profit/loss/breakeven: 🟢 PROFIT / 🔴 LOSS / ⚪ BREAKEVEN
- Dedup via db.is_notified/mark_notified; retry via with_retry; pouze Telegramem podporované HTML tagy
- 19 unit testů v `tests/test_telegram.py`

### Fáze 7 — Zapojení + hlavní smyčka ✅ (2026-05-07)
- `app/services/monitor.py` — 3 polling smyčky (orders, positions, history), interruptible_sleep
- `app/main.py` — inicializace všech komponent, startup notifikace, graceful shutdown via monitor.stop()
- Healthcheck: _touch_healthy() po každém úspěšném poll cyklu → /tmp/healthy
- `README.md` — kompletní dokumentace projektu

### GitHub push ✅
- `git init`, initial commit (40 souborů, 3614 řádků)
- Push na https://github.com/dolcaai53/checkDEX.git (branch: main)

### Denni souhrn pozic ✅ (2026-05-07, commit 355d567)
- `ENABLE_DAILY_SUMMARY`, `DAILY_SUMMARY_TIME` — jednou denne zprava 📊 DAILY POSITION SUMMARY

### Hyperliquid adapter ✅ (2026-05-12, commit 169c9f1)
- `app/exchanges/hyperliquid.py` — read-only pres `hyperliquid-python-sdk` (`Info`), staci `HYPERLIQUID_WALLET_ADDRESS`, zadny privatni klic
- 18 unit testu v `tests/test_hyperliquid.py`

### Vice DEXu soucasne ✅ (2026-05-12, commit 2f235b2)
- `ACTIVE_EXCHANGES=extended,hyperliquid` — pro kazdou burzu vlastni Monitor, spolecna DB a Telegram notifier

### Telegram prikaz /positions ✅ (2026-09-27)
- `app/services/commands.py` — `TelegramCommandListener`: long polling `getUpdates`, zadny webhook ani otevreny port
- `/positions` (ve skupine `/positions@JmenoBota`) posle aktualni pozice vsech sledovanych burz, data primo z API burzy
- Odpovida jen v chatu `TELEGRAM_CHAT_ID`; prikazy starsi nez 5 min se ignoruji; offset v `history_cursors`, takze po restartu zadna dvojita odpoved
- Burza, ktera neodpovi, je ve zprave oznacena "Data unavailable" — ostatni se vypisou
- Bot token se v logu listeneru maskuje (`bot***`)
- `ENABLE_TELEGRAM_COMMANDS=true` (vychozi); `false` = vypnuto
- Denni souhrn a /positions sdili formatovani pozic (`_position_lines`)
- 17 testu v `tests/test_commands.py`, 4 nove testy v `tests/test_telegram.py` (vcetne kontroly, ze se vzhled denniho souhrnu nezmenil)

### Odolnost pri vice burzach + Hyperliquid opravy ✅ (2026-09-27)
- Kazda burza bezi samostatne (`run_exchange` v `app/main.py`): kdyz se jedna nepripoji, ostatni monitoruji dal
- Neuspesne pripojeni: log `Exchange connection failed` + jednou ⚠️ Telegram zprava (`send_exchange_error`, token maskovany), dalsi pokus po 30 s, pak 2x delsi az 10 min
- Neocekavana chyba ukonci proces (Docker ho restartuje) — driv proces visel jako `unhealthy`
- Hyperliquid: timeout 15 s na kazdy request (SDK zadny nema), konstruktor `Info` bezi ve vlakne
- Hyperliquid: pri startu kontrola `userRole` — API (agent) penezenka se odmitne a chyba uvede adresu hlavniho uctu
- `ACTIVE_EXCHANGES` funguje s carkou (`extended,hyperliquid`) i jako JSON seznam
- Prvni spusteni se sleduje pro kazdou burzu zvlast (markery `*_initialized:<burza>` v `history_cursors`): prvni order/pozice na prazdnem uctu se oznami, nove pridana burza neposle davku starych POSITION CLOSED
- `requirements.txt` — presne verze knihoven, se kterymi prochazeji testy
- Nove testy: `tests/test_config.py` (13), `tests/test_main.py` (8), dalsi v event_engine, hyperliquid, storage, telegram

### Mark cena u Hyperliquid pozic ✅ (2026-09-28)
- Hyperliquid v pozicich (`clearinghouseState`) mark cenu neposila — radek `Mark` byl u Hyperliquid vzdy `—`
- `get_positions()` ji doplni z verejneho dotazu `metaAndAssetCtxs` (`parse_mark_prices`), jen kdyz jsou otevrene pozice
- Kdyz dotaz selze: varovani v logu, pozice se sleduji dal bez ceny
- Projevi se v dennim souhrnu, `/positions` i `POSITION UPDATED`; Extended posila mark cenu s pozicemi uz driv
- Overeno proti skutecnemu API (hlavni ucet): `Mark` u BTC a GRAM odpovida uPnL
- 15 novych testu v `tests/test_hyperliquid.py`

### Posledni cena (Last) u pozic a presnost cen ✅ (2026-09-28)
- Aktualni cena pozice = posledni obchodni cena (`Last`); kdyz neni, stejny radek ukaze `Mark` — zadny radek navic
- Projevi se v dennim souhrnu, `/positions` i `POSITION UPDATED`; notifikace se dal posilaji jen pri zmene velikosti pozice
- Nove pole `Position.last_price` (volitelne — stare snapshoty v DB se nactou bez nej)
- Extended: `marketStats.lastPrice` z verejneho `GET /info/markets` — jeden dotaz pro vsechny trhy s pozici, timeout 15 s (`parse_last_prices`)
- Hyperliquid: zaviraci cena posledni denni svicky (`candleSnapshot`, okno 2 dny) — jeden dotaz na trh (`parse_last_price`)
- Kdyz dotaz selze: varovani v logu, zprava ukaze `Mark`, pozice se sleduji dal
- Ceny ve vsech zpravach (ordery, pozice, uzavrene obchody) na nejvyse 7 platnych cislic bez nul na konci (`_price`): `0.004081` zustane, `80079.584688000002` → `80079.58`, cela cast se nezaokrouhluje; PnL dal na 2 desetinna mista
- Overeno proti skutecnemu API: Extended `lastPrice` odpovida poslednimu verejnemu obchodu, Hyperliquid zaviraci cena svicky = posledni obchod (`recentTrades`); Extended ucet mel pri overeni zadne otevrene pozice — cesta s pozicemi overena unit testy
- Nove testy: `tests/test_extended.py` (10), dalsi v hyperliquid (13), telegram (16), event_engine (1)

---

## Testovací výsledky

```
232 passed, 1 warning   (2026-09-28, v Dockeru; warning = DeprecationWarning z python-json-logger)
```

- tests/test_commands.py — 17 testu (/positions, getUpdates)
- tests/test_config.py — 13 testu (ACTIVE_EXCHANGES carka/JSON, .env.example)
- tests/test_event_engine.py — 38 testu (mapping + detekce + prvni spusteni)
- tests/test_extended.py — 10 testu (posledni ceny z /info/markets)
- tests/test_hyperliquid.py — 60 testu (mapping + connect, kontrola API penezenky, mark a posledni ceny)
- tests/test_main.py — 8 testu (opakovani pripojeni, nezavislost burz, uklid)
- tests/test_pnl.py — 16 testu
- tests/test_storage.py — 25 testu
- tests/test_telegram.py — 45 testu (vcetne presnosti cen a radku Last/Mark)

---

## Zbývá (volitelné)

### Fáze 8 — WebSocket vrstva (neimplementováno — polling je dostatečný)
- Privátní streamy jako doplněk pollingu
- Fallback na polling při výpadku WS

---

## Runtime opravy při nasazení (2026-05-07)

| Problém | Příčina | Oprava |
|---|---|---|
| `pydantic ValidationError` při startu | `UNREALIZED_PNL_THRESHOLD_USDC=` prázdný string nelze parsovat jako `float` | `@field_validator` s `empty_str_to_none` |
| `404` na všech API endpointech | SDK má zastaralou doménu `api.extended.exchange`; správná je `api.starknet.extended.exchange` | `dataclasses.replace()` v `connect()` |
| `401 Unauthorized` | Extended Exchange vyžaduje `X-Client-Id` header (nový požadavek); SDK ho nepodporuje | Injekce custom `aiohttp.ClientSession` s defaultním headerem do SDK's `_BaseModule__session` |
| `get_open_orders failed: unknown error` | pydantic v2 ukládá `ResponseStatus` jako string `'OK'`, ne enum; `!=` vrací vždy `True` | `_unwrap()` porovnává proti oběma variantám |
| `ValidationError` v `get_orders_history` | MARKET ordery v historii nemají `price` field; SDK model vyžaduje ho jako povinný | Raw HTTP volání s `_map_raw_order()` (toleruje chybějící `price`) |
| `EXTENDED_CLIENT_ID` vs `EXTENDED_VAULT` | Client ID je samostatná hodnota generovaná spolu s API klíčem v Extended Exchange UI | Nový config field `EXTENDED_CLIENT_ID`; fallback na vault pokud není nastaven |

## Runtime opravy pri nasazeni (2026-09-27)

| Problem | Pricina | Oprava |
|---|---|---|
| `ModuleNotFoundError: No module named 'hyperliquid'`, proces visel | Na server se nezkopiroval novy `requirements.txt`; pripojeni burzy bylo mimo try/finally | Zkopirovat `requirements.txt` + `docker compose build --no-cache`; v kodu nezavisle burzy s opakovanim pripojeni |
| Hyperliquid neukazuje zadne pozice | V `HYPERLIQUID_WALLET_ADDRESS` byla API (agent) penezenka | Nastavit adresu hlavniho uctu; aplikace ted API penezenku sama pozna a nahlasi |

## Klíčové technické poznámky

- Vše běží v Dockeru: `docker compose build`, `docker compose run --rm test`, `docker compose up`
- State DB: `/data/state.db` (absolutní cesta, Docker volume `./data:/data`)
- Všechny timestampy UTC
- Logy: JSON formát (python-json-logger), přepínatelné na text přes `LOG_FORMAT=text`
- `POSITION_UPDATED` se odesílá POUZE při změně size, ne při pohybu mark price
- PnL % fallback: `(realised_pnl / (entry_price * size)) * 100`, označit jako `(approx.)`
- Race condition pro zmizelé ordery: 2 retry cykly → DISAPPEARED_UNKNOWN
- Extended Exchange API doména: `https://api.starknet.extended.exchange/api/v1` (SDK má zastaralou)
- `X-Client-Id` header = Client ID z Extended Exchange UI (ne Vault Number)
- MARKET ordery v orders history nemají `price` field → raw HTTP + vlastní parser
