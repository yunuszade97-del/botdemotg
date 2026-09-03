"""Структурное логирование входящих апдейтов и времени обработки."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        chat_id = event.chat.id if isinstance(event, Message) else None
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Ошибка обработки апдейта (chat_id=%s)", chat_id)
            raise
        finally:
            logger.debug(
                "Апдейт обработан за %.0f мс (chat_id=%s)",
                (time.perf_counter() - started) * 1000,
                chat_id,
            )
