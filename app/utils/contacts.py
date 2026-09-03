"""Нормализация и валидация контактов клиента.

Результат `normalize_contact` — канонический ключ для дедупликации лидов,
а не «красивый» номер для показа: менеджеру уходит `phone_or_contact` ровно
в том виде, в каком его дал клиент. Поэтому телефоны сводятся к последним
10 цифрам — «+7 999 123-45-67» и «8 999 123 45 67» должны считаться одним
и тем же контактом.

Отдельная валидация нужна потому, что аргументы инструмента приходят из
генерации LLM: модель регулярно подставляет «не указан» или «уточняется»,
а такой лид бесполезен менеджеру.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\d")
# Отрицательный lookbehind, чтобы не спутать e-mail с ником: в Ivan@Example.com
# «@Example» — не username.
_USERNAME = re.compile(r"(?<![\w.@])@([A-Za-z0-9_]{4,32})\b")
_TME = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
PHONE_KEY_DIGITS = 10

# Заглушки, которые модель подставляет вместо реального контакта.
_PLACEHOLDERS = {
    "",
    "-",
    "—",
    "n/a",
    "na",
    "none",
    "null",
    "нет",
    "неизвестно",
    "не указан",
    "не указано",
    "не указана",
    "не предоставлен",
    "не сообщил",
    "не сообщила",
    "уточняется",
    "будет позже",
    "позже",
    "telegram",
    "телеграм",
    "тг",
}


class InvalidContactError(ValueError):
    """Контакт не похож на телефон, @username, ссылку t.me или e-mail."""


def normalize_contact(raw: str | None) -> str:
    """Канонический ключ контакта или InvalidContactError.

    Телефон -> `tel:9991234567`, ник -> `@username`, e-mail -> нижний регистр.
    """
    value = (raw or "").strip()
    if value.casefold() in _PLACEHOLDERS:
        raise InvalidContactError("контакт не указан")

    # Порядок важен: e-mail проверяется раньше ника, иначе «@example» из
    # адреса будет принят за username.
    if match := _EMAIL.search(value):
        return match.group(0).lower()

    if match := _TME.search(value):
        return f"@{match.group(1).lower()}"

    if match := _USERNAME.search(value):
        return f"@{match.group(1).lower()}"

    digits = "".join(_DIGITS.findall(value))
    if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return f"tel:{digits[-PHONE_KEY_DIGITS:]}"

    if len(digits) > MAX_PHONE_DIGITS:
        raise InvalidContactError("в номере слишком много цифр")

    raise InvalidContactError(
        "не похоже на телефон, @username, ссылку t.me или e-mail"
    )


def is_valid_contact(raw: str | None) -> bool:
    try:
        normalize_contact(raw)
    except InvalidContactError:
        return False
    return True


def clean_text_field(raw: str | None, *, max_len: int = 500) -> str:
    """Схлопывает пробелы и подрезает длину — модель любит писать простыни."""
    value = re.sub(r"\s+", " ", (raw or "").strip())
    if len(value) > max_len:
        value = value[: max_len - 1].rstrip() + "…"
    return value
