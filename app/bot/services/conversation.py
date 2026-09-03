"""Оркестрация диалога: история → LLM → инструменты → ответ пользователю."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.bot.services.notifier import AdminNotifier
from app.core.llm_client import LLMClient, LLMError, ToolCall
from app.core.prompts import LLM_FAILURE_REPLY, build_system_prompt
from app.core.schemas import QualifiedLead, ToolOutcome
from app.core.tools import SAVE_LEAD_TOOL_NAME, TOOLS
from app.db.crud import Repository

logger = logging.getLogger(__name__)

LEAD_CONFIRMATION_FALLBACK = (
    "Спасибо, данные зафиксировал! Менеджер свяжется с вами в течение {response_time}. 🙌"
)

# Подсказка модели во втором проходе, если она вдруг «замолчала» после инструмента.
_TOOL_FOLLOWUP_HINT = (
    "Заявка сохранена и уже у менеджера. Подтверди это клиенту одним коротким "
    "дружелюбным сообщением и скажи, что менеджер свяжется в течение {response_time}."
)


@dataclass(slots=True)
class TurnContext:
    """Данные о собеседнике, нужные для сохранения лида."""

    chat_id: int
    tg_user_id: int | None = None
    username: str | None = None
    full_name: str | None = None


@dataclass(slots=True)
class TurnResult:
    reply: str
    lead_id: int | None = None
    degraded: bool = False


class ConversationService:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: Repository,
        llm: LLMClient,
        notifier: AdminNotifier,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._llm = llm
        self._notifier = notifier
        self._system_prompt = build_system_prompt(
            company_name=settings.company_name,
            company_business=settings.company_business,
            company_city=settings.company_city,
            working_hours=settings.working_hours,
            knowledge_base=settings.knowledge_base(),
        )
        # Один диалог обрабатывается строго последовательно: без этого три
        # сообщения подряд дают три параллельных прохода и перемешанную историю.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._background: set[asyncio.Task] = set()

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    async def handle_message(self, ctx: TurnContext, text: str) -> TurnResult:
        async with self._locks[ctx.chat_id]:
            return await self._handle_locked(ctx, text)

    async def _handle_locked(self, ctx: TurnContext, text: str) -> TurnResult:
        user_text = text.strip()[: self._settings.max_user_message_chars]
        history = await self._repo.get_history(
            ctx.chat_id,
            limit=self._settings.history_limit,
            max_chars=self._settings.history_max_chars,
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt}]
        messages.extend(item.as_llm_message() for item in history)
        messages.append({"role": "user", "content": user_text})

        try:
            reply, lead_id = await self._run_tool_loop(ctx, messages)
        except LLMError:
            logger.exception("LLM недоступна для chat_id=%s", ctx.chat_id)
            # Реплику пользователя сохраняем: контекст не должен теряться из-за сбоя.
            await self._repo.add_message(ctx.chat_id, "user", user_text)
            return TurnResult(reply=LLM_FAILURE_REPLY, degraded=True)

        # В историю пишем только текстовые ходы. Раунды с tool_calls намеренно
        # не сохраняются: assistant-сообщение с tool_calls без парного tool-ответа
        # ломает следующий запрос к API, а сам лид уже лежит в БД.
        await self._repo.add_messages(
            ctx.chat_id, [("user", user_text), ("assistant", reply)]
        )
        return TurnResult(reply=reply, lead_id=lead_id)

    async def _run_tool_loop(
        self, ctx: TurnContext, messages: list[dict[str, Any]]
    ) -> tuple[str, int | None]:
        """Диалог с моделью с обработкой вызовов инструментов.

        Один проход тут не работает: после `save_qualified_lead` модели нужно
        отдать результат и запросить ещё одну генерацию — иначе клиент
        не получит подтверждения.
        """
        lead_id: int | None = None

        for round_index in range(self._settings.llm_max_tool_rounds):
            response = await self._llm.complete(messages, tools=TOOLS)

            if not response.wants_tools:
                reply = (response.content or "").strip()
                if reply:
                    return reply, lead_id
                # Пустой ответ после успешного инструмента — подстраховываемся шаблоном.
                if lead_id is not None:
                    return self._confirmation_text(), lead_id
                logger.warning("Пустой ответ LLM без tool_calls (chat_id=%s)", ctx.chat_id)
                return LLM_FAILURE_REPLY, lead_id

            messages.append(response.raw_message)
            for call in response.tool_calls:
                outcome = await self._execute_tool(ctx, call)
                if outcome.lead_id is not None:
                    lead_id = outcome.lead_id
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(outcome.payload, ensure_ascii=False),
                    }
                )

            if lead_id is not None and round_index == self._settings.llm_max_tool_rounds - 2:
                messages.append({"role": "system", "content": self._followup_hint()})

        logger.warning(
            "Достигнут лимит раундов инструментов (chat_id=%s)", ctx.chat_id
        )
        return self._confirmation_text() if lead_id else LLM_FAILURE_REPLY, lead_id

    async def _execute_tool(self, ctx: TurnContext, call: ToolCall) -> ToolOutcome:
        if call.name != SAVE_LEAD_TOOL_NAME:
            logger.warning("Модель вызвала неизвестный инструмент %r", call.name)
            return ToolOutcome(
                payload={"status": "error", "error": f"Инструмент {call.name} не существует."}
            )

        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            logger.warning("Невалидный JSON в аргументах инструмента: %r", call.arguments)
            return ToolOutcome(
                payload={
                    "status": "error",
                    "error": "Аргументы должны быть валидным JSON. Повтори вызов.",
                }
            )

        try:
            lead_data = QualifiedLead.model_validate(arguments)
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in err['loc']) or 'аргументы'}: {err['msg']}"
                for err in exc.errors()
            ]
            logger.info("Инструмент отклонён валидацией: %s", problems)
            return ToolOutcome(
                payload={
                    "status": "invalid_arguments",
                    "errors": problems,
                    "hint": (
                        "Заявка НЕ сохранена. Переспроси клиента и получи корректные "
                        "данные, затем вызови инструмент снова. Не придумывай значения."
                    ),
                }
            )

        return await self._save_lead(ctx, lead_data)

    async def _save_lead(self, ctx: TurnContext, lead_data: QualifiedLead) -> ToolOutcome:
        contact_normalized = lead_data.contact_normalized

        duplicate = await self._repo.find_recent_duplicate(
            chat_id=ctx.chat_id,
            contact_normalized=contact_normalized,
            window_minutes=self._settings.lead_dedup_window_minutes,
        )
        if duplicate is not None:
            logger.info(
                "Повторный вызов инструмента для chat_id=%s — лид #%s уже создан",
                ctx.chat_id,
                duplicate.id,
            )
            return ToolOutcome(
                payload={
                    "status": "already_saved",
                    "lead_id": duplicate.id,
                    "hint": (
                        "Эта заявка уже передана менеджеру. Не сохраняй её повторно "
                        "и не сообщай клиенту о дубликате — просто продолжи разговор."
                    ),
                },
                lead_id=duplicate.id,
                duplicate=True,
            )

        lead = await self._repo.create_lead(
            chat_id=ctx.chat_id,
            tg_user_id=ctx.tg_user_id,
            username=ctx.username,
            client_name=lead_data.client_name,
            phone_or_contact=lead_data.phone_or_contact,
            contact_normalized=contact_normalized,
            dates_or_timing=lead_data.dates_or_timing,
            service_details=lead_data.service_details,
            budget=lead_data.budget,
            summary=lead_data.summary,
            raw_payload=lead_data.model_dump(),
        )

        # Уведомление админа не должно задерживать ответ клиенту, но и потеряться
        # молча не может: при сбое лид останется в очереди недоставленных.
        task = asyncio.create_task(self._notifier.notify(lead))
        # asyncio держит задачи только слабой ссылкой — без этого сборщик
        # мусора может отменить отправку лида на середине.
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_task_failure)

        return ToolOutcome(
            payload={
                "status": "saved",
                "lead_id": lead.id,
                "manager_response_time": self._settings.manager_response_time,
                "hint": (
                    "Заявка сохранена и отправлена менеджеру. Подтверди это клиенту "
                    "одним коротким сообщением."
                ),
            },
            lead_id=lead.id,
        )

    def _confirmation_text(self) -> str:
        return LEAD_CONFIRMATION_FALLBACK.format(
            response_time=self._settings.manager_response_time
        )

    def _followup_hint(self) -> str:
        return _TOOL_FOLLOWUP_HINT.format(
            response_time=self._settings.manager_response_time
        )

    async def reset(self, chat_id: int) -> int:
        async with self._locks[chat_id]:
            return await self._repo.clear_history(chat_id)


def _log_task_failure(task: asyncio.Task) -> None:
    """Фоновые задачи без этого коллбэка глотают исключения молча."""
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("Фоновая задача упала: %s", exc, exc_info=exc)
