"""Minimal logging setup shared by CLI, API and pipeline."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging once (safe to call multiple times)."""

    root = logging.getLogger()
    if any(getattr(h, "_shouhou_configured", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler._shouhou_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
