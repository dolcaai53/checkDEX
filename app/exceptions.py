from __future__ import annotations


class CheckDEXError(Exception):
    """Base exception for all checkDEX errors."""


class ExchangeAPIError(CheckDEXError):
    """Exchange API returned an error response."""


class ExchangeConnectionError(CheckDEXError):
    """Failed to connect or lost connection to the exchange."""


class ExchangeRateLimitError(CheckDEXError):
    """Exchange rate limit exceeded (HTTP 429)."""
