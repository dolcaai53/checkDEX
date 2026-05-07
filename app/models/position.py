from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Position(BaseModel):
    market: str
    exchange: str
    side: PositionSide
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    leverage: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    opened_at: datetime | None = None
