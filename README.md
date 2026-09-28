# checkDEX

Read-only trading monitor for decentralised exchanges. Watches your accounts on Extended Exchange and Hyperliquid, detects order and position events, and sends formatted Telegram notifications.

## What it does

- Monitors open orders, partial fills, full fills, cancels, and rejects.
- Monitors open positions and size changes.
- Detects closed positions with realised PnL (profit / loss / breakeven).
- Sends all events as HTML-formatted messages to a Telegram chat.
- Answers the `/positions` command in the Telegram chat with the current open positions (see [Telegram commands](#telegram-commands)).
- Deduplicates notifications so the same event is never sent twice, even after a restart.
- Monitors Extended and Hyperliquid, one or both at once (`ACTIVE_EXCHANGES`). More exchanges (e.g. Lighter) can be added as adapters (see [Adding another exchange](#adding-another-exchange)).

## Architecture

```
app/
  config.py             — pydantic-settings configuration from env vars
  main.py               — entry point; wires all components and handles SIGTERM/SIGINT
  exchanges/
    base.py             — ExchangeAdapter ABC (multi-exchange interface)
    extended.py         — Extended Exchange implementation
    hyperliquid.py      — Hyperliquid implementation (wallet address only, no keys)
  models/
    order.py            — Order model and enums
    position.py         — Position model and enums
    trade.py            — Trade model (closed positions)
    events.py           — OrderOpenedEvent, OrderFilledEvent, PositionClosedEvent, …
  services/
    event_engine.py     — pure diff functions + EventEngine orchestrator
    monitor.py          — three independent polling loops
    commands.py         — Telegram chat commands (/positions) via long polling
  notifiers/
    telegram.py         — Telegram Bot API notifier with dedup and retry
  storage/
    database.py         — aiosqlite persistence layer
  utils/
    pnl.py              — PnL calculation and formatting
    retry.py            — exponential backoff retry helper
    logging.py          — structured JSON logging setup
tests/                  — pytest test suite (177 tests, no network required)
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

**Timeouts and retries:** every Extended request has a 15-second timeout (the SDK default is 500 s), so a stuck request cannot block the monitor. Account data requests that time out or hit a network error are retried up to 3 times (after 1, 2 and 4 s). If all attempts fail, the error is logged and the next poll tries again.

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

## Hyperliquid: main account, not an API wallet

Hyperliquid needs only the public wallet address in `HYPERLIQUID_WALLET_ADDRESS` — no API key, no private key.

Use the address of your **main account** (shown top right on hyperliquid.xyz). An API (agent) wallet only signs trades for the main account and has no positions or orders of its own, so with its address checkDEX would see an empty account.

checkDEX checks this at startup: it asks Hyperliquid for the role of the address. If it is an API wallet, Hyperliquid is not monitored and the error, including the main account address, goes to the log and to Telegram (see [When an exchange cannot connect](#when-an-exchange-cannot-connect)):

```
HYPERLIQUID_WALLET_ADDRESS 0x… is an API (agent) wallet without positions or orders of its own. Set HYPERLIQUID_WALLET_ADDRESS to the main account address: 0x…
```

Put that address into `.env` and run `docker compose up -d --force-recreate`.

- If the role check itself fails (e.g. a timeout), a warning is logged and monitoring continues.
- An address with no account activity on Hyperliquid is only logged as a warning — check it for typos.
- Every Hyperliquid request has a 15-second timeout (the SDK sets none by default), so a slow API cannot block the monitor.
- Hyperliquid sends neither the last traded price nor the mark price with positions. When positions are open, checkDEX fetches both with public requests on each positions poll (see [Prices in messages](#prices-in-messages)).

## Setting up the Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the bot token.
2. Add the bot to your target group (or start a private chat with it).
3. Get the chat ID: send a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id":…}`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your `.env`.

While checkDEX is running it reads the bot's updates itself (see [Telegram commands](#telegram-commands)), so the `getUpdates` page in step 3 may show an empty list. Stop the container first (`docker compose stop`), send a new message to the chat, then open the page.

## Telegram commands

Besides sending notifications, the bot answers commands sent in the chat:

| Command | Reply |
|---|---|
| `/positions` | Current open positions on all monitored exchanges — market, side, leverage, size, entry, current price (see [Prices in messages](#prices-in-messages)), unrealized PnL and total unrealized PnL |

How it works:

- checkDEX asks Telegram for new messages with long polling (`getUpdates`). No webhook and no open port are needed.
- Positions are fetched fresh from the exchange API when the command arrives, not from the local snapshot. If an exchange cannot be reached, its section says `Data unavailable` and the other exchanges are still listed.
- Only messages from `TELEGRAM_CHAT_ID` are answered; any other chat is ignored. In a group, every member of the group can use the command.
- In a group, send `/positions@YourBotName`. A plain `/positions` reaches the bot only if it was the last bot to post in the group (Telegram privacy mode, on by default).
- Commands older than 5 minutes (e.g. sent while the container was down) are ignored, so a restart does not trigger a burst of replies. The ID of the last processed message is stored in the database (`history_cursors`), so no command is answered twice.
- Several `/positions` received at once get a single reply.
- The command is read-only — it never changes anything on the exchange.
- Disable it with `ENABLE_TELEGRAM_COMMANDS=false`.

**One reader per bot token:** Telegram hands out updates to only one reader at a time. If another program reads updates of the same bot token, or a webhook is set for it, Telegram answers with `409 Conflict` and `/positions` gets no reply (notifications keep working). Give the other program its own bot, or set `ENABLE_TELEGRAM_COMMANDS=false`.

## Prices in messages

The daily summary, `/positions` and `POSITION UPDATED` show the current price of a position on one line:

```
BTC-USD LONG 10x
  Size: 0.25 | Entry: 63250.5
  Last: 63512.25 | uPnL: +65.44 USDC
```

- `Last` is the last traded price. If the exchange does not return it, the same line shows `Mark` (mark price) instead, or `Mark: —` when neither is available.
- Prices are fetched only when positions are open, on each positions poll. If the request fails, a warning is logged, positions are still monitored and the message shows `Mark`.

| Exchange | Last traded price | Mark price |
|---|---|---|
| Extended | `marketStats.lastPrice` from the public `GET /info/markets` — one request for all markets with an open position (15 s timeout) | sent with the position |
| Hyperliquid | close of the newest daily candle from `candleSnapshot` — one request per market with an open position | `metaAndAssetCtxs` — one request for all markets |

**Precision:** all prices in messages (entry, exit, order price, last, mark) show up to 7 significant digits without trailing zeros, e.g. `63250.5`, `0.004081`, `2450`. Cheap coins keep their decimals, and float noise from the API (Extended sends mark prices like `80079.584688000002`) is cut off (`80079.58`). The integer part is never rounded. PnL amounts stay at 2 decimals.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```
cp .env.example .env
$EDITOR .env
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `ACTIVE_EXCHANGES` | `extended` | Exchanges to monitor, comma-separated (`extended,hyperliquid`); a JSON list (`["extended","hyperliquid"]`) works too |
| `EXTENDED_API_KEY` | — | Required |
| `EXTENDED_PUBLIC_KEY` | — | Required |
| `EXTENDED_PRIVATE_KEY` | — | Required |
| `EXTENDED_VAULT` | — | Required (Vault Number) |
| `EXTENDED_CLIENT_ID` | — | Required (Client ID; falls back to EXTENDED_VAULT if unset) |
| `EXTENDED_NETWORK` | `mainnet` | `mainnet` or `testnet` |
| `HYPERLIQUID_WALLET_ADDRESS` | — | Required with `hyperliquid`: main account address, not an API wallet (see [Hyperliquid](#hyperliquid-main-account-not-an-api-wallet)) |
| `HYPERLIQUID_TESTNET` | `false` | `true` for the Hyperliquid testnet |
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `ENABLE_TELEGRAM_COMMANDS` | `true` | Answer `/positions` in `TELEGRAM_CHAT_ID` (see [Telegram commands](#telegram-commands)) |
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

If you deploy by copying files to the server, copy every changed file except `.env` and `data/` — including `requirements.txt`. Library versions are pinned there; an image built with an old `requirements.txt` misses libraries (e.g. `hyperliquid-python-sdk is not installed`). After copying, rebuild with `docker compose build --no-cache` and start with `docker compose up -d`.

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

The first time an exchange is monitored, checkDEX saves its current state silently:

- Existing open orders and positions are stored without notifications.
- Closed positions already in the exchange history are marked as notified, so adding a new exchange does not send a burst of old `POSITION CLOSED` messages.

After that, every poll compares the new snapshot with the stored one and sends only new events — including the first order or position on an account that was empty at the first run.

The first run is tracked per exchange by markers in `history_cursors` (`orders_initialized:<exchange>`, `positions_initialized:<exchange>`, `history_initialized:<exchange>`). A database from an older version that already holds snapshots or sent close notifications counts as initialised, so an upgrade sends nothing extra.

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
Entry: 63250.5
Exit: 63880
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
- **Two exchanges** — Extended and Hyperliquid are implemented, Lighter is not yet (see [Adding another exchange](#adding-another-exchange)).
- **No POSITION_UPDATED on unrealized PnL** — only size changes trigger `POSITION UPDATED` notifications, preventing spam from mark price fluctuations.

## Adding another exchange

Lighter or any other exchange is added as a new adapter, the same way as Hyperliquid:

1. Create `app/exchanges/lighter.py` with a class implementing `ExchangeAdapter` (`app/exchanges/base.py`). `extended.py` and `hyperliquid.py` are working examples.
2. Translate the exchange's native order/position/trade objects into the internal models (`Order`, `Position`, `Trade`).
3. In `app/config.py`, add the exchange ID (e.g. `lighter`) to the valid `ACTIVE_EXCHANGES` values, add its settings and check the required ones. Describe the new variables in `.env.example`.
4. In `app/main.py`, return the new adapter from `_create_exchange_adapter` for that ID.

No changes to `EventEngine`, `Monitor`, `TelegramNotifier`, or `Database` are needed.

## Healthcheck

The Docker healthcheck verifies that `/tmp/healthy` was touched within the last 60 seconds. The monitor writes this file after every successful poll cycle in any of the three loops. If all loops stall (e.g., API outage lasting > 60 s), the container is marked unhealthy.

## When an exchange cannot connect

Each exchange runs on its own. If one cannot connect at startup (wrong keys, API outage, API wallet address, …), the other exchanges are monitored as usual:

1. The log shows `Exchange connection failed` with the reason in `error`.
2. Telegram gets one ⚠️ **checkDEX — exchange connection failed** message with the same reason. If Telegram is unreachable at that moment, the message is sent at the next failed attempt.
3. checkDEX tries to connect again after 30 s, then doubles the wait up to every 10 minutes. A temporary outage needs no action.
4. Once connected, the usual startup message is sent (if `ENABLE_STARTUP_NOTIFICATION=true`) and monitoring starts.

A configuration error does not fix itself: correct `.env` and run `docker compose up -d --force-recreate`.

If no exchange is connected, no poll completes and the container shows `unhealthy`, but it keeps trying. An unexpected internal error stops the process instead of leaving it hanging, and Docker starts it again (`restart: unless-stopped`).

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
3. Look for `Exchange connection failed` — no exchange could connect (see [the next section](#exchange-connection-failed-in-telegram)).
4. Common causes: API authentication failure, network timeout, or Telegram error (see below).

### `exchange connection failed` in Telegram

One exchange could not connect; the others keep running and checkDEX keeps retrying (see [When an exchange cannot connect](#when-an-exchange-cannot-connect)). The `Error:` line says why.

| Error contains | Fix |
|---|---|
| `is an API (agent) wallet` | Set `HYPERLIQUID_WALLET_ADDRESS` to the main account address at the end of the message, then `docker compose up -d --force-recreate` |
| `hyperliquid-python-sdk is not installed` | The image was built with an old `requirements.txt`. Copy the current one to the server, then `docker compose build --no-cache` and `docker compose up -d` |
| `Name or service not known`, `Temporary failure in name resolution`, timeouts | Network or API outage — no action, checkDEX reconnects automatically |

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

### `/positions` gets no reply

1. Check that `ENABLE_TELEGRAM_COMMANDS` is not set to `false`.
2. In a group, send `/positions@YourBotName` (see [Telegram commands](#telegram-commands)).
3. The command must be sent in the chat set in `TELEGRAM_CHAT_ID`; other chats are ignored.
4. Look for `Telegram command handling failed` in the logs:
   ```bash
   docker compose logs --tail=200 app | grep "Telegram command"
   ```
   A `409` error means another program reads updates of the same bot token, or a webhook is set for it. Give the other program its own bot, or disable the command.

### Bot token exposed in logs

If the bot token appears in Docker logs (e.g. in a `400 Bad Request` URL), **revoke it immediately**:

1. Open Telegram → [@BotFather](https://t.me/BotFather) → `/mybots` → select your bot → *API Token* → *Revoke current token*.
2. Copy the new token into `.env`.
3. Restart the container.

A revoked token stops working instantly. Any process or person who saw the old token in logs can no longer use it.
