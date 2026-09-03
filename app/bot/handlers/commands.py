"""Команды: /start, /reset, /help и админская /stats."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.bot.keyboards import contact_keyboard
from app.bot.services.conversation import ConversationService
from app.config import Settings
from app.core.prompts import DEFAULT_WELCOME
from app.db.crud import Repository

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Просто напишите, что вам нужно и на какие даты — я подберу варианты "
    "и передам заявку менеджеру.\n\n"
    "/start — начать заново\n"
    "/reset — очистить историю диалога\n"
    "/help — эта подсказка"
)


async def cmd_start(
    message: Message,
    settings: Settings,
    repo: Repository,
    conversation: ConversationService,
) -> None:
    user = message.from_user
    await repo.upsert_user(
        chat_id=message.chat.id,
        tg_user_id=user.id if user else None,
        username=user.username if user else None,
        full_name=user.full_name if user else None,
    )
    # /start — это «начать заново»: старый контекст только мешает.
    await conversation.reset(message.chat.id)

    welcome = settings.welcome_message.strip() or DEFAULT_WELCOME.format(
        company_name=settings.company_name,
        company_business=settings.company_business,
    )
    await message.answer(welcome, reply_markup=contact_keyboard())


async def cmd_reset(message: Message, conversation: ConversationService) -> None:
    removed = await conversation.reset(message.chat.id)
    logger.info("История очищена (chat_id=%s, удалено=%s)", message.chat.id, removed)
    await message.answer("Готово, начинаем с чистого листа. Чем могу помочь? 🙂")


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def cmd_stats(message: Message, settings: Settings, repo: Repository) -> None:
    """Сводка по лидам. Доступна только чатам из ADMIN_CHAT_IDS."""
    if message.chat.id not in settings.admin_ids:
        return  # молча: посторонним незачем знать о существовании команды

    total = await repo.count_leads()
    recent = await repo.last_leads(5)
    lines = [f"📊 <b>Всего лидов:</b> {total}"]
    if recent:
        lines.append("\n<b>Последние:</b>")
        lines.extend(
            f"#{lead.id} · {escape(lead.client_name)} · {escape(lead.phone_or_contact)}"
            f" · {escape(lead.created_at.replace('T', ' '))}"
            for lead in recent
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


def build_router() -> Router:
    """Свежий роутер на каждый вызов.

    Модульный синглтон нельзя включить во второй Dispatcher — aiogram бросает
    "Router is already attached". Это ломает тесты и любую пересборку в рантайме.
    """
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_reset, Command("reset"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_stats, Command("stats"))
    return router
