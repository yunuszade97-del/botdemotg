"""Оркестрация диалога: история → LLM → инструменты → ответ пользователю."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Coroutine

from pydantic import ValidationError

from app.config import Settings
from app.bot.services.lead_webhook import LeadWebhookSender
from app.bot.services.notifier import AdminNotifier
from app.core.llm_client import LLMClient, LLMError, ToolCall
from app.core.niches import Niche, NicheRegistry
from app.core.prompts import (
    ASK_CONTACT_FALLBACK,
    LLM_FAILURE_REPLY,
    NEED_NICHE_REPLY,
    RATE_LIMIT_REPLY,
    build_system_prompt,
)
from app.core.schemas import QualifiedLead, ToolOutcome
from app.core.tools import REQUEST_PHONE_TOOL_NAME, SAVE_LEAD_TOOL_NAME, TOOLS
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
    profile_slug: str | None = None


@dataclass(slots=True)
class TurnResult:
    reply: str
    lead_id: int | None = None
    degraded: bool = False
    request_contact: bool = False
    rate_limited: bool = False
    need_niche: bool = False


class ConversationService:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: Repository,
        llm: LLMClient,
        notifier: AdminNotifier,
        lead_webhook: LeadWebhookSender | None = None,
        niches: NicheRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._llm = llm
        self._notifier = notifier
        self._lead_webhook = lead_webhook
        self._niches = niches
        self._system_prompt = build_system_prompt(
            company_name=settings.company_name,
            company_business=settings.company_business,
            company_city=settings.company_city,
            working_hours=settings.working_hours,
            knowledge_base=settings.knowledge_base(),
            qualify_fields=settings.qualify_fields,
        )
        # Один диалог обрабатывается строго последовательно: без этого три
        # сообщения подряд дают три параллельных прохода и перемешанную историю.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._background: set[asyncio.Task] = set()

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def showcase_enabled(self) -> bool:
        return self._niches is not None and self._niches.enabled

    async def get_chat_niche(self, chat_id: int) -> Niche | None:
        """Ниша, выбранная для этого чата. None, если витрина выключена или ниша не выбрана."""
        if not self.showcase_enabled:
            return None
        slug = await self._repo.get_chat_profile(chat_id)
        assert self._niches is not None
        return self._niches.get(slug)

    def _response_time(self, niche: Niche | None) -> str:
        return niche.profile.response_time if niche is not None else self._settings.manager_response_time

    async def handle_message(self, ctx: TurnContext, text: str) -> TurnResult:
        async with self._locks[ctx.chat_id]:
            return await self._handle_locked(ctx, text)

    async def _handle_locked(self, ctx: TurnContext, text: str) -> TurnResult:
        user_text = text.strip()[: self._settings.max_user_message_chars]

        niche: Niche | None = None
        if self.showcase_enabled:
            slug = await self._repo.get_chat_profile(ctx.chat_id)
            niche = self._niches.get(slug)  # type: ignore[union-attr]
            if niche is None:
                # Ниша не выбрана: ни платного вызова модели, ни записи в
                # историю — клиент не должен заплатить за случайную нишу.
                return TurnResult(reply=NEED_NICHE_REPLY, need_niche=True)
            ctx.profile_slug = slug

        if await self._is_over_daily_limit(ctx.chat_id):
            # Диалог продолжать нечем, но клиента нельзя терять: предлагаем
            # оставить номер кнопкой — этот путь не требует LLM.
            await self._repo.add_message(ctx.chat_id, "user", user_text)
            return TurnResult(
                reply=RATE_LIMIT_REPLY.format(response_time=self._response_time(niche)),
                request_contact=True,
                rate_limited=True,
            )

        history = await self._repo.get_history(
            ctx.chat_id,
            limit=self._settings.history_limit,
            max_chars=self._settings.history_max_chars,
        )

        system_prompt = niche.system_prompt if niche is not None else self._system_prompt
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(item.as_llm_message() for item in history)
        messages.append({"role": "user", "content": user_text})

        await self._repo.register_llm_call(ctx.chat_id)

        try:
            reply, lead_id, wants_contact = await self._run_tool_loop(ctx, messages, niche)
        except LLMError as exc:
            logger.exception("LLM недоступна для chat_id=%s", ctx.chat_id)
            # Реплику пользователя сохраняем: контекст не должен теряться из-за сбоя.
            await self._repo.add_message(ctx.chat_id, "user", user_text)
            # Владелец бизнеса должен узнать о поломке от бота, а не от клиентов.
            self._spawn(
                self._notifier.alert(
                    "llm_down",
                    f"Модель {self._settings.llm_model} не отвечает: {exc}. "
                    f"Клиенты получают извинения вместо консультаций. "
                    f"Проверьте баланс и ключ LLM_API_KEY.",
                )
            )
            return TurnResult(
                reply=LLM_FAILURE_REPLY, degraded=True, request_contact=True
            )

        # В историю пишем только текстовые ходы. Раунды с tool_calls намеренно
        # не сохраняются: assistant-сообщение с tool_calls без парного tool-ответа
        # ломает следующий запрос к API, а сам лид уже лежит в БД.
        await self._repo.add_messages(
            ctx.chat_id, [("user", user_text), ("assistant", reply)]
        )
        return TurnResult(reply=reply, lead_id=lead_id, request_contact=wants_contact)

    async def _run_tool_loop(
        self, ctx: TurnContext, messages: list[dict[str, Any]], niche: Niche | None
    ) -> tuple[str, int | None, bool]:
        """Диалог с моделью с обработкой вызовов инструментов.

        Один проход тут не работает: после `save_qualified_lead` модели нужно
        отдать результат и запросить ещё одну генерацию — иначе клиент
        не получит подтверждения.
        """
        lead_id: int | None = None
        wants_contact = False

        for round_index in range(self._settings.llm_max_tool_rounds):
            response = await self._llm.complete(messages, tools=TOOLS)

            if not response.wants_tools:
                reply = (response.content or "").strip()
                if reply:
                    return reply, lead_id, wants_contact
                # Пустой ответ после успешного инструмента — подстраховываемся шаблоном.
                if lead_id is not None:
                    return self._confirmation_text(niche), lead_id, wants_contact
                if wants_contact:
                    return ASK_CONTACT_FALLBACK, lead_id, wants_contact
                logger.warning("Пустой ответ LLM без tool_calls (chat_id=%s)", ctx.chat_id)
                return LLM_FAILURE_REPLY, lead_id, wants_contact

            messages.append(response.raw_message)
            for call in response.tool_calls:
                outcome = await self._execute_tool(ctx, call, niche)
                if outcome.lead_id is not None:
                    lead_id = outcome.lead_id
                if outcome.request_contact:
                    wants_contact = True
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(outcome.payload, ensure_ascii=False),
                    }
                )

            if lead_id is not None and round_index == self._settings.llm_max_tool_rounds - 2:
                messages.append({"role": "system", "content": self._followup_hint(niche)})

        logger.warning(
            "Достигнут лимит раундов инструментов (chat_id=%s)", ctx.chat_id
        )
        fallback = self._confirmation_text(niche) if lead_id else LLM_FAILURE_REPLY
        return fallback, lead_id, wants_contact

    async def _execute_tool(
        self, ctx: TurnContext, call: ToolCall, niche: Niche | None
    ) -> ToolOutcome:
        if call.name == REQUEST_PHONE_TOOL_NAME:
            return ToolOutcome(
                payload={
                    "status": "button_shown",
                    "hint": (
                        "Кнопка «Отправить мой номер» показана под сообщением. "
                        "Одной короткой фразой попроси клиента нажать её, объяснив зачем. "
                        "Если он предпочтёт написать контакт текстом — прими текстом."
                    ),
                },
                request_contact=True,
            )

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

        return await self._save_lead(ctx, lead_data, niche)

    async def _save_lead(
        self, ctx: TurnContext, lead_data: QualifiedLead, niche: Niche | None
    ) -> ToolOutcome:
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
            profile_slug=ctx.profile_slug,
        )

        # Уведомление админа не должно задерживать ответ клиенту, но и потеряться
        # молча не может: при сбое лид останется в очереди недоставленных.
        # Обе доставки — фоном и независимо друг от друга: упавшая внешняя
        # система не должна задерживать уведомление менеджеру, и наоборот.
        self._spawn(self._notifier.notify(lead))
        if self._lead_webhook is not None and self._lead_webhook.enabled:
            self._spawn(self._lead_webhook.send(lead))

        return ToolOutcome(
            payload={
                "status": "saved",
                "lead_id": lead.id,
                "manager_response_time": self._response_time(niche),
                "hint": (
                    "Заявка сохранена и отправлена менеджеру. Подтверди это клиенту "
                    "одним коротким сообщением."
                ),
            },
            lead_id=lead.id,
        )

    def _confirmation_text(self, niche: Niche | None) -> str:
        return LEAD_CONFIRMATION_FALLBACK.format(response_time=self._response_time(niche))

    def _followup_hint(self, niche: Niche | None) -> str:
        return _TOOL_FOLLOWUP_HINT.format(response_time=self._response_time(niche))

    async def reset(self, chat_id: int) -> int:
        async with self._locks[chat_id]:
            return await self._repo.clear_history(chat_id)

    async def switch_niche(self, chat_id: int, slug: str) -> Niche | None:
        """Меняет нишу чата: сначала стирает историю, потом переключает профиль.

        Порядок важен: при сбое между операциями безопасное состояние — старая
        ниша с пустой историей, а не новый промпт со старой историей (модель
        начала бы отвечать про другую нишу по старым репликам клиента).
        """
        if self._niches is None:
            return None
        niche = self._niches.get(slug)
        if niche is None:
            return None
        async with self._locks[chat_id]:
            await self._repo.clear_history(chat_id)
            await self._repo.set_chat_profile(chat_id, slug)
        return niche


    async def _is_over_daily_limit(self, chat_id: int) -> bool:
        """Суточный потолок расходов. 0 в настройке — лимит выключен."""
        per_user = self._settings.daily_llm_calls_per_user
        overall = self._settings.daily_llm_calls_global
        if not per_user and not overall:
            return False

        if per_user and await self._repo.count_llm_calls_today(chat_id) >= per_user:
            logger.warning("Суточный лимит вызовов LLM исчерпан (chat_id=%s)", chat_id)
            return True
        if overall and await self._repo.count_llm_calls_today_global() >= overall:
            logger.error("Исчерпан ГЛОБАЛЬНЫЙ суточный лимит вызовов LLM")
            return True
        return False

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Запускает фоновую задачу, удерживая на неё сильную ссылку.

        asyncio хранит задачи только слабо: без этого сборщик мусора способен
        отменить отправку лида на середине.
        """
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_task_failure)

    async def is_llm_unavailable(self, chat_id: int) -> bool:
        """Можно ли вообще обратиться к модели для этого чата."""
        return await self._is_over_daily_limit(chat_id)

    async def capture_contact_without_llm(
        self, ctx: TurnContext, *, phone: str, name: str
    ) -> TurnResult:
        """Сохраняет лид напрямую из кнопки Telegram, минуя LLM.

        Нужен, когда модель недоступна или исчерпан суточный лимит: клиент,
        уже нажавший «Отправить мой номер», не должен пропасть только потому,
        что мы не можем сгенерировать ему красивый ответ.
        """
        async with self._locks[ctx.chat_id]:
            niche = await self.get_chat_niche(ctx.chat_id)
            if niche is not None:
                ctx.profile_slug = niche.profile.slug

            history = await self._repo.get_history(
                ctx.chat_id, limit=6, max_chars=1_000
            )
            asked = " / ".join(m.content for m in history if m.role == "user")
            lead_data = QualifiedLead(
                client_name=name or ctx.full_name or "Клиент Telegram",
                phone_or_contact=phone,
                dates_or_timing="не уточнено — спросить у клиента",
                service_details="не уточнено — заявка принята без диалога",
                summary=(
                    f"Клиент оставил номер кнопкой. Из переписки: {asked}"
                    if asked
                    else "Клиент оставил номер кнопкой, диалог не состоялся."
                ),
            )
            outcome = await self._save_lead(ctx, lead_data, niche)

        return TurnResult(
            reply=self._confirmation_text(niche),
            lead_id=outcome.lead_id,
            degraded=True,
        )


def _log_task_failure(task: asyncio.Task) -> None:
    """Фоновые задачи без этого коллбэка глотают исключения молча."""
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("Фоновая задача упала: %s", exc, exc_info=exc)
