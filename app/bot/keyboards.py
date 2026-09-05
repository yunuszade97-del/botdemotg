"""Клавиатуры бота."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.core.niches import NicheRegistry

SHARE_CONTACT_TEXT = "📞 Отправить мой номер"
NICHE_CALLBACK_PREFIX = "niche:"


def contact_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка «поделиться контактом».

    Номер из Telegram приходит подтверждённым самой платформой — это
    надёжнее, чем текст, набранный руками (и уж тем более чем то, что
    в этот текст «увидела» модель).
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SHARE_CONTACT_TEXT, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Напишите вопрос или отправьте номер",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def niche_keyboard(registry: NicheRegistry) -> InlineKeyboardMarkup:
    """Выбор направления в режиме витрины: одна кнопка на нишу.

    Inline, а не reply — reply-поверхность уже занята кнопкой «Отправить мой
    номер», которой управляет модель, и меню на ней вытесняло бы кнопку в
    непредсказуемый момент.
    """
    buttons = []
    for niche in registry:
        callback_data = f"{NICHE_CALLBACK_PREFIX}{niche.profile.slug}"
        buttons.append([InlineKeyboardButton(text=niche.profile.label, callback_data=callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def parse_niche_callback(data: str) -> str | None:
    """Достаёт slug ниши из callback_data. Чужие данные — None, не исключение."""
    if not data.startswith(NICHE_CALLBACK_PREFIX):
        return None
    return data[len(NICHE_CALLBACK_PREFIX):]
