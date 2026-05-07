from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Remove any existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if log_format.lower() == "json":
        formatter = jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)
