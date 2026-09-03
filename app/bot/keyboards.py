"""Клавиатуры бота."""

from __future__ import annotations

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

SHARE_CONTACT_TEXT = "📞 Отправить мой номер"


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
