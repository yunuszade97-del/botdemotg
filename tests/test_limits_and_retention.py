"""Лимиты расходов, аварийный захват лида и хранение персональных данных."""

from __future__ import annotations

from app.bot.services.conversation import TurnContext
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import text_response

CTX = TurnContext(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван Петров")


async def test_llm_calls_are_counted(make_service, repo: Repository) -> None:
    service, _, _ = make_service([text_response("раз"), text_response("два")])

    await service.handle_message(CTX, "первое")
    await service.handle_message(CTX, "второе")

    assert await repo.count_llm_calls_today(1) == 2
    assert await repo.count_llm_calls_today_global() == 2


async def test_per_user_limit_blocks_llm_and_asks_for_contact(
    make_service, settings: Settings, repo: Repository
) -> None:
    """Дойдя до потолка расходов, бот не молчит, а забирает контакт."""
    settings.daily_llm_calls_per_user = 2
    service, llm, _ = make_service([text_response("раз"), text_response("два")])

    await service.handle_message(CTX, "первое")
    await service.handle_message(CTX, "второе")
    third = await service.handle_message(CTX, "третье")

    assert len(llm.calls) == 2  # третье сообщение до модели не дошло
    assert third.rate_limited is True
    assert third.request_contact is True
    assert "менеджер" in third.reply.lower()


async def test_global_limit_blocks_all_users(
    make_service, settings: Settings, repo: Repository
) -> None:
    settings.daily_llm_calls_per_user = 0
    settings.daily_llm_calls_global = 1
    service, llm, _ = make_service([text_response("раз")])

    await service.handle_message(CTX, "первое")
    other = await service.handle_message(TurnContext(chat_id=2), "я другой юзер")

    assert len(llm.calls) == 1
    assert other.rate_limited is True


async def test_zero_limits_disable_the_check(
    make_service, settings: Settings
) -> None:
    settings.daily_llm_calls_per_user = 0
    settings.daily_llm_calls_global = 0
    service, llm, _ = make_service([text_response(f"ответ {i}") for i in range(5)])

    for i in range(5):
        result = await service.handle_message(CTX, f"сообщение {i}")
        assert result.rate_limited is False

    assert len(llm.calls) == 5


async def test_is_llm_unavailable_reflects_limit(
    make_service, settings: Settings
) -> None:
    settings.daily_llm_calls_per_user = 1
    service, _, _ = make_service([text_response("раз")])

    assert await service.is_llm_unavailable(1) is False
    await service.handle_message(CTX, "первое")
    assert await service.is_llm_unavailable(1) is True


async def test_contact_capture_without_llm_saves_lead(
    make_service, repo: Repository
) -> None:
    """Клиент прислал номер кнопкой — заявка не должна зависеть от LLM."""
    service, llm, notifier = make_service([])

    result = await service.capture_contact_without_llm(
        CTX, phone="+79991234567", name="Иван Петров"
    )

    assert llm.calls == []  # модель не понадобилась
    assert result.lead_id is not None
    lead = await repo.get_lead(result.lead_id)
    assert lead is not None
    assert lead.client_name == "Иван Петров"
    assert lead.contact_normalized == "tel:9991234567"
    assert len(notifier.sent) == 1


async def test_contact_capture_includes_prior_questions(
    make_service, repo: Repository
) -> None:
    """Менеджеру нужен контекст: что человек спрашивал до того, как дал номер."""
    service, _, _ = make_service([text_response("ответ")])
    await service.handle_message(CTX, "Есть Toyota RAV4 на август?")

    result = await service.capture_contact_without_llm(CTX, phone="+79991234567", name="")

    lead = await repo.get_lead(result.lead_id)  # type: ignore[arg-type]
    assert lead is not None
    assert "RAV4" in lead.summary


async def test_contact_capture_falls_back_to_telegram_name(
    make_service, repo: Repository
) -> None:
    service, _, _ = make_service([])

    result = await service.capture_contact_without_llm(CTX, phone="+79991234567", name="")

    lead = await repo.get_lead(result.lead_id)  # type: ignore[arg-type]
    assert lead is not None and lead.client_name == "Иван Петров"


# --- хранение персональных данных ---------------------------------------------


async def test_purge_old_messages_keeps_fresh_ones(repo: Repository) -> None:
    await repo.add_message(1, "user", "свежее")
    await repo._db.connection.execute(  # noqa: SLF001 - подделываем возраст записи
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
        (1, "user", "древнее", "2020-01-01T00:00:00+00:00"),
    )
    await repo._db.connection.commit()  # noqa: SLF001

    removed = await repo.purge_old_messages(30)

    assert removed == 1
    assert [m.content for m in await repo.get_history(1, limit=10, max_chars=9999)] == [
        "свежее"
    ]


async def test_purge_with_zero_days_keeps_everything(repo: Repository) -> None:
    """0 в настройке — бессрочное хранение, а не «удалить всё»."""
    await repo.add_message(1, "user", "важное")

    assert await repo.purge_old_messages(0) == 0
    assert await repo.purge_old_leads(0) == 0
    assert len(await repo.get_history(1, limit=10, max_chars=9999)) == 1


async def test_forget_chat_removes_everything(repo: Repository) -> None:
    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван")
    await repo.add_message(1, "user", "привет")
    await repo.register_llm_call(1)
    await repo.create_lead(
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="август",
        service_details="RAV4",
        budget=None,
        summary="кроссовер",
        raw_payload={},
    )

    messages, leads = await repo.forget_chat(1)

    assert (messages, leads) == (1, 1)
    assert await repo.count_leads() == 0
    assert await repo.count_users() == 0
    assert await repo.count_llm_calls_today(1) == 0


async def test_forget_chat_does_not_touch_other_users(repo: Repository) -> None:
    await repo.add_message(1, "user", "мой")
    await repo.add_message(2, "user", "чужой")

    await repo.forget_chat(1)

    assert [m.content for m in await repo.get_history(2, limit=5, max_chars=9999)] == [
        "чужой"
    ]
