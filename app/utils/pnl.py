from __future__ import annotations

from decimal import Decimal, InvalidOperation


def calculate_pnl_pct(
    realised_pnl: Decimal,
    entry_price: Decimal,
    size: Decimal,
) -> Decimal | None:
    """Approximate PnL percentage without leverage or fees.

    Formula: pnl_pct = (realised_pnl / (entry_price * size)) * 100

    Returns None when inputs are invalid. Label result as "(approx.)" in UI
    because leverage and fees are not included.
    """
    try:
        if entry_price <= 0 or size <= 0:
            return None
        notional = entry_price * size
        if notional == 0:
            return None
        return (realised_pnl / notional * Decimal("100")).quantize(Decimal("0.01"))
    except (InvalidOperation, ZeroDivisionError):
        return None


def pnl_label(realised_pnl: Decimal) -> tuple[str, str]:
    """Return (emoji, label) for profit/loss/breakeven display."""
    if realised_pnl > 0:
        return "🟢", "PROFIT"
    if realised_pnl < 0:
        return "🔴", "LOSS"
    return "⚪", "BREAKEVEN"


def fmt_pnl(pnl: Decimal) -> str:
    """Format PnL with explicit + sign for positive values."""
    sign = "+" if pnl > 0 else ""
    return f"{sign}{pnl:.2f} USDC"


def fmt_pct(pct: Decimal | None) -> str | None:
    """Format PnL % with sign, or None if unavailable."""
    if pct is None:
        return None
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}% (approx.)"
