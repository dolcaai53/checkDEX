from __future__ import annotations

import pytest

from app.config import Config


@pytest.fixture
def config() -> Config:
    """Config loaded from .env.test values via environment variables."""
    return Config()


# TODO: Phase 4 — add tmp_db fixture (aiosqlite in-memory or tmp file)
# TODO: Phase 5 — add mock ExchangeAdapter fixture
# TODO: Phase 6 — add mock TelegramNotifier fixture
