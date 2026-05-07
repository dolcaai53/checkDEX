"""Unit tests for PnL and PnL % calculation."""
from __future__ import annotations

from decimal import Decimal

from app.utils.pnl import calculate_pnl_pct, fmt_pnl, fmt_pct, pnl_label


def test_pnl_pct_profit() -> None:
    # entry=63250.5, size=0.25 → notional=15812.625
    # realised_pnl=157.38 → pct = 157.38/15812.625*100 = ~0.99%
    pct = calculate_pnl_pct(Decimal("157.38"), Decimal("63250.5"), Decimal("0.25"))
    assert pct is not None
    assert pct > 0


def test_pnl_pct_loss() -> None:
    pct = calculate_pnl_pct(Decimal("-92.14"), Decimal("63250.5"), Decimal("0.25"))
    assert pct is not None
    assert pct < 0


def test_pnl_pct_breakeven() -> None:
    pct = calculate_pnl_pct(Decimal("0"), Decimal("60000"), Decimal("1"))
    assert pct == Decimal("0.00")


def test_pnl_pct_precision() -> None:
    pct = calculate_pnl_pct(Decimal("100"), Decimal("50000"), Decimal("1"))
    # 100/50000*100 = 0.20
    assert pct == Decimal("0.20")


def test_pnl_pct_zero_entry_price() -> None:
    assert calculate_pnl_pct(Decimal("100"), Decimal("0"), Decimal("1")) is None


def test_pnl_pct_zero_size() -> None:
    assert calculate_pnl_pct(Decimal("100"), Decimal("60000"), Decimal("0")) is None


def test_pnl_pct_negative_entry() -> None:
    assert calculate_pnl_pct(Decimal("100"), Decimal("-1"), Decimal("1")) is None


def test_fmt_pnl_profit() -> None:
    assert fmt_pnl(Decimal("157.38")) == "+157.38 USDC"


def test_fmt_pnl_loss() -> None:
    assert fmt_pnl(Decimal("-92.14")) == "-92.14 USDC"


def test_fmt_pnl_zero() -> None:
    assert fmt_pnl(Decimal("0")) == "0.00 USDC"


def test_pnl_label_profit() -> None:
    emoji, label = pnl_label(Decimal("100"))
    assert emoji == "🟢"
    assert label == "PROFIT"


def test_pnl_label_loss() -> None:
    emoji, label = pnl_label(Decimal("-1"))
    assert emoji == "🔴"
    assert label == "LOSS"


def test_pnl_label_breakeven() -> None:
    emoji, label = pnl_label(Decimal("0"))
    assert emoji == "⚪"
    assert label == "BREAKEVEN"


def test_fmt_pct_positive() -> None:
    result = fmt_pct(Decimal("2.49"))
    assert result == "+2.49% (approx.)"


def test_fmt_pct_negative() -> None:
    result = fmt_pct(Decimal("-1.46"))
    assert result == "-1.46% (approx.)"


def test_fmt_pct_none() -> None:
    assert fmt_pct(None) is None
