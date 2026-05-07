from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.order import Order
from app.models.position import Position
from app.models.trade import Trade


class ExchangeAdapter(ABC):
    """Abstract interface for any exchange.

    Each concrete adapter translates exchange-specific API responses into the
    internal Order / Position / Trade models. The rest of the system never
    touches exchange-specific types.
    """

    @property
    @abstractmethod
    def exchange_name(self) -> str:
        """Human-readable exchange name used in log messages and notifications."""

    @abstractmethod
    async def connect(self) -> None:
        """Initialise the API client and verify connectivity."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connections and release resources."""

    @abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """Return all currently open orders."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all currently open positions."""

    @abstractmethod
    async def get_orders_history(self, since: datetime) -> list[Order]:
        """Return order history newer than *since* (UTC)."""

    @abstractmethod
    async def get_positions_history(self, since: datetime) -> list[Trade]:
        """Return closed positions (with realised PnL) newer than *since* (UTC)."""

    @abstractmethod
    async def get_trades(self, since: datetime) -> list[Trade]:
        """Return individual fill events newer than *since* (UTC)."""
