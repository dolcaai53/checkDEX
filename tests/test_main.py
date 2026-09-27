"""Testy spousteni burz v app.main — opakovani pripojeni, nezavislost burz, uklid.

Bez site: burzy, Telegram notifier i Monitor jsou nahrazene fake tridami,
DB je skutecna SQLite v tmp_path.
"""
from __future__ import annotations

import asyncio

import pytest

from app import main as main_module
from app.exceptions import ExchangeConnectionError
from app.storage.database import Database

_WALLET = "0x" + "a" * 40


class FakeExchange:
    """Burza, ktera se prvnich *failures* pokusu nepripoji."""

    def __init__(
        self, name: str = "Extended", failures: int = 0, error: Exception | None = None
    ) -> None:
        self.exchange_name = name
        self.network = "mainnet"
        self._failures = failures
        self._error = error or ExchangeConnectionError(f"{name} is down")
        self.connect_calls = 0
        self.disconnected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self._failures:
            raise self._error

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeNotifier:
    """Zaznamenava odeslane zpravy; vysledky send_exchange_error lze predepsat."""

    def __init__(
        self, error_send_results: list[bool] | None = None, startup_error: Exception | None = None
    ) -> None:
        self.errors: list[tuple[str, str, str]] = []
        self.startups: list[str] = []
        self.disconnected = False
        self._error_send_results = list(error_send_results or [])
        self._startup_error = startup_error

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_exchange_error(self, exchange: str, network: str, error: str) -> bool:
        self.errors.append((exchange, network, error))
        return self._error_send_results.pop(0) if self._error_send_results else True

    async def send_startup(self, exchange: str, network: str) -> None:
        self.startups.append(exchange)
        if self._startup_error is not None:
            raise self._startup_error

    def safe_error(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"


class FakeMonitor:
    """Misto Monitoru: bezi, dokud se nezavola stop()."""

    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange
        self.running = False
        self.stopped = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.running = True
        await self._stop.wait()
        self.running = False

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_CONNECT_RETRY_MIN_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "_CONNECT_RETRY_MAX_SECONDS", 0.04)


@pytest.fixture
def monitors(monkeypatch) -> list[FakeMonitor]:
    """Nahradi Monitor v app.main; vraci seznam vytvorenych monitoru."""
    created: list[FakeMonitor] = []

    def factory(config, exchange, db, notifier) -> FakeMonitor:
        monitor = FakeMonitor(exchange)
        created.append(monitor)
        return monitor

    monkeypatch.setattr(main_module, "Monitor", factory)
    return created


async def _wait_until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _start(exchange: FakeExchange, notifier: FakeNotifier, shutdown: asyncio.Event) -> asyncio.Task:
    return asyncio.create_task(
        main_module.run_exchange(None, exchange, None, notifier, shutdown)
    )


# ---------------------------------------------------------------------------
# run_exchange / _connect_with_retry
# ---------------------------------------------------------------------------

async def test_connect_retries_until_success_and_reports_error_once(monitors) -> None:
    exchange = FakeExchange(failures=3)
    notifier = FakeNotifier()
    shutdown = asyncio.Event()

    task = _start(exchange, notifier, shutdown)
    await _wait_until(lambda: monitors and monitors[0].running)

    assert exchange.connect_calls == 4
    assert notifier.errors == [("Extended", "mainnet", "Extended is down")]
    assert notifier.startups == ["Extended"]

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)
    assert monitors[0].stopped


async def test_retry_delay_doubles_up_to_max(monkeypatch) -> None:
    delays: list[float] = []

    async def fake_sleep(shutdown: asyncio.Event, seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(main_module, "_sleep_unless_shutdown", fake_sleep)
    monkeypatch.setattr(main_module, "_CONNECT_RETRY_MIN_SECONDS", 30.0)
    monkeypatch.setattr(main_module, "_CONNECT_RETRY_MAX_SECONDS", 100.0)

    connected = await main_module._connect_with_retry(
        FakeExchange(failures=4), FakeNotifier(), asyncio.Event()
    )

    assert connected
    assert delays == [30.0, 60.0, 100.0, 100.0]


async def test_failed_error_message_is_sent_again_on_next_failure(monitors) -> None:
    exchange = FakeExchange(failures=3)
    notifier = FakeNotifier(error_send_results=[False, True])
    shutdown = asyncio.Event()

    task = _start(exchange, notifier, shutdown)
    await _wait_until(lambda: monitors and monitors[0].running)

    # 1. zprava neodesla -> 2. odesla -> treti neuspech uz se neposila
    assert len(notifier.errors) == 2

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)


