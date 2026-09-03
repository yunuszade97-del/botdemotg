"""Ограничение бота личными чатами.

Без этого фильтра бота достаточно добавить в любой групповой чат, чтобы он
начал отвечать на каждое сообщение — платным вызовом LLM на каждое. Один
активный чат сжигает дневной бюджет за час, и владелец узнаёт об этом
из счёта.

Чаты админов пропускаются всегда: ADMIN_CHAT_IDS может указывать на группу,
куда падают лиды и где работают /stats и /export.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}

LEAVE_NOTICE = (
    "Спасибо, но я умею работать только в личных сообщениях 🙂\n"
    "Напишите мне напрямую — подберу вариант и передам заявку менеджеру."
)


class ChatGuardMiddleware(BaseMiddleware):
    def __init__(self, *, admin_ids: list[int], allow_groups: bool = False) -> None:
        self._admin_ids = set(admin_ids)
        self._allow_groups = allow_groups

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        chat = event.chat
        if chat.id in self._admin_ids or chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        if self._allow_groups:
            return await handler(event, data)

        if chat.type in GROUP_TYPES:
            await self._leave(event)
        return None

    async def _leave(self, event: Message) -> None:
        logger.warning(
            "Бот добавлен в чат %s (%s) — выхожу, чтобы не тратить бюджет LLM",
            event.chat.id,
            event.chat.type,
        )
        try:
            await event.answer(LEAVE_NOTICE)
            await event.chat.leave()
        except TelegramAPIError as exc:
            logger.warning("Не удалось выйти из чата %s: %s", event.chat.id, exc)
