# checkDEX

Read-only trading monitor for decentralised exchanges. Watches your accounts on Extended Exchange and Hyperliquid, detects order and position events, and sends formatted Telegram notifications.

**Setup:** [Installation on a new server](#installation-on-a-new-server) · [Updating to the latest version](#updating-to-the-latest-version) · [Troubleshooting](#troubleshooting)

## What it does

- Monitors open orders, partial fills, full fills, cancels, and rejects.
- Monitors open positions and size changes.
- Detects closed positions with realised PnL (profit / loss / breakeven).
- Sends all events as HTML-formatted messages to a Telegram chat.
- Sends a daily summary of open positions (see [Daily position summary](#daily-position-summary)).
- Answers the `/positions` command in the Telegram chat with the current open positions (see [Telegram commands](#telegram-commands)).
- Deduplicates notifications so the same event is never sent twice, even after a restart.
- Monitors Extended and Hyperliquid, one or both at once (`ACTIVE_EXCHANGES`). More exchanges (e.g. Lighter) can be added as adapters (see [Adding another exchange](#adding-another-exchange)).

## Architecture

```
app/
  config.py             — pydantic-settings configuration from env vars
  main.py               — entry point; wires all components and handles SIGTERM/SIGINT
  exceptions.py         — checkDEX exception types
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
    monitor.py          — polling loops and the daily summary of one exchange
    commands.py         — Telegram chat commands (/positions) via long polling
  notifiers/
    telegram.py         — Telegram Bot API notifier with dedup and retry
  storage/
    database.py         — aiosqlite persistence layer
  utils/
    pnl.py              — PnL calculation and formatting
    retry.py            — exponential backoff retry helper
    logging.py          — structured JSON logging setup
tests/                  — pytest test suite (239 tests, no network required)
```

## Extended Exchange integration

Authentication uses the official [x10-python-trading](https://github.com/x10xchange/python_sdk) SDK. Generating API keys in the Extended Exchange web UI gives you the values below. The first four are required when `extended` is in `ACTIVE_EXCHANGES`:

| Variable | Description |
|---|---|
| `EXTENDED_API_KEY` | API key — sent as `X-Api-Key` header |
| `EXTENDED_PUBLIC_KEY` | Stark public key (0x…) |
| `EXTENDED_PRIVATE_KEY` | Stark private key (0x…) |
| `EXTENDED_VAULT` | Vault Number |
| `EXTENDED_CLIENT_ID` | Optional. Client ID — sent as `X-Client-Id` header; if empty, `EXTENDED_VAULT` is sent instead |

`EXTENDED_PRIVATE_KEY` and `EXTENDED_PUBLIC_KEY` are passed to the SDK initialiser but never used for signing — this is a read-only system. At startup only the format of the values is checked, no request is sent (see [When an exchange cannot connect](#when-an-exchange-cannot-connect)).

**API endpoint note:** The SDK ships with an outdated base URL (`api.extended.exchange`). checkDEX automatically patches it to the current endpoint `api.starknet.extended.exchange` at startup — no manual change is needed.

**Timeouts and retries:** every Extended request has a 15-second timeout (the SDK default is 500 s), so a stuck request cannot block the monitor. Account data requests that time out or hit a network error are retried up to 3 times (after 1, 2 and 4 s). If all attempts fail, the error is logged and the next poll tries again.

### Polling mode

All data is fetched via periodic polling — there is no WebSocket layer. Each monitored exchange runs three independent asyncio loops (plus the [daily summary](#daily-position-summary)):

| Loop | Data source (Extended) | Default interval |
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
- The three loops read the open orders, the account state (positions) and the account fills of the last 24 hours (filled orders and closed positions). Markets are shown as `<COIN>-USDC`, e.g. `BTC-USDC`.
- Hyperliquid reports closed positions per fill and has no history of cancelled orders — see [Known limitations](#known-limitations-and-assumptions).
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

## Daily position summary

Once a day at `DAILY_SUMMARY_TIME` (UTC, default `08:00`), checkDEX sends one message per monitored exchange with its open positions:

```
📊 DAILY POSITION SUMMARY
Exchange: Extended (mainnet)
2026-05-07 08:00:00 UTC

BTC-USD LONG 10x
  Size: 0.25 | Entry: 63250.5
  Last: 63512.25 | uPnL: +65.44 USDC

─────────────────────
Total uPnL: +65.44 USDC
Open positions: 1
```

- The positions come from the latest positions poll. Without open positions the message says `No open positions.`
- The summary is sent at most once per exchange and UTC day, also across restarts (its ID `daily_summary:<exchange>:<date>` is stored in `sent_notifications`).
- If the container is not running at `DAILY_SUMMARY_TIME`, that day's summary is skipped, not sent later.
- Disable it with `ENABLE_DAILY_SUMMARY=false`.

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

All settings are environment variables, read from `.env`. `.env.example` lists every variable with comments — copy it to `.env` and fill in your values (see [Installation on a new server](#installation-on-a-new-server)):

```bash
cp .env.example .env
nano .env
```

| Variable | Default | Description |
|---|---|---|
| `ACTIVE_EXCHANGES` | `extended` | Exchanges to monitor, comma-separated (`extended,hyperliquid`); a JSON list (`["extended","hyperliquid"]`) works too |
| `EXTENDED_API_KEY` | — | Required with `extended` |
| `EXTENDED_PUBLIC_KEY` | — | Required with `extended` (Stark public key, 0x…) |
| `EXTENDED_PRIVATE_KEY` | — | Required with `extended` (Stark private key, 0x…; never used for signing) |
| `EXTENDED_VAULT` | — | Required with `extended` (Vault Number) |
| `EXTENDED_CLIENT_ID` | — | Optional (Client ID); falls back to `EXTENDED_VAULT` |
| `EXTENDED_NETWORK` | `mainnet` | `mainnet` or `testnet` |
| `HYPERLIQUID_WALLET_ADDRESS` | — | Required with `hyperliquid`: main account address, not an API wallet (see [Hyperliquid](#hyperliquid-main-account-not-an-api-wallet)) |
| `HYPERLIQUID_TESTNET` | `false` | `true` for the Hyperliquid testnet |
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `ENABLE_TELEGRAM_COMMANDS` | `true` | Answer `/positions` in `TELEGRAM_CHAT_ID` (see [Telegram commands](#telegram-commands)) |
| `POLL_INTERVAL_ORDERS_SECONDS` | `60` | Orders polling interval (at least 1) |
| `POLL_INTERVAL_POSITIONS_SECONDS` | `60` | Positions polling interval (at least 1) |
| `POLL_INTERVAL_HISTORY_SECONDS` | `60` | History polling interval (at least 1) |
| `STATE_DB_PATH` | `/data/state.db` | SQLite file inside the container; keep it in `/data`, which is `./data` on the host |
| `NOTIFICATION_DEDUP_TTL_DAYS` | `30` | Meant to set how long sent-notification IDs are kept. **Currently not applied** — they are always kept 30 days |
| `ENABLE_ORDER_OPENED` | `true` | `ORDER OPENED` messages |
| `ENABLE_ORDER_UPDATED` | `true` | `ORDER UPDATED` messages (partial fill, cancel, reject, disappeared order) |
| `ENABLE_ORDER_FILLED` | `true` | `ORDER FILLED` messages |
| `ENABLE_POSITION_OPENED` | `true` | `POSITION OPENED` messages |
| `ENABLE_POSITION_UPDATED` | `true` | `POSITION UPDATED` messages (size change) |
| `ENABLE_POSITION_CLOSED` | `true` | `POSITION CLOSED` messages |
| `ENABLE_STARTUP_NOTIFICATION` | `true` | `checkDEX started` message each time an exchange connects |
| `ENABLE_DAILY_SUMMARY` | `true` | Daily position summary (see [Daily position summary](#daily-position-summary)) |
| `DAILY_SUMMARY_TIME` | `08:00` | Time of the daily summary, `HH:MM` in UTC |
| `UNREALIZED_PNL_THRESHOLD_USDC` | empty | **Not implemented yet** — has no effect; no unrealized PnL alerts are sent |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `json` | `json` (default) or `text` |

- Values of an exchange that is not in `ACTIVE_EXCHANGES` are ignored.
- A changed `.env` takes effect after `docker compose up -d --force-recreate` (`docker compose restart` keeps the old values).
- A missing required value or an invalid one (unknown exchange, `DAILY_SUMMARY_TIME` not in `HH:MM`) stops the app at startup with an error in the log, and Docker keeps restarting it until `.env` is fixed (see [Troubleshooting](#container-stuck-in-restarting-loop-exit-code-1)).

## Installation on a new server

checkDEX runs in Docker on any Linux server. The server needs only outgoing HTTPS access (exchange APIs and Telegram); no port has to be opened.

**Before you start**, have ready:

- the Telegram bot token and chat ID (see [Setting up the Telegram bot](#setting-up-the-telegram-bot)),
- for Extended: the API values from the Extended web UI (see [Extended Exchange integration](#extended-exchange-integration)),
- for Hyperliquid: the main account address (see [Hyperliquid](#hyperliquid-main-account-not-an-api-wallet)).

The commands below assume your user may run `docker` (member of the `docker` group); otherwise prefix the `docker` commands with `sudo`.

### 1. Install Docker

Install Docker Engine with the Compose plugin by the official guide for your distribution: https://docs.docker.com/engine/install/. Then make Docker start on boot and check that both commands work:

```bash
sudo systemctl enable --now docker
docker --version
docker compose version
```

### 2. Get the code

```bash
git clone https://github.com/dolcaai53/checkDEX.git
cd checkDEX
```

If `git` is missing, install it (e.g. `sudo apt install git` on Debian/Ubuntu). Or copy the project directory to the server instead — without `.env`, `data/` and `.git/`.

### 3. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set at least `ACTIVE_EXCHANGES`, the values of the selected exchanges, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see [Configuration](#configuration)). `chmod 600` makes the file with your keys readable only by you.

### 4. Prepare the data directory

The database is stored in `./data` on the host. The app runs as uid 1000 inside the container and must be able to write there:

```bash
mkdir -p data
sudo chown -R 1000:1000 data
```

### 5. Run the tests (optional)

```bash
cp .env.example .env.test
docker compose run --rm --build test
```

The `test` service reads `.env.test`; an unchanged copy of `.env.example` is enough, the tests call no exchange or Telegram API. The summary on the last line must show no `failed` or `error`. `.env.test` is needed only for the tests, the app itself does not read it. See [Running tests](#running-tests).

### 6. Start

```bash
docker compose up -d --build
```

The first build downloads and installs the Python libraries and takes a few minutes.

### 7. Check

```bash
docker compose ps
docker compose logs --tail=50 app
```

- `STATUS` shows `healthy` within about a minute.
- Telegram receives `🟡 checkDEX started` for each monitored exchange.
- `/positions` in the chat (in a group `/positions@YourBotName`) gets a reply with the open positions.

A clean log is shown in [Verifying the system started correctly](#verifying-the-system-started-correctly). If something fails, see [Troubleshooting](#troubleshooting).

From now on the container starts by itself after a crash or a server reboot (`restart: unless-stopped` and Docker enabled in step 1).

## Updating to the latest version

For a server where checkDEX already runs. All state is kept in `./data`, so after the update checkDEX continues where it stopped: nothing already sent is sent again, and positions closed during the short downtime are reported after the start.

### With git (installed by `git clone`)

```bash
cd /path/to/checkDEX

# Note the current version — you need it for a rollback
git log --oneline -1

# Download the latest version
git pull
```

If `git pull` refuses because of local changes, look at them with `git status` and `git diff`. Settings belong in `.env`, not in the project files — undo the changes you do not need and run `git pull` again. Then continue with [Finish the update](#finish-the-update).

### Without git (files copied manually)

1. Back up the current files for a rollback:

   ```bash
   cd /path/to/checkDEX
   tar czf ../checkDEX-files-$(date +%F).tgz --exclude=./data --exclude=./.env .
   ```

2. Copy all files of the new version into the project directory, except `.env` and `data/`. Do not skip `requirements.txt`: library versions are pinned there, and an image built with an old one misses libraries (e.g. `hyperliquid-python-sdk is not installed`).

Then continue with [Finish the update](#finish-the-update).

### Finish the update

```bash
# 1. Show variables that are new in .env.example and missing in your .env
comm -23 <(grep -oE '^[A-Z_]+' .env.example | sort) <(grep -oE '^[A-Z_]+' .env | sort)

# 2. Build the new image — the old version keeps monitoring meanwhile
docker compose build --pull

# 3. Stop the old version — it finishes the running poll and closes the database
docker compose stop

# 4. Back up the database
sudo cp -a data ../checkDEX-data-$(date +%F)

# 5. Start the new version
docker compose up -d

# 6. Check
docker compose ps
docker compose logs --tail=50 app
```

- If step 1 prints variable names, copy their lines (with the comments) from `.env.example` to `.env` and set the values before step 5. Without them the defaults apply (see [Configuration](#configuration)).
- Back up only while the container is stopped — a copy of a database in use can be inconsistent. `cp -a` keeps the owner (uid 1000), so the backup can be put back as it is.
- After the start, Telegram receives `checkDEX started` again and `STATUS` turns `healthy` within about a minute.
- Old backups (`../checkDEX-data-*`, `../checkDEX-files-*.tgz`) can be deleted once the new version runs fine.

### Rolling back

With git, go back to the version noted before `git pull`:

```bash
git checkout <version>
docker compose build
docker compose up -d
```

Without git, stop the container (`docker compose stop`), unpack the file backup in the project directory (`tar xzf ../checkDEX-files-YYYY-MM-DD.tgz`), then run `docker compose build` and `docker compose up -d`.

Keep `data/` as it is: the database format is the same in all versions so far, and older versions ignore stored fields they do not know. To return to the latest version later: `git checkout main && git pull`, then [Finish the update](#finish-the-update).

Restore the database backup only if the database is damaged (the log shows e.g. `database disk image is malformed`):

```bash
docker compose stop
sudo mv data data-damaged-$(date +%F)
sudo cp -a ../checkDEX-data-YYYY-MM-DD data
docker compose up -d
```

Events notified after the backup was made may then be sent once more.

## Everyday commands

```bash
docker compose ps                       # status: healthy / unhealthy / restarting
docker compose logs -f app              # follow the logs (Ctrl+C to quit)
docker compose logs --since 1h app      # logs of the last hour
docker compose up -d --force-recreate   # apply changes in .env
docker compose stop                     # stop; stays stopped after a server reboot
docker compose up -d                    # start again
docker compose down                     # remove the container; ./data stays
```

## Running tests

```bash
docker compose run --rm --build test
```

The `test` service reads `.env.test`, which is not in the repository. Create it once as a copy of the example — the tests need no real keys:

```bash
cp .env.example .env.test
```

No API keys or network access to exchanges or Telegram are required — the tests use temporary SQLite files, mock objects and local test servers.

## Persistence and deduplication

The SQLite database is `./data/state.db` on the host (`/data/state.db` in the container). While the app runs, SQLite also keeps `state.db-wal` and `state.db-shm` next to it — they belong to the database, so always copy the whole `data/` directory, and only while the container is stopped.

The database stores:

| Table | Purpose |
|---|---|
| `order_snapshots` | Last known state of open orders per exchange |
| `position_snapshots` | Last known state of open positions per exchange |
| `sent_notifications` | IDs of notifications already sent (kept 30 days, older ones are removed at each start) |
| `disappeared_pending` | Orders that vanished from open orders but haven't appeared in history yet |
| `history_cursors` | Cursor/offset bookmarks for history endpoints |

The first time an exchange is monitored, checkDEX saves its current state silently:

- Existing open orders and positions are stored without notifications.
- Closed positions already in the exchange history are marked as notified, so adding a new exchange does not send a burst of old `POSITION CLOSED` messages.

After that, every poll compares the new snapshot with the stored one and sends only new events — including the first order or position on an account that was empty at the first run.

The first run is tracked per exchange by markers in `history_cursors` (`orders_initialized:<exchange>`, `positions_initialized:<exchange>`, `history_initialized:<exchange>`). A database from an older version that already holds snapshots or sent close notifications counts as initialised, so an upgrade sends nothing extra.

Notification IDs follow the pattern `{event_type}:{exchange}:{id}`. Before sending, the notifier checks whether the ID is already in `sent_notifications`. After a successful send it records the ID. This prevents duplicate messages after a crash or restart.

## Closed position notifications: profit / loss / breakeven

The realised PnL comes from the exchange and is used as the authoritative value: on Extended from the positions history, on Hyperliquid from `closedPnl` of the closing fill.

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
PnL %: +1.00% (approx.)
Duration: 01h 42m
Closed at: 2026-05-07 13:42:11 UTC
```

**Hyperliquid:** every closing fill is sent as its own `POSITION CLOSED`, so a position closed in several fills, or closed partially, gives several messages. A fill contains neither the entry price nor the opening time: `Entry` shows the exit price, `Duration` shows `—`, and PnL % is computed from the exit price.

## Disappeared order handling

If an order vanishes from `get_open_orders()` and is not yet in `get_orders_history()` (a race condition common with fast fills), checkDEX queues it as `disappeared_pending` and retries for 2 poll cycles. If the order appears in history within those retries, the correct event (FILLED or CANCELLED) is emitted. If it never appears, an `ORDER UPDATED` with status `DISAPPEARED_UNKNOWN` is sent so you are aware.

On Hyperliquid the order history is built from fills only, so a cancelled or rejected order always ends as `DISAPPEARED_UNKNOWN`.

## Known limitations and assumptions

- **Polling only** — no WebSocket layer. Minimum detection latency equals the poll interval (default 60 s).
- **PnL % is approximate** — does not include leverage, fees, or funding rate.
- **Extended SDK cursor pagination** — the history endpoint returns the most recent 50 records. Very high-frequency trading (>50 events per poll interval) could cause missed events.
- **Hyperliquid closes per fill** — one `POSITION CLOSED` per closing fill, without entry price and duration (see [Closed position notifications](#closed-position-notifications-profit--loss--breakeven)).
- **Hyperliquid cancels** — cancelled and rejected orders are reported as `DISAPPEARED_UNKNOWN` (see [Disappeared order handling](#disappeared-order-handling)).
- **Downtime** — while checkDEX is stopped nothing is monitored. After the start, position changes and closes from that time are reported (as far as the history reaches: 50 records on Extended, 24 hours on Hyperliquid); orders placed and filled or cancelled in the meantime are not.
- **Two exchanges** — Extended and Hyperliquid are implemented, Lighter is not yet (see [Adding another exchange](#adding-another-exchange)).
- **No POSITION_UPDATED on unrealized PnL** — only size changes trigger `POSITION UPDATED` notifications, preventing spam from mark price fluctuations.
- **No unrealized PnL alert** — `UNREALIZED_PNL_THRESHOLD_USDC` is not implemented yet and has no effect.
- **`NOTIFICATION_DEDUP_TTL_DAYS` is not applied** — sent-notification IDs are always kept 30 days; old ones are removed only at startup.

## Adding another exchange

Lighter or any other exchange is added as a new adapter, the same way as Hyperliquid:

1. Create `app/exchanges/lighter.py` with a class implementing `ExchangeAdapter` (`app/exchanges/base.py`). `extended.py` and `hyperliquid.py` are working examples.
2. Translate the exchange's native order/position/trade objects into the internal models (`Order`, `Position`, `Trade`).
3. In `app/config.py`, add the exchange ID (e.g. `lighter`) to the valid `ACTIVE_EXCHANGES` values, add its settings and check the required ones. Describe the new variables in `.env.example`.
4. In `app/main.py`, return the new adapter from `_create_exchange_adapter` for that ID.

No changes to `EventEngine`, `Monitor`, `TelegramNotifier`, or `Database` are needed.

## Healthcheck

The Docker healthcheck verifies that `/tmp/healthy` was touched within the last 60 seconds. The monitor writes this file after every successful poll cycle of any loop of any exchange (and after a sent daily summary). If no poll completes for over 60 s (e.g. an API outage, or no exchange connected), the container is marked `unhealthy`. Docker only marks it — the container is not restarted, and checkDEX keeps retrying.

## When an exchange cannot connect

Each exchange runs on its own. If one cannot connect at startup (invalid values in `.env`, API outage, API wallet address, …), the other exchanges are monitored as usual:

1. The log shows `Exchange connection failed` with the reason in `error`.
2. Telegram gets one ⚠️ **checkDEX — exchange connection failed** message with the same reason. If Telegram is unreachable at that moment, the message is sent at the next failed attempt.
3. checkDEX tries to connect again after 30 s, then doubles the wait up to every 10 minutes. A temporary outage needs no action.
4. Once connected, the usual startup message is sent (if `ENABLE_STARTUP_NOTIFICATION=true`) and monitoring starts.

A configuration error does not fix itself: correct `.env` and run `docker compose up -d --force-recreate`.

What the connection step checks:

- **Extended** — only the format of the values (`EXTENDED_VAULT` must be a number, the Stark keys hex values `0x…`); no request is sent. A wrong but well-formed API key connects, and the error appears in the polling loops instead: the log shows `Error in orders loop` (and the other loops) with an authorization error, e.g. HTTP 401.
- **Hyperliquid** — sends requests (market data, the wallet role check), so an API outage shows up here.

If no exchange is connected, no poll completes and the container shows `unhealthy`, but it keeps trying. An unexpected internal error stops the process instead of leaving it hanging, and Docker starts it again (`restart: unless-stopped`).

## Auto-start after server reboot

The `docker-compose.yml` uses `restart: unless-stopped`. For the container to start automatically after a server reboot, the Docker daemon itself must be enabled as a systemd service:

```bash
sudo systemctl enable docker
```

Run this once on the server (the [installation](#1-install-docker) does it). After that, any container that was running when the server shut down will be restarted automatically by Docker on boot.

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

The `STATUS` column should show `healthy` within about a minute of startup. While the container is initialising it shows `starting`; if no poll completes for over 60 seconds it shows `unhealthy`.

**Read the logs:**

```bash
docker compose logs --tail=50 app
```

A clean startup with Extended looks like this:

```
{"message": "checkDEX starting", "exchanges": ["extended"]}
{"message": "Database connected", "path": "/data/state.db", "dedup_cleaned": 0}
{"message": "Telegram notifier ready"}
{"message": "Telegram command listener started", "commands": ["/positions"]}
{"message": "Extended adapter initialised", "network": "mainnet"}
{"message": "Exchange connected", "exchange": "Extended"}
{"message": "Startup notification sent"}
{"message": "Orders loop started", "interval": 60}
{"message": "Positions loop started", "interval": 60}
{"message": "History loop started", "interval": 60}
{"message": "Daily summary loop started", "scheduled_utc": "08:00"}
```

The lines are shortened: each also contains `timestamp` (UTC), `name` and `level`, and some are logged twice. With Hyperliquid you also see `Hyperliquid wallet role` (`"role": "user"` for a main account) and `Hyperliquid adapter initialised`. With several exchanges their lines are interleaved.

A clean stop (`docker compose stop`) ends with `Shutdown signal received` … `Database disconnected` → `checkDEX stopped cleanly`.

**Telegram startup notification:**

If `ENABLE_STARTUP_NOTIFICATION=true` (default), a message is sent to your Telegram chat every time an exchange connects after a start. The absence of this message is a reliable signal that something went wrong.

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

**Cause 1: `sqlite3.OperationalError: unable to open database file`**

The `/data` directory inside the container is mounted from `./data` on the host. If Docker created `./data` automatically it is owned by `root`, but the app runs as `appuser` (uid 1000) and cannot write to it.

Fix on the server:

```bash
cd /path/to/checkDEX
sudo chown -R 1000:1000 data
docker compose up -d
```

**Cause 2: `ValidationError` for `Config`**

A value in `.env` is missing or invalid, e.g. `Field required` for `telegram_bot_token`, `Missing required Extended config: …`, `Unknown exchange … in ACTIVE_EXCHANGES` or `DAILY_SUMMARY_TIME must be HH:MM (UTC)`. Fix `.env`, then run `docker compose up -d --force-recreate`.

### Container shows `unhealthy`

The container is running but the healthcheck fails: no poll of any exchange completed in the last 60 seconds.

1. Check logs for errors: `docker compose logs --tail=50 app`
2. Look for `Error in orders loop`, `Error in positions loop`, or `Error in history loop`.
3. Look for `Exchange connection failed` — no exchange could connect (see [the next section](#exchange-connection-failed-in-telegram)).
4. Common causes: a wrong Extended API key (authorization error, e.g. HTTP 401, in the loop errors), a network or API outage, or a Telegram error when sending a notification (see below).

### `exchange connection failed` in Telegram

One exchange could not connect; the others keep running and checkDEX keeps retrying (see [When an exchange cannot connect](#when-an-exchange-cannot-connect)). The `Error:` line says why.

| Error contains | Fix |
|---|---|
| `is an API (agent) wallet` | Set `HYPERLIQUID_WALLET_ADDRESS` to the main account address at the end of the message, then `docker compose up -d --force-recreate` |
| `invalid literal for int()` | `EXTENDED_VAULT` in `.env` is not a number. Fix it, then `docker compose up -d --force-recreate` |
| `AssertionError` | `EXTENDED_PUBLIC_KEY` or `EXTENDED_PRIVATE_KEY` in `.env` is not a hex value (`0x…`). Fix it, then `docker compose up -d --force-recreate` |
| `hyperliquid-python-sdk is not installed` | The image was built with an old `requirements.txt`. Copy the current one to the server, then `docker compose build --no-cache` and `docker compose up -d` |
| `Name or service not known`, `Temporary failure in name resolution`, timeouts | Network or API outage — no action, checkDEX reconnects automatically |

### Telegram `400 Bad Request`

The log shows `Startup notification failed` — and `Error in … loop` or `Error sending daily summary` whenever a message should be sent — with `400, message='Bad Request'` in the error. Monitoring keeps running, but no message reaches the chat.

This usually means `TELEGRAM_CHAT_ID` in `.env` is wrong or the bot has not been added to the target chat. (`401 Unauthorized` or `404 Not Found` instead means `TELEGRAM_BOT_TOKEN` is wrong.)

**How to find the correct chat ID:**

1. Send any message to your bot (or add it to a group and send a message there).
2. Open in a browser — replace `<TOKEN>` with your bot token:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Find `"chat": {"id": ...}` in the JSON response.
4. Copy the value exactly into `.env` as `TELEGRAM_CHAT_ID`.

Private chats have a positive integer ID (`123456789`). Groups and channels have a negative ID (`-1001234567890`). While checkDEX runs, the `getUpdates` page may be empty — see [Setting up the Telegram bot](#setting-up-the-telegram-bot).

After updating `.env`, recreate the container:

```bash
docker compose up -d --force-recreate
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

Errors of the startup message, of the `exchange connection failed` message and of `/positions` are logged with the token masked as `***`. A failed event notification or daily summary (`Error in … loop`, `Error sending daily summary`), however, logs the full error including the request URL with the token. If the token appears in the logs, or you shared such logs, **revoke it immediately**:

1. Open Telegram → [@BotFather](https://t.me/BotFather) → `/mybots` → select your bot → *API Token* → *Revoke current token*.
2. Copy the new token into `.env`.
3. Recreate the container: `docker compose up -d --force-recreate`.

A revoked token stops working instantly. Any process or person who saw the old token in logs can no longer use it.
