"""Асинхронная обёртка над OpenAI-совместимым Chat Completions API.

Совместима с OpenAI, OpenRouter (в т.ч. Claude 3.5 Haiku через OpenRouter)
и любым локальным шлюзом с тем же контрактом — отличается только
`LLM_BASE_URL` и `LLM_MODEL`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Ошибки, которые имеет смысл повторить: сеть, таймаут, 429, 5xx.
RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


class LLMError(RuntimeError):
    """LLM не ответила после всех повторов."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.4,
        max_tokens: int = 700,
        timeout: float = 45.0,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        # Ретраи ведём сами: нужен единый бэкофф и логирование по попыткам.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def close(self) -> None:
        await self._client.close()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except RETRYABLE as exc:
                last_error = exc
                await self._sleep_backoff(attempt, exc)
                continue
            except APIStatusError as exc:
                if exc.status_code >= 500:
                    last_error = exc
                    await self._sleep_backoff(attempt, exc)
                    continue
                # 4xx (кроме 429) не лечится повтором: битый ключ, модель, схема.
                logger.error("LLM вернула %s: %s", exc.status_code, exc.message)
                raise LLMError(f"LLM отклонила запрос: {exc.status_code}") from exc

            return self._parse(completion)

        raise LLMError("LLM недоступна после повторных попыток") from last_error

    async def _sleep_backoff(self, attempt: int, exc: Exception) -> None:
        if attempt >= self._max_retries:
            return
        delay = min(2**attempt + random.uniform(0, 0.5), 10.0)
        logger.warning(
            "LLM: попытка %s не удалась (%s), повтор через %.1fс",
            attempt + 1,
            type(exc).__name__,
            delay,
        )
        await asyncio.sleep(delay)

    @staticmethod
    def _parse(completion: Any) -> LLMResponse:
        if not completion.choices:
            raise LLMError("LLM вернула пустой ответ")
        message = completion.choices[0].message
        tool_calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
            for call in (message.tool_calls or [])
            if call.type == "function"
        ]
        raw: dict[str, Any] = {"role": "assistant", "content": message.content}
        if tool_calls:
            raw["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in tool_calls
            ]
        return LLMResponse(content=message.content, tool_calls=tool_calls, raw_message=raw)
