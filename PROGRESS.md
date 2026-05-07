# checkDEX — Progress Log

## Stav projektu

**Aktuální stav:** ✅ Projekt dokončen — Fáze 1–7 hotové, 83 testů prochází, push na GitHub

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

---

## Testovací výsledky

```
83 passed in 0.97s
```

- tests/test_event_engine.py — 28 testů (mapping + detekce)
- tests/test_pnl.py — 16 testů
- tests/test_storage.py — 20 testů
- tests/test_telegram.py — 19 testů

---

## Zbývá (volitelné)

### Fáze 8 — WebSocket vrstva (neimplementováno — polling je dostatečný)
- Privátní streamy jako doplněk pollingu
- Fallback na polling při výpadku WS

---

## Klíčové technické poznámky

- Vše běží v Dockeru: `docker compose build`, `docker compose run --rm test`, `docker compose up`
- State DB: `/data/state.db` (absolutní cesta, Docker volume `./data:/data`)
- Všechny timestampy UTC
- Logy: JSON formát (python-json-logger), přepínatelné na text přes `LOG_FORMAT=text`
- `POSITION_UPDATED` se odesílá POUZE při změně size, ne při pohybu mark price
- PnL % fallback: `(realised_pnl / (entry_price * size)) * 100`, označit jako `(approx.)`
- Race condition pro zmizelé ordery: 2 retry cykly → DISAPPEARED_UNKNOWN
