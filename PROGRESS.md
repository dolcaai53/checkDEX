# checkDEX — Progress Log

## Stav projektu

**Aktuální fáze:** Fáze 4 dokončena — připraveno na Fázi 5 (Event engine) + Fázi 6 (Telegram notifier)

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
- `app/exchanges/extended.py` — stub, TODO Phase 3
- `app/notifiers/telegram.py` — stub, TODO Phase 6
- `app/services/monitor.py` — stub, TODO Phase 7
- `app/storage/database.py` — stub, TODO Phase 4
- `tests/` — 11 skeleton testů, vše prochází
- Ověřeno: `docker compose build` ✅, `docker compose run --rm test` ✅ (11/11)

---

## Zbývá

### Fáze 2 — modely (přeskočena — modely jsou součástí Fáze 1)

### Fáze 3 — Extended polling connector ✅ (2026-05-07)
- [x] `app/exceptions.py` — CheckDEXError, ExchangeAPIError, ExchangeConnectionError, ExchangeRateLimitError
- [x] `app/utils/retry.py` — exponential backoff, HTTP 429 handling (30s wait), retryable network exceptions
- [x] `app/exchanges/extended.py` — plná implementace: PerpetualTradingClient init, connect/disconnect, get_open_orders, get_positions, get_orders_history, get_positions_history, get_trades
- [x] Mapování SDK → interní modely: map_order, map_position, map_position_history, map_trade
- [x] `app/models/order.py` — přidány OrderType.CONDITIONAL a TPSL
- [x] `app/exchanges/base.py` — get_positions_history vrací list[Trade] (ne list[Position])
- [x] 11 unit testů pro mapping funkce (vše bez live API)
- [x] 22/22 testů prochází v Dockeru
- Zjištěno: pydantic v2 + StrEnum ukládá hodnoty jako string → mapy používají str klíče + str() konverze
- Zjištěno: Extended SDK nepodporuje time-based filtering — history vždy fetchuje prvních 50 záznamů, dedup řeší event engine

### Fáze 4 — Storage + deduplikace ✅ (2026-05-07)
- [x] `app/storage/database.py` — aiosqlite, WAL mode, schema (schema_version tabulka)
- [x] Tabulky: order_snapshots, position_snapshots, sent_notifications, disappeared_pending, history_cursors
- [x] TTL cleanup sent_notifications při connect() a po každé NOTIFICATION_DEDUP_TTL_DAYS
- [x] 20 unit testů v `tests/test_storage.py` — 40/40 prochází
- Zjištěno: `async with aiosqlite.Connection` na otevřeném spojení restartuje thread → použít execute+commit+rollback

### Fáze 5 — Event engine
- [ ] Order diff logika (nový, změněný, zmizelý order)
- [ ] Position diff logika (nová, změněná pozice — pouze při změně size)
- [ ] History scan pro uzavřené pozice / trades
- [ ] Race condition handling pro zmizelé ordery (disappeared_pending, 2 retry)
- [ ] PnL výpočet + PnL % (fallback vzorec)
- [ ] Unit testy v `tests/test_event_engine.py`, `tests/test_pnl.py`

### Fáze 6 — Telegram notifier
- [ ] `app/notifiers/telegram.py` — aiohttp, HTML šablony, retry
- [ ] Startup notifikace
- [ ] Šablony pro všechny eventy (ORDER_OPENED, ORDER_UPDATED, ORDER_FILLED, POSITION_OPENED, POSITION_UPDATED, POSITION_CLOSED)
- [ ] Profit/loss/breakeven emoji + HTML formátování
- [ ] Unit testy v `tests/test_telegram.py`

### Fáze 7 — Zapojení + hlavní smyčka
- [ ] `app/services/monitor.py` — 3 polling smyčky (orders, positions, history)
- [ ] `app/main.py` — wire up všechny komponenty
- [ ] Healthcheck touch /tmp/healthy
- [ ] Graceful shutdown

### Fáze 8 — WebSocket vrstva (volitelná)
- [ ] Privátní streamy jako doplněk pollingu
- [ ] Fallback na polling při výpadku WS

---

## Otevřená rozhodnutí / bloky

- Ověřit, zda Extended SDK vyžaduje private_key i pro read-only volání při inicializaci StarkPerpetualAccount (vyplyne při implementaci Fáze 3)
- WebSocket implementace závisí na kvalitě SDK dokumentace (konzervativní fallback = polling only)

---

## Klíčové technické poznámky

- Vše běží v Dockeru: `docker compose build`, `docker compose run --rm test`, `docker compose up`
- State DB: `/data/state.db` (absolutní cesta, Docker volume `./data:/data`)
- Všechny timestampy UTC
- Logy: JSON formát (python-json-logger), přepínatelné na text přes `LOG_FORMAT=text`
- `POSITION_UPDATED` se odesílá POUZE při změně size, ne při pohybu mark price
- PnL % fallback: `(realised_pnl / (entry_price * size)) * 100`, označit jako `(approx.)`
- Race condition pro zmizelé ordery: 2 retry cykly → DISAPPEARED_UNKNOWN
