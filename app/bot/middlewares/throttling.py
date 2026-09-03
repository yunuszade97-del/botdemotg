"""Ограничение частоты сообщений.

Каждое сообщение — это платный вызов LLM, поэтому троттлинг здесь не
украшение, а защита бюджета: без него один спамящий пользователь
выжигает лимиты API.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.core.prompts import THROTTLE_REPLY

logger = logging.getLogger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        min_interval: float = 1.2,
        messages_per_minute: int = 15,
        warn_cooldown: float = 20.0,
    ) -> None:
        self._min_interval = min_interval
        self._limit = messages_per_minute
        self._warn_cooldown = warn_cooldown
        self._last_seen: dict[int, float] = {}
        self._window: defaultdict[int, deque[float]] = defaultdict(deque)
        self._last_warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.monotonic()

        window = self._window[user_id]
        while window and now - window[0] > 60.0:
            window.popleft()

        # get(...) без сентинела дал бы «0.0» как момент прошлого сообщения,
        # а monotonic() считается от произвольной точки: сразу после старта
        # процесса первое сообщение каждого клиента отбрасывалось бы как спам.
        last_seen = self._last_seen.get(user_id)
        too_fast = last_seen is not None and now - last_seen < self._min_interval
        too_many = len(window) >= self._limit

        if too_fast or too_many:
            logger.info(
                "Троттлинг user_id=%s (too_fast=%s, too_many=%s)",
                user_id,
                too_fast,
                too_many,
            )
            await self._maybe_warn(event, user_id, now)
            return None

        self._last_seen[user_id] = now
        window.append(now)
        self._prune(now)
        return await handler(event, data)

    async def _maybe_warn(self, event: Message, user_id: int, now: float) -> None:
        """Предупреждаем раз в cooldown, иначе спамим в ответ на спам."""
        last_warned = self._last_warned.get(user_id)
        if last_warned is not None and now - last_warned < self._warn_cooldown:
            return
        self._last_warned[user_id] = now
        try:
            await event.answer(THROTTLE_REPLY)
        except Exception:  # noqa: BLE001 - предупреждение не критично
            logger.debug("Не удалось отправить предупреждение о троттлинге", exc_info=True)

    def _prune(self, now: float, max_users: int = 10_000) -> None:
        """Хранилище в памяти: чистим, чтобы оно не росло бесконечно."""
        if len(self._last_seen) <= max_users:
            return
        stale = [uid for uid, ts in self._last_seen.items() if now - ts > 3600]
        for uid in stale:
            self._last_seen.pop(uid, None)
            self._window.pop(uid, None)
            self._last_warned.pop(uid, None)
