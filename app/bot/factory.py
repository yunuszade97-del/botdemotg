"""Сборка Bot и Dispatcher со всеми зависимостями."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.bot.handlers import build_router
from app.bot.middlewares import (
    ChatGuardMiddleware,
    LoggingMiddleware,
    ThrottlingMiddleware,
)
from app.bot.services.conversation import ConversationService
from app.bot.services.lead_webhook import LeadWebhookSender
from app.bot.services.notifier import AdminNotifier
from app.config import Settings
from app.core.llm_client import LLMClient
from app.db.crud import Repository

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Начать заново"),
    BotCommand(command="reset", description="Очистить историю диалога"),
    BotCommand(command="forget", description="Удалить мои данные"),
    BotCommand(command="help", description="Как пользоваться ботом"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode(settings.parse_mode)),
    )


def create_dispatcher(
    *,
    settings: Settings,
    repo: Repository,
    conversation: ConversationService,
) -> Dispatcher:
    # workflow_data: aiogram отдаст эти объекты хэндлерам по имени аргумента.
    dispatcher = Dispatcher(
        settings=settings,
        repo=repo,
        conversation=conversation,
    )

    dispatcher.message.middleware(LoggingMiddleware())
    # Порядок важен: посторонние чаты отсекаются до троттлинга и хэндлеров.
    dispatcher.message.middleware(
        ChatGuardMiddleware(
            admin_ids=settings.admin_ids, allow_groups=settings.allow_group_chats
        )
    )
    if settings.throttle_enabled:
        dispatcher.message.middleware(
            ThrottlingMiddleware(
                min_interval=settings.throttle_min_interval,
                messages_per_minute=settings.throttle_messages_per_minute,
            )
        )

    dispatcher.include_router(build_router())
    return dispatcher


def build_services(
    *, settings: Settings, bot: Bot, repo: Repository
) -> tuple[LLMClient, AdminNotifier, LeadWebhookSender, ConversationService]:
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=settings.admin_ids)
    lead_webhook = LeadWebhookSender(
        url=settings.lead_webhook_url,
        secret=settings.lead_webhook_secret,
        company=settings.company_name,
        timeout=settings.lead_webhook_timeout,
        repo=repo,
    )
    conversation = ConversationService(
        settings=settings,
        repo=repo,
        llm=llm,
        notifier=notifier,
        lead_webhook=lead_webhook,
    )
    return llm, notifier, lead_webhook, conversation


async def setup_bot_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    except Exception:  # noqa: BLE001 - меню команд не критично для работы
        logger.warning("Не удалось установить меню команд", exc_info=True)


async def verify_admin_chats(bot: Bot, admin_ids: list[int]) -> list[int]:
    """Проверяет, что бот может писать в чаты админов.

    Telegram не даёт боту написать первым тому, кто не нажимал /start.
    Молча это выясняется в худший момент — когда придёт первый лид.
    Возвращает список недостижимых чатов.
    """
    unreachable: list[int] = []
    for admin_id in admin_ids:
        try:
            await bot.get_chat(admin_id)
        except TelegramAPIError as exc:
            unreachable.append(admin_id)
            logger.error(
                "Админ-чат %s недоступен (%s). Лиды туда не дойдут. "
                "Откройте бота с этого аккаунта и нажмите /start "
                "(для группы — добавьте бота в неё).",
                admin_id,
                exc,
            )
    if not unreachable:
        logger.info("Все админ-чаты доступны: %s", admin_ids)
    return unreachable
