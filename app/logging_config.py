"""Единая настройка логирования."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Библиотеки шумят на INFO — оставляем только предупреждения.
    for noisy in ("httpx", "httpcore", "openai._base_client", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
