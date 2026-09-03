"""Тесты оркестратора: именно тут живёт вся нетривиальная логика."""

from __future__ import annotations

import asyncio
import json

from app.bot.services.conversation import ConversationService, TurnContext
from app.core.llm_client import LLMError
from app.core.prompts import LLM_FAILURE_REPLY
from app.db.crud import Repository
from tests.conftest import text_response, tool_response

CTX = TurnContext(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван Петров")

VALID_ARGS = json.dumps(
    {
        "client_name": "Иван",
        "phone_or_contact": "+7 999 123-45-67",
        "dates_or_timing": "12–19 августа",
        "service_details": "Toyota RAV4, нужен автомат",
        "budget": "до 100 GEL в сутки",
        "summary": "Клиент ищет кроссовер на неделю в августе.",
    },
    ensure_ascii=False,
)


async def _drain_background_tasks() -> None:
    """Уведомление админа уходит фоновой задачей — даём ей отработать."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_plain_answer_is_persisted(make_service, repo: Repository) -> None:
    service, llm, _ = make_service([text_response("Здравствуйте! Что подыскиваете?")])

    result = await service.handle_message(CTX, "Привет")

    assert result.reply == "Здравствуйте! Что подыскиваете?"
    assert result.lead_id is None
    history = await repo.get_history(1, limit=10, max_chars=10_000)
    assert [(m.role, m.content) for m in history] == [
        ("user", "Привет"),
        ("assistant", "Здравствуйте! Что подыскиваете?"),
    ]


async def test_system_prompt_is_first_message(make_service) -> None:
    service, llm, _ = make_service([text_response("ок")])

    await service.handle_message(CTX, "Привет")

    assert llm.calls[0][0]["role"] == "system"
    assert "TestCo" in llm.calls[0][0]["content"]


async def test_history_is_passed_to_llm(make_service) -> None:
    service, llm, _ = make_service([text_response("раз"), text_response("два")])

    await service.handle_message(CTX, "первое")
    await service.handle_message(CTX, "второе")

    second_call = llm.calls[1]
    assert [m["content"] for m in second_call[1:]] == ["первое", "раз", "второе"]


async def test_tool_call_saves_lead_and_notifies(make_service, repo: Repository) -> None:
    service, llm, notifier = make_service(
        [
            tool_response("save_qualified_lead", VALID_ARGS),
            text_response("Спасибо, зафиксировал! Менеджер свяжется в течение 5 минут."),
        ]
    )

    result = await service.handle_message(CTX, "Иван, +7 999 123-45-67, RAV4 на 12–19 августа")
    await _drain_background_tasks()

    assert result.lead_id is not None
    lead = await repo.get_lead(result.lead_id)
    assert lead is not None
    assert lead.client_name == "Иван"
    assert lead.contact_normalized == "tel:9991234567"
    assert lead.tg_user_id == 42 and lead.username == "ivan"
    assert [x.id for x in notifier.sent] == [lead.id]
    assert "Менеджер свяжется" in result.reply


async def test_tool_result_is_fed_back_to_model(make_service) -> None:
    """Без второго прохода клиент не получил бы подтверждения."""
    service, llm, _ = make_service(
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Готово!")]
    )

    await service.handle_message(CTX, "данные")

    assert len(llm.calls) == 2
    tool_message = llm.calls[1][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["status"] == "saved"


async def test_tool_rounds_are_not_written_to_history(make_service, repo: Repository) -> None:
    """assistant с tool_calls без парного tool-ответа сломал бы следующий запрос."""
    service, _, _ = make_service(
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Готово!")]
    )

    await service.handle_message(CTX, "данные")

    history = await repo.get_history(1, limit=10, max_chars=10_000)
    assert [(m.role, m.content) for m in history] == [
        ("user", "данные"),
        ("assistant", "Готово!"),
    ]


async def test_invalid_contact_rejects_lead_and_asks_again(
    make_service, repo: Repository
) -> None:
    bad_args = json.dumps(
        {
            "client_name": "Иван",
            "phone_or_contact": "не указан",
            "dates_or_timing": "август",
            "service_details": "RAV4",
            "summary": "Хочет кроссовер",
        },
        ensure_ascii=False,
    )
    service, llm, notifier = make_service(
        [
            tool_response("save_qualified_lead", bad_args),
            text_response("Подскажите, пожалуйста, ваш телефон?"),
        ]
    )

    result = await service.handle_message(CTX, "Иван, RAV4 в августе")
    await _drain_background_tasks()

    assert result.lead_id is None
    assert await repo.count_leads() == 0
    assert notifier.sent == []
    payload = json.loads(llm.calls[1][-1]["content"])
    assert payload["status"] == "invalid_arguments"


async def test_duplicate_tool_call_does_not_create_second_lead(
    make_service, repo: Repository
) -> None:
    """Модель любит перевызывать инструмент — админ не должен получать дубли."""
    service, _, notifier = make_service(
        [
            tool_response("save_qualified_lead", VALID_ARGS),
            text_response("Готово!"),
            tool_response("save_qualified_lead", VALID_ARGS, call_id="call_2"),
            text_response("Всё уже у менеджера."),
        ]
    )

    first = await service.handle_message(CTX, "данные")
    await _drain_background_tasks()
    second = await service.handle_message(CTX, "и ещё раз данные")
    await _drain_background_tasks()

    assert await repo.count_leads() == 1
    assert first.lead_id == second.lead_id
    assert len(notifier.sent) == 1


async def test_unknown_tool_is_reported_back(make_service) -> None:
    service, llm, _ = make_service(
        [tool_response("drop_database", "{}"), text_response("Продолжим?")]
    )

    await service.handle_message(CTX, "привет")

    payload = json.loads(llm.calls[1][-1]["content"])
    assert payload["status"] == "error"


async def test_malformed_tool_arguments(make_service, repo: Repository) -> None:
    service, llm, _ = make_service(
        [tool_response("save_qualified_lead", "{не json"), text_response("Уточните данные")]
    )

    result = await service.handle_message(CTX, "данные")

    assert result.lead_id is None
    assert await repo.count_leads() == 0
    assert json.loads(llm.calls[1][-1]["content"])["status"] == "error"


async def test_llm_failure_keeps_user_message_and_degrades_gracefully(
    make_service, repo: Repository
) -> None:
    service, llm, notifier = make_service([])

    async def boom(*args, **kwargs):
        raise LLMError("провайдер недоступен")

    llm.complete = boom  # type: ignore[method-assign]

    result = await service.handle_message(CTX, "Сколько стоит RAV4?")

    await _drain_background_tasks()

    assert result.degraded is True
    assert result.reply == LLM_FAILURE_REPLY
    # Даже в аварии предлагаем кнопку: контакт можно взять и без модели.
    assert result.request_contact is True
    history = await repo.get_history(1, limit=10, max_chars=10_000)
    assert [(m.role, m.content) for m in history] == [("user", "Сколько стоит RAV4?")]
    # Владелец узнаёт о поломке от бота, а не от недовольных клиентов.
    assert [key for key, _ in notifier.alerts] == ["llm_down"]


async def test_tool_round_limit_still_confirms_saved_lead(
    make_service, repo: Repository, settings
) -> None:
    """Даже если модель зациклилась на инструменте, клиент получит ответ."""
    service, _, _ = make_service(
        [
            tool_response("save_qualified_lead", VALID_ARGS, call_id=f"c{i}")
            for i in range(settings.llm_max_tool_rounds)
        ]
    )

    result = await service.handle_message(CTX, "данные")

    assert result.lead_id is not None
    assert "Менеджер свяжется" in result.reply
    assert await repo.count_leads() == 1


async def test_concurrent_messages_are_serialized(make_service, repo: Repository) -> None:
    """Три сообщения подряд не должны перемешать историю."""
    service, _, _ = make_service(
        [text_response("ответ 1"), text_response("ответ 2"), text_response("ответ 3")]
    )

    await asyncio.gather(
        service.handle_message(CTX, "вопрос 1"),
        service.handle_message(CTX, "вопрос 2"),
        service.handle_message(CTX, "вопрос 3"),
    )

    history = await repo.get_history(1, limit=20, max_chars=100_000)
    roles = [m.role for m in history]
    assert roles == ["user", "assistant"] * 3


async def test_reset_clears_history(make_service, repo: Repository) -> None:
    service, _, _ = make_service([text_response("ок")])
    await service.handle_message(CTX, "привет")

    removed = await service.reset(1)

    assert removed == 2
    assert await repo.get_history(1, limit=10, max_chars=1000) == []


async def test_long_message_is_truncated(make_service, settings) -> None:
    service, llm, _ = make_service([text_response("ок")])

    await service.handle_message(CTX, "я" * (settings.max_user_message_chars + 500))

    assert len(llm.calls[0][-1]["content"]) == settings.max_user_message_chars
