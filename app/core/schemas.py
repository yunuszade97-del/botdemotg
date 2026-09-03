"""Модели данных лида и результат работы инструмента."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.utils.contacts import (
    InvalidContactError,
    clean_text_field,
    normalize_contact,
)


class QualifiedLead(BaseModel):
    """Аргументы `save_qualified_lead` после валидации.

    Валидация здесь — не формальность: аргументы приходят из генерации LLM,
    то есть это ненадёжный ввод ровно в том же смысле, что и пользовательский.
    """

    client_name: str = Field(min_length=1, max_length=120)
    phone_or_contact: str = Field(min_length=1, max_length=200)
    dates_or_timing: str = Field(min_length=1, max_length=300)
    service_details: str = Field(min_length=1, max_length=600)
    budget: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=600)

    @field_validator("client_name", "dates_or_timing", "service_details", "summary")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = clean_text_field(value, max_len=600)
        if not cleaned:
            raise ValueError("поле не может быть пустым")
        return cleaned

    @field_validator("budget")
    @classmethod
    def _clean_budget(cls, value: str | None) -> str | None:
        cleaned = clean_text_field(value, max_len=200)
        return cleaned or None

    @field_validator("phone_or_contact")
    @classmethod
    def _validate_contact(cls, value: str) -> str:
        try:
            normalize_contact(value)
        except InvalidContactError as exc:
            raise ValueError(str(exc)) from exc
        return clean_text_field(value, max_len=200)

    @property
    def contact_normalized(self) -> str:
        return normalize_contact(self.phone_or_contact)


@dataclass(slots=True)
class ToolOutcome:
    """Что вернуть модели в tool-сообщении и что показать пользователю."""

    payload: dict[str, Any] = field(default_factory=dict)
    lead_id: int | None = None
    duplicate: bool = False
    request_contact: bool = False
