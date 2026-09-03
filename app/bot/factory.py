"""Сборка Bot и Dispatcher со всеми зависимостями."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.bot.handlers import build_router
from app.bot.middlewares import LoggingMiddleware, ThrottlingMiddleware
from app.bot.services.conversation import ConversationService
from app.bot.services.notifier import AdminNotifier
from app.config import Settings
from app.core.llm_client import LLMClient
from app.db.crud import Repository

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Начать заново"),
    BotCommand(command="reset", description="Очистить историю диалога"),
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
) -> tuple[LLMClient, AdminNotifier, ConversationService]:
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
    conversation = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier
    )
    return llm, notifier, conversation


async def setup_bot_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    except Exception:  # noqa: BLE001 - меню команд не критично для работы
        logger.warning("Не удалось установить меню команд", exc_info=True)
