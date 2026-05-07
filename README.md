# checkDEX

Read-only trading monitor for decentralised exchanges. Watches your account on Extended Exchange, detects order and position events, and sends formatted Telegram notifications.

## What it does

- Monitors open orders, partial fills, full fills, cancels, and rejects.
- Monitors open positions and size changes.
- Detects closed positions with realised PnL (profit / loss / breakeven).
- Sends all events as HTML-formatted messages to a Telegram chat.
- Deduplicates notifications so the same event is never sent twice, even after a restart.
- Designed from the start for multi-exchange extensibility (Lighter, Hyperliquid, etc.).

## Architecture

```
app/
  config.py             — pydantic-settings configuration from env vars
  main.py               — entry point; wires all components and handles SIGTERM/SIGINT
  exchanges/
    base.py             — ExchangeAdapter ABC (multi-exchange interface)
    extended.py         — Extended Exchange implementation
  models/
    order.py            — Order model and enums
    position.py         — Position model and enums
    trade.py            — Trade model (closed positions)
    events.py           — OrderOpenedEvent, OrderFilledEvent, PositionClosedEvent, …
  services/
    event_engine.py     — pure diff functions + EventEngine orchestrator
    monitor.py          — three independent polling loops
  notifiers/
    telegram.py         — Telegram Bot API notifier with dedup and retry
  storage/
    database.py         — aiosqlite persistence layer
  utils/
    pnl.py              — PnL calculation and formatting
    retry.py            — exponential backoff retry helper
    logging.py          — structured JSON logging setup
tests/                  — pytest test suite (83 tests, no network required)
```

## Extended Exchange integration

