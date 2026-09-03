"""Обработка обычных сообщений: текст и присланный контакт."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.bot.keyboards import remove_keyboard
from app.bot.services.conversation import ConversationService, TurnContext
from app.config import Settings
from app.db.crud import Repository

logger = logging.getLogger(__name__)

UNSUPPORTED_CONTENT = (
    "Пока я понимаю только текст 🙂 Напишите, пожалуйста, словами — "
    "что нужно и на какие даты."
)


def _context(message: Message) -> TurnContext:
    user = message.from_user
    return TurnContext(
        chat_id=message.chat.id,
        tg_user_id=user.id if user else None,
        username=user.username if user else None,
        full_name=user.full_name if user else None,
    )


async def _respond(message: Message, conversation: ConversationService, text: str) -> None:
    ctx = _context(message)
    # «печатает…» держим до конца ответа: вызов LLM занимает секунды.
    async with ChatActionSender(
        bot=message.bot, chat_id=message.chat.id, action=ChatAction.TYPING
    ):
        result = await conversation.handle_message(ctx, text)

    # Клавиатуру с запросом контакта убираем, как только контакт получен.
    reply_markup = remove_keyboard() if result.lead_id else None
    await message.answer(result.reply, reply_markup=reply_markup)

    if result.lead_id:
        logger.info("Лид #%s зафиксирован в chat_id=%s", result.lead_id, message.chat.id)


async def handle_contact(
    message: Message,
    conversation: ConversationService,
    repo: Repository,
) -> None:
    """Контакт из кнопки Telegram — подтверждённый платформой номер."""
    contact = message.contact
    assert contact is not None  # гарантировано фильтром F.contact

    await repo.upsert_user(
        chat_id=message.chat.id,
        tg_user_id=message.from_user.id if message.from_user else None,
        username=message.from_user.username if message.from_user else None,
        full_name=message.from_user.full_name if message.from_user else None,
    )

    name = " ".join(filter(None, [contact.first_name, contact.last_name])).strip()
    # Отдаём модели как реплику клиента, чтобы она сама решила, хватает ли
    # данных для вызова save_qualified_lead.
    synthetic = (
        f"Мой номер телефона: {contact.phone_number}."
        + (f" Меня зовут {name}." if name else "")
    )
    await _respond(message, conversation, synthetic)


async def handle_text(
    message: Message, conversation: ConversationService, settings: Settings
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > settings.max_user_message_chars:
        await message.answer(
            "Сообщение слишком длинное — сократите, пожалуйста, до главного 🙏"
        )
        return
    await _respond(message, conversation, text)


async def handle_unsupported(message: Message) -> None:
    """Фото, голосовые, стикеры и прочее — вежливо возвращаем в текст."""
    await message.answer(UNSUPPORTED_CONTENT)


def build_router() -> Router:
    """Свежий роутер на каждый вызов (см. commands.build_router)."""
    router = Router(name="dialog")
    router.message.register(handle_contact, F.contact)
    router.message.register(handle_text, F.text & ~F.text.startswith("/"))
    router.message.register(handle_unsupported)  # catch-all — строго последним
    return router
