"""Testy nacteni konfigurace — hlavne ACTIVE_EXCHANGES (carka i JSON).

Promenne z .env.test (EXTENDED_*, TELEGRAM_*) jsou v kontejneru nastavene,
proto staci doplnit jen to, co dany test zkousi.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Config

_ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"
_WALLET = "0x" + "a" * 40


@pytest.fixture
def hl_wallet(monkeypatch) -> None:
    # Hyperliquid v ACTIVE_EXCHANGES vyzaduje adresu penezenky.
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", _WALLET)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("extended", ["extended"]),
        ("hyperliquid", ["hyperliquid"]),
        ("extended,hyperliquid", ["extended", "hyperliquid"]),
        (" extended , hyperliquid ", ["extended", "hyperliquid"]),
        ('["extended","hyperliquid"]', ["extended", "hyperliquid"]),
        ('["hyperliquid"]', ["hyperliquid"]),
    ],
)
def test_active_exchanges_from_env(monkeypatch, hl_wallet, raw: str, expected: list[str]) -> None:
    monkeypatch.setenv("ACTIVE_EXCHANGES", raw)
    assert Config(_env_file=None).active_exchanges == expected


def test_active_exchanges_default_is_extended(monkeypatch) -> None:
    monkeypatch.delenv("ACTIVE_EXCHANGES", raising=False)
    assert Config(_env_file=None).active_exchanges == ["extended"]


@pytest.mark.parametrize(
    "line",
    [
        "ACTIVE_EXCHANGES=extended,hyperliquid",
        'ACTIVE_EXCHANGES=["extended","hyperliquid"]',
    ],
)
def test_active_exchanges_from_env_file(monkeypatch, tmp_path, hl_wallet, line: str) -> None:
    monkeypatch.delenv("ACTIVE_EXCHANGES", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")
    assert Config(_env_file=str(env_file)).active_exchanges == ["extended", "hyperliquid"]


def test_unknown_exchange_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ACTIVE_EXCHANGES", "extended,binance")
    with pytest.raises(ValidationError, match="binance"):
        Config(_env_file=None)


def test_empty_active_exchanges_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ACTIVE_EXCHANGES", "")
    with pytest.raises(ValidationError, match="must not be empty"):
        Config(_env_file=None)


def test_hyperliquid_requires_wallet_address(monkeypatch) -> None:
    monkeypatch.setenv("ACTIVE_EXCHANGES", "extended,hyperliquid")
    monkeypatch.delenv("HYPERLIQUID_WALLET_ADDRESS", raising=False)
    with pytest.raises(ValidationError, match="HYPERLIQUID_WALLET_ADDRESS"):
        Config(_env_file=None)


def _env_example_keys() -> list[str]:
    keys = []
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0])
    return keys


def test_env_example_is_loadable(monkeypatch) -> None:
    """.env.example musi jit nacist — kazdy klic odpovida poli v Config."""
    # Promenne prostredi maji prednost pred souborem; bez nich se ctou
    # opravdu hodnoty z .env.example.
    for key in _env_example_keys():
        monkeypatch.delenv(key, raising=False)
    config = Config(_env_file=str(_ENV_EXAMPLE))
    assert config.active_exchanges == ["extended"]
    assert config.telegram_chat_id == "-100your-chat-id"