Authentication uses the official [x10-python-trading](https://github.com/x10xchange/python_sdk) SDK. When you generate API keys in the Extended Exchange web UI, five values are produced — all five are required:

| Variable | Description |
|---|---|
| `EXTENDED_API_KEY` | API key — sent as `X-Api-Key` header |
| `EXTENDED_PUBLIC_KEY` | Stark public key (0x…) |
| `EXTENDED_PRIVATE_KEY` | Stark private key (0x…) |
| `EXTENDED_VAULT` | Vault Number |
| `EXTENDED_CLIENT_ID` | Client ID — sent as `X-Client-Id` header |

`EXTENDED_PRIVATE_KEY` and `EXTENDED_PUBLIC_KEY` are passed to the SDK initialiser but never used for signing — this is a read-only system.

**API endpoint note:** The SDK ships with an outdated base URL (`api.extended.exchange`). checkDEX automatically patches it to the current endpoint `api.starknet.extended.exchange` at startup — no manual change is needed.

### Polling mode

All data is fetched via periodic polling — there is no WebSocket layer. Three independent asyncio loops run concurrently:

| Loop | Data source | Default interval |
|---|---|---|
| Orders | `get_open_orders()` + `get_orders_history()` | 60 s |
| Positions | `get_positions()` | 60 s |
| History | `get_positions_history()` | 60 s |

Intervals are configurable via `POLL_INTERVAL_ORDERS_SECONDS`, `POLL_INTERVAL_POSITIONS_SECONDS`, `POLL_INTERVAL_HISTORY_SECONDS`.

The Extended SDK uses cursor-based pagination with no time-based filtering. checkDEX always fetches the most recent 50 records from history endpoints and relies on ID-based deduplication in the database.

**WebSocket mode** is not implemented. The polling baseline is reliable and sufficient for 60-second intervals.

## Setting up the Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the bot token.
2. Add the bot to your target group (or start a private chat with it).
3. Get the chat ID: send a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id":…}`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your `.env`.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```
cp .env.example .env
$EDITOR .env
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `EXTENDED_API_KEY` | — | Required |
| `EXTENDED_PUBLIC_KEY` | — | Required |
| `EXTENDED_PRIVATE_KEY` | — | Required |
| `EXTENDED_VAULT` | — | Required (Vault Number) |
| `EXTENDED_CLIENT_ID` | — | Required (Client ID; falls back to EXTENDED_VAULT if unset) |
| `EXTENDED_NETWORK` | `mainnet` | `mainnet` or `testnet` |
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `POLL_INTERVAL_ORDERS_SECONDS` | `60` | Orders polling interval |
| `POLL_INTERVAL_POSITIONS_SECONDS` | `60` | Positions polling interval |
| `POLL_INTERVAL_HISTORY_SECONDS` | `60` | History polling interval |
| `STATE_DB_PATH` | `/data/state.db` | SQLite file path |
| `NOTIFICATION_DEDUP_TTL_DAYS` | `30` | How long to keep sent-notification IDs |
| `ENABLE_ORDER_OPENED` | `true` | Toggle individual notification types |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `json` | `json` (default) or `text` |

## Running in Docker (recommended)

```bash
# Build and start
docker compose up -d

# View logs
docker logs -f checkdex-app-1

# Stop
docker compose down
```

The `data/` directory on the host is mounted as `/data` in the container. The SQLite database is written there. This directory persists across container restarts and image updates.

### Updating the image without losing state

```bash
# Pull / rebuild the image
docker compose build --pull

# Restart with the new image
docker compose up -d
```

The `data/` volume is never removed by these commands. No state is lost, no old notifications are re-sent.

### Running tests

```bash
docker compose run --rm --build test
```

No API keys or network access required — all tests use in-memory SQLite and mock objects.

## Persistence and deduplication

The SQLite database stores:

| Table | Purpose |
|---|---|
| `order_snapshots` | Last known state of open orders per exchange |
| `position_snapshots` | Last known state of open positions per exchange |
| `sent_notifications` | IDs of notifications already sent (TTL: `NOTIFICATION_DEDUP_TTL_DAYS`) |
| `disappeared_pending` | Orders that vanished from open orders but haven't appeared in history yet |
| `history_cursors` | Cursor/offset bookmarks for history endpoints |

On first run the snapshots are populated silently — no notifications are sent for pre-existing orders and positions. Subsequent runs diff the new snapshot against the stored one and emit only new events.

Notification IDs follow the pattern `{event_type}:{exchange}:{id}`. Before sending, the notifier checks whether the ID is already in `sent_notifications`. After a successful send it records the ID. This prevents duplicate messages after a crash or restart.

## Closed position notifications: profit / loss / breakeven

When a position closes, `realised_pnl` from the Extended positions history is used as the authoritative value.

Classification:

| Condition | Emoji | Label |
|---|---|---|
| `realised_pnl > 0` | 🟢 | PROFIT |
| `realised_pnl < 0` | 🔴 | LOSS |
| `realised_pnl == 0` | ⚪ | BREAKEVEN |

**PnL %** is an approximation:

```
pnl_pct = (realised_pnl / (entry_price × size)) × 100
```

This does not include leverage, fees, or funding. The result is labelled `(approx.)` in the message.

Example profit message:

```
🟢 POSITION CLOSED — PROFIT
Exchange: Extended
Market: BTC-USD
Side: LONG
Size: 0.25
Entry: 63250.50
Exit: 63880.00
PnL: +157.38 USDC
PnL %: +0.99% (approx.)
Duration: 01h 42m
Closed at: 2026-05-07 13:42:11 UTC
```

## Disappeared order handling

If an order vanishes from `get_open_orders()` and is not yet in `get_orders_history()` (a race condition common with fast fills), checkDEX queues it as `disappeared_pending` and retries for 2 poll cycles. If the order appears in history within those retries, the correct event (FILLED or CANCELLED) is emitted. If it never appears, an `ORDER UPDATED` with status `DISAPPEARED_UNKNOWN` is sent so you are aware.

## Known limitations and assumptions

- **Polling only** — no WebSocket layer. Minimum detection latency equals the poll interval (default 60 s).
- **PnL % is approximate** — does not include leverage, fees, or funding rate.
- **Extended SDK cursor pagination** — the history endpoint returns the most recent 50 records. Very high-frequency trading (>50 events per poll interval) could cause missed events.
- **Single exchange** — only Extended Exchange is implemented. The adapter interface is ready for Lighter and Hyperliquid.
- **No POSITION_UPDATED on unrealized PnL** — only size changes trigger `POSITION UPDATED` notifications, preventing spam from mark price fluctuations.

## Adding Lighter or Hyperliquid

1. Create `app/exchanges/lighter.py` (or `hyperliquid.py`) implementing `ExchangeAdapter`.
2. Translate the exchange's native order/position/trade objects into the internal models (`Order`, `Position`, `Trade`).
3. Instantiate the new adapter in `app/main.py` alongside (or instead of) `ExtendedAdapter`.

No changes to `EventEngine`, `Monitor`, `TelegramNotifier`, or `Database` are needed.

## Healthcheck

The Docker healthcheck verifies that `/tmp/healthy` was touched within the last 60 seconds. The monitor writes this file after every successful poll cycle in any of the three loops. If all loops stall (e.g., API outage lasting > 60 s), the container is marked unhealthy.

## Auto-start after server reboot

The `docker-compose.yml` uses `restart: unless-stopped`. For the container to start automatically after a server reboot, the Docker daemon itself must be enabled as a systemd service:

```bash
sudo systemctl enable docker
```

Run this once on the server. After that, any container that was running when the server shut down will be restarted automatically by Docker on boot.

To verify Docker is enabled:

```bash
sudo systemctl is-enabled docker
```

**Restart policy reference:**

| Policy | Behaviour after reboot |
|---|---|
| `no` | does not start |
| `always` | always starts, even after `docker stop` |
| `unless-stopped` | starts unless manually stopped |
| `on-failure` | starts only after non-zero exit |

`unless-stopped` is the correct choice here — if you manually stop the container (`docker compose stop`), it will not restart automatically on the next reboot.

## Verifying the system started correctly

**Quick status check:**

```bash
docker compose ps
```

The `STATUS` column should show `healthy` within ~45 seconds of startup. While the container is initialising it shows `starting`; if all polling loops fail for over 60 seconds it shows `unhealthy`.

**Read the logs:**

```bash
docker compose logs --tail=50 app
```

A clean startup looks like this:

```
{"message": "checkDEX starting", "exchange": "Extended", "network": "mainnet"}
{"message": "Database connected", "path": "/data/state.db"}
{"message": "Exchange connected", "exchange": "Extended"}
{"message": "Telegram notifier ready"}
{"message": "Startup notification sent"}
{"message": "Orders loop started", "interval": 60}
{"message": "Positions loop started", "interval": 60}
{"message": "History loop started", "interval": 60}
```

**Telegram startup notification:**

If `ENABLE_STARTUP_NOTIFICATION=true` (default), a message is sent to your Telegram chat every time the container starts. The absence of this message is a reliable signal that something went wrong.

**Healthcheck file directly:**

```bash
docker compose exec app python -c "import os,time; f='/tmp/healthy'; print('OK' if os.path.exists(f) and time.time()-os.path.getmtime(f)<60 else 'FAIL')"
```

## Troubleshooting

### Container stuck in `Restarting` loop (exit code 1)

The container exits immediately and Docker keeps restarting it. Check the logs:

```bash
docker compose logs --tail=50 app
```

**Common cause: `sqlite3.OperationalError: unable to open database file`**

The `/data` directory inside the container is mounted from `./data` on the host. If Docker created `./data` automatically it is owned by `root`, but the app runs as `appuser` (uid 1000) and cannot write to it.

Fix on the server:

```bash
cd /path/to/checkDEX
sudo chown -R 1000:1000 data
docker compose up -d
```

### Container shows `unhealthy`

The container is running but the healthcheck fails. This means the polling loops are not completing successfully.

1. Check logs for errors: `docker compose logs --tail=50 app`
2. Look for `Error in orders loop`, `Error in positions loop`, or `Error in history loop`.
3. Common causes: API authentication failure, network timeout, or Telegram error (see below).

### Telegram `400 Bad Request`

The app crashes at startup with:

```
aiohttp.client_exceptions.ClientResponseError: 400, message='Bad Request'
```

This always means `TELEGRAM_CHAT_ID` in `.env` is wrong or the bot has not been added to the target chat.

**How to find the correct chat ID:**

1. Send any message to your bot (or add it to a group and send a message there).
2. Open in a browser — replace `<TOKEN>` with your bot token:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Find `"chat": {"id": ...}` in the JSON response.
4. Copy the value exactly into `.env` as `TELEGRAM_CHAT_ID`.

Private chats have a positive integer ID (`123456789`). Groups and channels have a negative ID (`-1001234567890`).

After updating `.env`, restart the container:

```bash
docker compose up -d
```

### Bot token exposed in logs

If the bot token appears in Docker logs (e.g. in a `400 Bad Request` URL), **revoke it immediately**:

1. Open Telegram → [@BotFather](https://t.me/BotFather) → `/mybots` → select your bot → *API Token* → *Revoke current token*.
2. Copy the new token into `.env`.
3. Restart the container.

A revoked token stops working instantly. Any process or person who saw the old token in logs can no longer use it.
