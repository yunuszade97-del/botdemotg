"""Склейка подряд идущих сообщений одного чата в один ход диалога.

Люди в мессенджерах пишут очередями: «Иван», «+7 999 123-45-67», «на август» —
три сообщения за пару секунд. Обрабатывать каждое отдельным вызовом LLM дорого
и бессмысленно (модель отвечает на обрывок фразы), а отбрасывать лишние
троттлингом — прямая потеря контакта, если номер оказался во втором сообщении.

Поэтому сообщения копятся, пока клиент печатает, и уходят в модель одним
ходом после короткой паузы. Побочный эффект приятный: три сообщения стоят
одного вызова LLM вместо трёх.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Flush = Callable[[str], Awaitable[None]]


class MessageAggregator:
    def __init__(self, *, delay: float, max_chars: int = 4_000) -> None:
        self._delay = delay
        self._max_chars = max_chars
        self._buffers: defaultdict[int, list[str]] = defaultdict(list)
        self._timers: dict[int, asyncio.Task] = {}

    @property
    def enabled(self) -> bool:
        return self._delay > 0

    async def add(self, chat_id: int, text: str, flush: Flush) -> None:
        """Копит сообщение и переносит отправку на `delay` секунд вперёд.

        `flush` пересоздаётся на каждое сообщение, поэтому ответ уходит
        на последнее из очереди — там, где клиент его и ждёт.
        """
        if not self.enabled:
            await flush(text)
            return

        buffer = self._buffers[chat_id]
        buffer.append(text)
        # Ограничение на всякий случай: очередь не должна расти безгранично,
        # если клиент печатает без пауз.
        while sum(len(item) for item in buffer) > self._max_chars and len(buffer) > 1:
            buffer.pop(0)

        if timer := self._timers.get(chat_id):
            timer.cancel()
        self._timers[chat_id] = asyncio.create_task(
            self._flush_later(chat_id, flush), name=f"aggregate-{chat_id}"
        )

    async def _flush_later(self, chat_id: int, flush: Flush) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return  # пришло следующее сообщение — ждём дальше

        texts = self._buffers.pop(chat_id, [])
        self._timers.pop(chat_id, None)
        if not texts:
            return

        try:
            await flush("\n".join(texts))
        except Exception:  # noqa: BLE001 - задача вне цепочки хэндлеров aiogram
            logger.exception("Ошибка обработки склеенных сообщений (chat_id=%s)", chat_id)

    async def close(self) -> None:
        """Отменяет незавершённые таймеры при остановке приложения."""
        for timer in list(self._timers.values()):
            timer.cancel()
        self._timers.clear()
        self._buffers.clear()
