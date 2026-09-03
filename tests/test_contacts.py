from __future__ import annotations

import pytest

from app.utils.contacts import (
    InvalidContactError,
    clean_text_field,
    is_valid_contact,
    normalize_contact,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 999 123-45-67", "tel:9991234567"),
        # Тот же номер в другом формате должен дать тот же ключ дедупа.
        ("8 (999) 123 45 67", "tel:9991234567"),
        ("тел: +995 555 12 34 56", "tel:5555123456"),
        ("@ivan_petrov", "@ivan_petrov"),
        ("пишите в тг @Ivan_Petrov", "@ivan_petrov"),
        ("https://t.me/ivan_petrov", "@ivan_petrov"),
        ("t.me/ivan_petrov", "@ivan_petrov"),
        ("Ivan@Example.COM", "ivan@example.com"),
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    assert normalize_contact(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "не указан", "уточняется", "позже", None, "Иван", "123", "-"],
)
def test_normalize_rejects_placeholders(raw: str | None) -> None:
    """Заглушки от LLM не должны попадать в базу лидов."""
    with pytest.raises(InvalidContactError):
        normalize_contact(raw)


def test_normalize_rejects_too_many_digits() -> None:
    with pytest.raises(InvalidContactError):
        normalize_contact("1234567890123456789")


def test_email_is_not_mistaken_for_username() -> None:
    assert normalize_contact("Ivan@Example.COM") == "ivan@example.com"


def test_is_valid_contact() -> None:
    assert is_valid_contact("+79991234567")
    assert not is_valid_contact("не указан")


def test_clean_text_field_collapses_and_truncates() -> None:
    assert clean_text_field("  много   \n пробелов  ") == "много пробелов"
    assert clean_text_field("а" * 100, max_len=10) == "а" * 9 + "…"
    assert clean_text_field(None) == ""
