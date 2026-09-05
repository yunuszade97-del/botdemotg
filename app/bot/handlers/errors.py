"""Перехват необработанных исключений в хэндлерах.

Без этого любая ошибка в обработке приводит к тишине: клиент написал,
бот не ответил, человек ушёл. Молчание бота — это потерянный лид,
поэтому в ответ всегда уходит хотя бы извинение с просьбой оставить контакт.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

ERROR_REPLY = (
    "Извините, что-то пошло не так с моей стороны 🙏\n"
    "Напишите, пожалуйста, ещё раз — или оставьте имя и телефон, "
    "и менеджер свяжется с вами сам."
)


def _extract_chat_id(event: ErrorEvent) -> int | None:
    update = event.update
    message = update.message or update.edited_message
    if message is not None:
        return message.chat.id
    if update.callback_query is not None and update.callback_query.message is not None:
        return update.callback_query.message.chat.id
    return None


async def on_unhandled_error(event: ErrorEvent, bot: Bot) -> bool:
    """Логирует исключение и извиняется перед пользователем.

    Возвращает True: апдейт считается обработанным, иначе aiogram в режиме
    polling будет повторять его и наступит на ту же ошибку.
    """
    chat_id = _extract_chat_id(event)
    callback_query = event.update.callback_query

    # Пользователь заблокировал бота или удалил чат — это нормальная жизнь
    # мессенджера, а не сбой. Полный стектрейс здесь только зашумляет логи.
    if isinstance(event.exception, TelegramForbiddenError):
        logger.info("Бот заблокирован пользователем (chat_id=%s)", chat_id)
        return True

    logger.exception(
        "Необработанная ошибка (chat_id=%s): %s",
        chat_id,
        event.exception,
        exc_info=event.exception,
    )

    if chat_id is None:
        return True

    try:
        await bot.send_message(chat_id, ERROR_REPLY)
    except TelegramAPIError:
        # Пользователь заблокировал бота или чат недоступен — писать некуда.
        logger.warning("Не удалось отправить извинение в chat_id=%s", chat_id)

    if callback_query is not None:
        # Извинение ушло в чат отдельным сообщением, но кнопка у клиента
        # так и крутится, пока спиннер не погашен явным ответом на колбэк.
        try:
            await bot.answer_callback_query(callback_query.id)
        except TelegramAPIError:
            logger.debug("Не удалось погасить спиннер callback_query", exc_info=True)

    return True


def build_router() -> Router:
    router = Router(name="errors")
    router.errors.register(on_unhandled_error)
    return router
