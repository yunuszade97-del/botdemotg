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

from aiogram import Bot, BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject

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
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery):
            # У колбэка нет .chat: чат лежит в .message, а .message может
            # отсутствовать (инлайн-режим) — тогда решить нечего, пропускаем.
            if event.message is None:
                return await handler(event, data)
            chat = event.message.chat
        else:
            return await handler(event, data)

        if chat.id in self._admin_ids or chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        if self._allow_groups:
            return await handler(event, data)

        if chat.type in GROUP_TYPES:
            # CallbackQuery.answer() гасит спиннер всплывающим тостом — это
            # не сообщение в чат. Извещение о выходе всегда шлём в чат отдельно.
            await self._leave(chat, data["bot"])
        return None

    async def _leave(self, chat: Chat, bot: Bot) -> None:
        logger.warning(
            "Бот добавлен в чат %s (%s) — выхожу, чтобы не тратить бюджет LLM",
            chat.id,
            chat.type,
        )
        try:
            await bot.send_message(chat.id, LEAVE_NOTICE)
            await chat.leave()
        except TelegramAPIError as exc:
            logger.warning("Не удалось выйти из чата %s: %s", chat.id, exc)
