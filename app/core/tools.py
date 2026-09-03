"""Описание инструментов (function calling) в формате OpenAI tools."""

from __future__ import annotations

from typing import Any

SAVE_LEAD_TOOL_NAME = "save_qualified_lead"

SAVE_QUALIFIED_LEAD: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SAVE_LEAD_TOOL_NAME,
        "description": (
            "Сохранить квалифицированную заявку и мгновенно передать её менеджеру. "
            "Вызывай, как только клиент назвал своё имя, контакт для связи, сроки "
            "и то, что именно ему нужно. Не вызывай, пока контакт не получен."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "client_name": {
                    "type": "string",
                    "description": "Имя клиента так, как он себя назвал.",
                },
                "phone_or_contact": {
                    "type": "string",
                    "description": (
                        "Телефон в любом формате, @username, ссылка t.me или e-mail. "
                        "Только реальный контакт от клиента — заглушки недопустимы."
                    ),
                },
                "dates_or_timing": {
                    "type": "string",
                    "description": (
                        "Когда нужна услуга: конкретные даты, период, время или срочность. "
                        "Например: «12–19 августа», «завтра к 9 утра», «в течение месяца»."
                    ),
                },
                "service_details": {
                    "type": "string",
                    "description": (
                        "Что именно нужно клиенту: конкретная машина / квартира / тур / "
                        "трансфер и озвученные пожелания (класс, количество человек, район и т.п.)."
                    ),
                },
                "budget": {
                    "type": "string",
                    "description": (
                        "Бюджет клиента, если он его называл. Не спрашивай специально "
                        "ради заполнения поля и не выдумывай значение."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Выжимка диалога в 1–2 предложениях для менеджера: суть запроса "
                        "и всё важное, что не поместилось в остальные поля."
                    ),
                },
            },
            "required": [
                "client_name",
                "phone_or_contact",
                "dates_or_timing",
                "service_details",
                "summary",
            ],
            "additionalProperties": False,
        },
    },
}

TOOLS: list[dict[str, Any]] = [SAVE_QUALIFIED_LEAD]
