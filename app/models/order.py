from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_MARKET = "STOP_MARKET"
    CONDITIONAL = "CONDITIONAL"
    TPSL = "TPSL"


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    DISAPPEARED_UNKNOWN = "DISAPPEARED_UNKNOWN"


class Order(BaseModel):
    id: str
    exchange: str
    market: str
    side: OrderSide
    type: OrderType
    price: Decimal | None
    qty: Decimal
    filled_qty: Decimal = Decimal("0")
    status: OrderStatus
    created_at: datetime
    updated_at: datetime | None = None
