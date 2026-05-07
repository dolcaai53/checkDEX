from __future__ import annotations

from pydantic import BaseModel

from app.models.order import Order
from app.models.position import Position
from app.models.trade import Trade


class OrderOpenedEvent(BaseModel):
    order: Order


class OrderUpdatedEvent(BaseModel):
    order: Order
    previous: Order


class OrderFilledEvent(BaseModel):
    order: Order
    previous: Order


class PositionOpenedEvent(BaseModel):
    position: Position


class PositionUpdatedEvent(BaseModel):
    position: Position
    previous: Position


class PositionClosedEvent(BaseModel):
    trade: Trade
