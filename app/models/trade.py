from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.position import PositionSide


class Trade(BaseModel):
    """Represents a closed position / completed trade."""

    id: str
    exchange: str
    market: str
    side: PositionSide
    size: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realised_pnl: Decimal
    opened_at: datetime | None = None
    closed_at: datetime
