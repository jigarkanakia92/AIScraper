"""Structured logging via loguru.

Emits human-readable text in dev, JSON in prod. Used everywhere instead of
stdlib logging so we get a single configured sink.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from loguru import logger

from app.core.config import get_settings


def _json_sink(message: Any) -> None:
    record = message.record
    payload = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "msg": record["message"],
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
    }
    if record["extra"]:
        payload["extra"] = record["extra"]
    sys.stdout.write(json.dumps(payload, default=str) + "\n")


def configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    if settings.log_json:
        logger.add(_json_sink, level=settings.log_level)
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            colorize=True,
        )
    logger.info(
        "Logging configured",
        extra={"env": settings.app_env, "level": settings.log_level, "json": settings.log_json},
    )


__all__ = ["configure_logging", "logger"]
