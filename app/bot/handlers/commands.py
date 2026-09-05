"""Команды: /start, /reset, /help и админская /stats."""

from __future__ import annotations

import logging
from html import escape

import csv
import io
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from app.bot.handlers.niche import cmd_niche, show_niche_menu
from app.bot.keyboards import contact_keyboard, remove_keyboard
from app.bot.services.conversation import ConversationService
from app.config import Settings
from app.core.niches import NicheRegistry
from app.core.prompts import DEFAULT_WELCOME, NICHE_RESET_NOTICE
from app.db.crud import Repository

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Просто напишите, что вам нужно и на какие даты — я подберу варианты "
    "и передам заявку менеджеру.\n\n"
    "/start — начать заново\n"
    "/reset — очистить историю диалога\n"
    "/forget — удалить все мои данные\n"
    "/help — эта подсказка"
)

HELP_TEXT_SHOWCASE = (
    "Просто напишите, что вам нужно и на какие даты — я подберу варианты "
    "и передам заявку менеджеру.\n\n"
    "/start — начать заново\n"
    "/niche — сменить направление\n"
    "/reset — очистить историю диалога\n"
    "/forget — удалить все мои данные\n"
    "/help — эта подсказка"
)

CSV_COLUMNS = [
    "id",
    "created_at",
    "client_name",
    "phone_or_contact",
    "dates_or_timing",
    "service_details",
    "budget",
    "summary",
    "username",
    "tg_user_id",
    "chat_id",
    "niche",
]


async def cmd_start(
    message: Message,
    settings: Settings,
    repo: Repository,
    conversation: ConversationService,
    niches: NicheRegistry | None,
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

    if niches is not None and niches.enabled:
        # Направление тоже сбрасывается: /start начинает демо с нуля. Если
        # ниша уже была выбрана, вместе с её приветствием висит клавиатура
        # «Отправить мой номер» — без явного снятия она переживает сброс, и
        # контакт может прийти уже при невыбранной нише.
        if await repo.get_chat_profile(message.chat.id) is not None:
            await message.answer(NICHE_RESET_NOTICE, reply_markup=remove_keyboard())
        await repo.set_chat_profile(message.chat.id, None)
        await show_niche_menu(message, settings, niches)
        return

    welcome = settings.welcome_message.strip() or DEFAULT_WELCOME.format(
        company_name=settings.company_name,
        company_business=settings.company_business,
    )
    await message.answer(welcome, reply_markup=contact_keyboard())


async def cmd_reset(message: Message, conversation: ConversationService) -> None:
    removed = await conversation.reset(message.chat.id)
    logger.info("История очищена (chat_id=%s, удалено=%s)", message.chat.id, removed)
    await message.answer("Готово, начинаем с чистого листа. Чем могу помочь? 🙂")


async def cmd_help(message: Message, niches: NicheRegistry | None) -> None:
    text = HELP_TEXT_SHOWCASE if niches is not None and niches.enabled else HELP_TEXT
    await message.answer(text)


async def cmd_stats(message: Message, settings: Settings, repo: Repository) -> None:
    """Сводка по воронке. Доступна только чатам из ADMIN_CHAT_IDS."""
    if message.chat.id not in settings.admin_ids:
        return  # молча: посторонним незачем знать о существовании команды

    users = await repo.count_users()
    dialogs = await repo.count_dialogs()
    total = await repo.count_leads()
    today = await repo.count_leads_since(1)
    week = await repo.count_leads_since(7)
    calls_today = await repo.count_llm_calls_today_global()
    # Конверсия считается от диалогов, а не от нажавших /start: человек,
    # который открыл бота и ушёл, воронку не характеризует.
    conversion = round(total / dialogs * 100, 1) if dialogs else 0.0

    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"👥 Пользователей: <b>{users}</b>",
        f"💬 Диалогов состоялось: <b>{dialogs}</b>",
        f"🔥 Лидов всего: <b>{total}</b> (конверсия {conversion}%)",
        f"📅 За сегодня: <b>{today}</b> · за 7 дней: <b>{week}</b>",
        f"🤖 Вызовов LLM сегодня: <b>{calls_today}</b>",
    ]

    if recent := await repo.last_leads(5):
        lines.append("\n<b>Последние заявки:</b>")
        lines.extend(
            f"#{lead.id} · {escape(lead.client_name)} · {escape(lead.phone_or_contact)}"
            f" · {escape(lead.created_at.replace('T', ' '))}"
            for lead in recent
        )
    else:
        lines.append("\nЗаявок пока нет.")

    await message.answer("\n".join(lines), parse_mode="HTML")


async def cmd_export(message: Message, settings: Settings, repo: Repository) -> None:
    """Выгрузка лидов в CSV — владельцу бизнеса нужна работа вне Telegram."""
    if message.chat.id not in settings.admin_ids:
        return

    leads = await repo.all_leads()
    if not leads:
        await message.answer("Заявок пока нет — выгружать нечего.")
        return

    buffer = io.StringIO()
    # utf-8-sig: без BOM Excel открывает кириллицу кракозябрами.
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow(
            {
                "id": lead.id,
                "created_at": lead.created_at,
                "client_name": lead.client_name,
                "phone_or_contact": lead.phone_or_contact,
                "dates_or_timing": lead.dates_or_timing,
                "service_details": lead.service_details,
                "budget": lead.budget or "",
                "summary": lead.summary,
                "username": lead.username or "",
                "tg_user_id": lead.tg_user_id or "",
                "chat_id": lead.chat_id,
                "niche": lead.profile_slug or "",
            }
        )

    filename = f"leads_{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    document = BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), filename=filename)
    await message.answer_document(document, caption=f"Выгрузка: {len(leads)} заявок.")


async def cmd_forget(message: Message, repo: Repository) -> None:
    """Удаление персональных данных по требованию пользователя."""
    messages, leads = await repo.forget_chat(message.chat.id)
    logger.info(
        "Запрос на удаление данных: chat_id=%s, сообщений=%s, заявок=%s",
        message.chat.id,
        messages,
        leads,
    )
    await message.answer(
        "Готово — удалил историю переписки"
        + (f" и {leads} заявку(и)" if leads else "")
        + ". Если хотите начать заново, напишите /start.",
        reply_markup=remove_keyboard(),
    )


def build_router() -> Router:
    """Свежий роутер на каждый вызов.

    Модульный синглтон нельзя включить во второй Dispatcher — aiogram бросает
    "Router is already attached". Это ломает тесты и любую пересборку в рантайме.
    """
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_reset, Command("reset"))
    router.message.register(cmd_niche, Command("niche"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_export, Command("export"))
    router.message.register(cmd_forget, Command("forget"))
    return router
