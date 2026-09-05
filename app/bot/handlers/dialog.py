"""Обработка обычных сообщений: текст и присланный контакт."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.bot.handlers.errors import ERROR_REPLY
from app.bot.handlers.niche import show_niche_menu
from app.bot.keyboards import contact_keyboard, remove_keyboard
from app.bot.services.aggregator import MessageAggregator
from app.bot.services.conversation import ConversationService, TurnContext
from app.config import Settings
from app.core.niches import NicheRegistry
from app.core.prompts import CONTACT_BEFORE_NICHE_REPLY
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


async def _respond(
    message: Message,
    conversation: ConversationService,
    settings: Settings,
    niches: NicheRegistry | None,
    text: str,
) -> None:
    ctx = _context(message)
    # «печатает…» держим до конца ответа: вызов LLM занимает секунды.
    async with ChatActionSender(
        bot=message.bot, chat_id=message.chat.id, action=ChatAction.TYPING
    ):
        result = await conversation.handle_message(ctx, text)

    if result.need_niche:
        assert niches is not None
        await message.answer(result.reply)
        await show_niche_menu(message, settings, niches)
        return

    # Клавиатура следует за состоянием диалога: показать кнопку, когда модель
    # просит контакт, и убрать её, как только контакт получен.
    if result.lead_id:
        reply_markup = remove_keyboard()
    elif result.request_contact:
        reply_markup = contact_keyboard()
    else:
        reply_markup = None
    # Ответ модели — сырой текст, не HTML: символ «<» в нём (например,
    # «стаж < 2 лет») ломает парсинг entities и роняет отправку целиком.
    await message.answer(result.reply, reply_markup=reply_markup, parse_mode=None)

    if result.lead_id:
        logger.info("Лид #%s зафиксирован в chat_id=%s", result.lead_id, message.chat.id)


async def handle_contact(
    message: Message,
    conversation: ConversationService,
    settings: Settings,
    niches: NicheRegistry | None,
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

    if conversation.showcase_enabled and await conversation.get_chat_niche(message.chat.id) is None:
        # Ниша не выбрана — сохранять лид не из чего (нечем сверить нишу), а
        # заглушка тут же «съедалась» бы дедупом, когда клиент дойдёт до
        # настоящей заявки после выбора направления. Убираем клавиатуру
        # контакта и уводим к выбору ниши.
        await message.answer(CONTACT_BEFORE_NICHE_REPLY, reply_markup=remove_keyboard())
        logger.info(
            "Контакт получен без выбранной ниши — лид не создан (chat_id=%s)", message.chat.id
        )
        assert niches is not None
        await show_niche_menu(message, settings, niches)
        return

    if await conversation.is_llm_unavailable(message.chat.id):
        # Лимит исчерпан или модель недоступна: сохраняем лид напрямую.
        # Клиент уже прислал номер — терять его из-за отсутствия LLM нельзя.
        result = await conversation.capture_contact_without_llm(
            _context(message), phone=contact.phone_number, name=name
        )
        await message.answer(result.reply, reply_markup=remove_keyboard())
        logger.info("Лид #%s принят без LLM (chat_id=%s)", result.lead_id, message.chat.id)
        return

    # Отдаём модели как реплику клиента, чтобы она сама решила, хватает ли
    # данных для вызова save_qualified_lead.
    synthetic = (
        f"Мой номер телефона: {contact.phone_number}."
        + (f" Меня зовут {name}." if name else "")
    )
    await _respond(message, conversation, settings, niches, synthetic)


async def _handle_aggregated_text(
    message: Message,
    conversation: ConversationService,
    settings: Settings,
    aggregator: MessageAggregator,
    niches: NicheRegistry | None,
    text: str,
) -> None:
    if len(text) > settings.max_user_message_chars:
        await message.answer(
            "Сообщение слишком длинное — сократите, пожалуйста, до главного 🙏"
        )
        return

    # Гейт до буфера агрегатора: иначе после выбора ниши в модель уедет всё,
    # что человек успел написать до выбора направления.
    if conversation.showcase_enabled and await conversation.get_chat_niche(message.chat.id) is None:
        assert niches is not None
        await show_niche_menu(message, settings, niches)
        return

    # Ждём, не допишет ли клиент продолжение: люди отправляют имя, телефон
    # и даты тремя отдельными сообщениями.
    async def flush(joined: str) -> None:
        # Эта корутина выполняется вне цепочки хэндлеров aiogram (в фоновой
        # задаче агрегатора), поэтому error-роутер её исключения не увидит —
        # клиент останется без ответа. Извиняемся сами, тем же текстом.
        try:
            await _respond(message, conversation, settings, niches, joined)
        except Exception:  # noqa: BLE001 - последний рубеж перед тишиной
            logger.exception(
                "Ошибка обработки склеенного сообщения (chat_id=%s)", message.chat.id
            )
            try:
                await message.answer(ERROR_REPLY)
            except Exception:  # noqa: BLE001 - отправка извинения тоже может упасть
                logger.warning(
                    "Не удалось отправить извинение в chat_id=%s", message.chat.id
                )

    await aggregator.add(message.chat.id, text, flush)


async def handle_text(
    message: Message,
    conversation: ConversationService,
    settings: Settings,
    aggregator: MessageAggregator,
    niches: NicheRegistry | None,
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    await _handle_aggregated_text(message, conversation, settings, aggregator, niches, text)


async def handle_caption(
    message: Message,
    conversation: ConversationService,
    settings: Settings,
    aggregator: MessageAggregator,
    niches: NicheRegistry | None,
) -> None:
    """Фото/видео/документ с подписью — суть запроса часто именно в ней."""
    text = (message.caption or "").strip()
    if not text:
        await handle_unsupported(message)
        return
    await _handle_aggregated_text(message, conversation, settings, aggregator, niches, text)


async def handle_unsupported(message: Message) -> None:
    """Фото, голосовые, стикеры и прочее — вежливо возвращаем в текст."""
    await message.answer(UNSUPPORTED_CONTENT)


def build_router() -> Router:
    """Свежий роутер на каждый вызов (см. commands.build_router)."""
    router = Router(name="dialog")
    router.message.register(handle_contact, F.contact)
    router.message.register(handle_text, F.text & ~F.text.startswith("/"))
    router.message.register(handle_caption, F.caption & ~F.voice)
    router.message.register(handle_unsupported)  # catch-all — строго последним
    return router