async def test_error_without_message_reports_exception_type(monitors) -> None:
    # x10 SDK hlasi neplatny klic holym AssertionError bez textu.
    exchange = FakeExchange(failures=1, error=AssertionError())
    notifier = FakeNotifier()
    shutdown = asyncio.Event()

    task = _start(exchange, notifier, shutdown)
    await _wait_until(lambda: monitors and monitors[0].running)

    assert notifier.errors[0][2] == "AssertionError"

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)


async def test_shutdown_during_retry_wait_returns_promptly(monkeypatch, monitors) -> None:
    monkeypatch.setattr(main_module, "_CONNECT_RETRY_MIN_SECONDS", 60.0)
    exchange = FakeExchange(failures=100)
    notifier = FakeNotifier()
    shutdown = asyncio.Event()

    task = _start(exchange, notifier, shutdown)
    await _wait_until(lambda: len(notifier.errors) == 1)
    shutdown.set()

    await asyncio.wait_for(task, timeout=1)  # necekat 60 s na dalsi pokus
    assert exchange.connect_calls == 1
    assert monitors == []
    assert notifier.startups == []


async def test_startup_message_failure_does_not_stop_monitoring(monitors) -> None:
    exchange = FakeExchange()
    notifier = FakeNotifier(startup_error=RuntimeError("telegram down"))
    shutdown = asyncio.Event()

    task = _start(exchange, notifier, shutdown)
    await _wait_until(lambda: monitors and monitors[0].running)

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

@pytest.fixture
def main_env(monkeypatch, tmp_path) -> list[Database]:
    """Prostredi pro main(): dve burzy, bez Telegram prikazu, DB v tmp_path.

    Vraci seznam databazi, ktere main() vytvoril (pro kontrolu uzavreni).
    """
    monkeypatch.setenv("ACTIVE_EXCHANGES", "extended,hyperliquid")
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", _WALLET)
    monkeypatch.setenv("ENABLE_TELEGRAM_COMMANDS", "false")
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(main_module, "setup_logging", lambda *args, **kwargs: None)

    databases: list[Database] = []

    class RecordingDatabase(Database):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            databases.append(self)

    monkeypatch.setattr(main_module, "Database", RecordingDatabase)
    return databases


def _use_fakes(monkeypatch, exchanges: dict[str, FakeExchange], notifier: FakeNotifier) -> None:
    monkeypatch.setattr(
        main_module, "_create_exchange_adapter", lambda config, exchange_id: exchanges[exchange_id]
    )
    monkeypatch.setattr(main_module, "TelegramNotifier", lambda config, db: notifier)


async def test_main_keeps_monitoring_when_other_exchange_fails(
    monkeypatch, main_env, monitors
) -> None:
    extended = FakeExchange("Extended")
    hyperliquid = FakeExchange("Hyperliquid", failures=10**6)
    notifier = FakeNotifier()
    _use_fakes(monkeypatch, {"extended": extended, "hyperliquid": hyperliquid}, notifier)

    shutdown = asyncio.Event()
    task = asyncio.create_task(main_module.main(shutdown))

    await _wait_until(lambda: monitors and monitors[0].running)
    await _wait_until(lambda: hyperliquid.connect_calls >= 3)  # zkousi se dal
    assert [m.exchange for m in monitors] == [extended]
    assert notifier.startups == ["Extended"]
    assert [e[0] for e in notifier.errors] == ["Hyperliquid"]

    shutdown.set()
    await asyncio.wait_for(task, timeout=2)

    assert monitors[0].stopped
    assert extended.disconnected and hyperliquid.disconnected
    assert notifier.disconnected
    assert main_env[0]._conn is None  # DB zavrena


async def test_main_exits_and_closes_db_on_unexpected_error(monkeypatch, main_env) -> None:
    """Necekana chyba ukonci proces (Docker ho restartuje) — nesmi zustat viset."""
    extended = FakeExchange("Extended")
    hyperliquid = FakeExchange("Hyperliquid")
    notifier = FakeNotifier()
    _use_fakes(monkeypatch, {"extended": extended, "hyperliquid": hyperliquid}, notifier)

    def broken_monitor(*args) -> None:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(main_module, "Monitor", broken_monitor)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await asyncio.wait_for(main_module.main(asyncio.Event()), timeout=2)

    assert extended.disconnected and hyperliquid.disconnected
    assert notifier.disconnected
    assert main_env[0]._conn is None
