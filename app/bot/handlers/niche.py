"""Выбор направления в режиме витрины: плашка, кнопки, переключение ниши."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    NICHE_CALLBACK_PREFIX,
    contact_keyboard,
    niche_keyboard,
    parse_niche_callback,
    remove_keyboard,
)
from app.bot.services.aggregator import MessageAggregator
from app.bot.services.conversation import ConversationService
from app.config import Settings
from app.core.niches import NicheRegistry
from app.core.prompts import DEFAULT_WELCOME, NICHE_RESET_NOTICE, SHOWCASE_INTRO
from app.db.crud import Repository

logger = logging.getLogger(__name__)


async def show_niche_menu(message: Message, settings: Settings, niches: NicheRegistry) -> None:
    """Плашка «это демо» с кнопками направлений. Общая точка для всех путей,
    которые должны привести клиента к выбору ниши.
    """
    intro = settings.showcase_intro.strip() or SHOWCASE_INTRO
    await message.answer(intro, reply_markup=niche_keyboard(niches), parse_mode="HTML")


async def cmd_niche(
    message: Message, settings: Settings, niches: NicheRegistry | None, repo: Repository
) -> None:
    """Повторный показ плашки и меню — например, чтобы передумать посреди демо."""
    if niches is None or not niches.enabled:
        await message.answer("У этого бота одно направление — переключать нечего.")
        return
    # Ниша уже выбрана — значит висит клавиатура «Отправить мой номер» из её
    # приветствия. Снимаем явно, иначе контакт может прийти при невыбранной
    # (уже сброшенной следующим выбором) нише.
    if await repo.get_chat_profile(message.chat.id) is not None:
        await message.answer(NICHE_RESET_NOTICE, reply_markup=remove_keyboard())
    await show_niche_menu(message, settings, niches)


async def on_niche_selected(
    callback: CallbackQuery,
    conversation: ConversationService,
    aggregator: MessageAggregator,
    niches: NicheRegistry | None,
    settings: Settings,
    repo: Repository,
) -> None:
    """Нажатие кнопки направления.

    `callback.answer()` вызывается на каждом пути — не погашенный спиннер
    на демо выглядит как зависший бот.
    """
    await callback.answer()

    if callback.message is None or niches is None:
        return

    slug = parse_niche_callback(callback.data or "")
    niche = niches.get(slug) if slug is not None else None
    if niche is None:
        # Неизвестный slug — кнопка из старой плашки после правки конфига.
        # Показываем меню заново, а не падаем.
        await show_niche_menu(callback.message, settings, niches)
        return

    chat_id = callback.message.chat.id
    user = callback.from_user
    # switch_niche пишет profile_slug UPDATE'ом: без строки пользователя
    # (например, если плашку открыли командой /niche, минуя /start) выбор
    # молча не сохранился бы.
    await repo.upsert_user(
        chat_id=chat_id,
        tg_user_id=user.id if user else None,
        username=user.username if user else None,
        full_name=user.full_name if user else None,
    )
    # Отменяем буфер агрегатора ДО switch_niche: иначе таймер флаша, который
    # ждёт тот же лок чата, получает его уже после переключения и уезжает в
    # модель со старым текстом, но промптом и базой знаний новой ниши. Если
    # флаш уже стартовал (отменять нечего), он корректно отработает на ещё
    # не переключённой нише.
    aggregator.cancel(chat_id)
    await conversation.switch_niche(chat_id, slug)  # type: ignore[arg-type]

    # Кнопки убираем сразу: иначе клиент может нажать другое направление
    # посреди уже начавшегося диалога, а смена ниши стирает историю.
    # callback.message — MaybeInaccessibleMessage: на слишком старой или
    # удалённой плашке Telegram присылает InaccessibleMessage, у которого
    # edit_reply_markup нет вовсе — это не TelegramAPIError, а AttributeError,
    # и его нельзя дать уронить хэндлер после того как ниша уже переключена.
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            logger.debug("Не удалось убрать кнопки плашки (chat_id=%s)", chat_id, exc_info=True)
    else:
        logger.debug("Плашка недоступна (chat_id=%s) — кнопки снимать нечем", chat_id)

    welcome = niche.profile.welcome.strip() or DEFAULT_WELCOME.format(
        company_name=niche.profile.name, company_business=niche.profile.business
    )
    await callback.message.answer(welcome, reply_markup=contact_keyboard())


def build_router() -> Router:
    """Свежий роутер на каждый вызов (см. commands.build_router).

    Только callback_query: /niche зарегистрирована в commands.build_router,
    потому что должна отвечать и вне режима витрины, а этот роутер
    подключается в дерево только когда витрина включена (см. handlers/__init__.py).
    """
    router = Router(name="niche")
    router.callback_query.register(
        on_niche_selected, F.data.startswith(NICHE_CALLBACK_PREFIX)
    )
    return router


__all__ = ["build_router", "show_niche_menu"]
