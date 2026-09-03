from __future__ import annotations

from app.db.crud import Repository


async def test_history_returns_chronological_window(repo: Repository) -> None:
    for i in range(10):
        await repo.add_message(1, "user", f"сообщение {i}")

    history = await repo.get_history(1, limit=4, max_chars=10_000)

    assert [m.content for m in history] == [
        "сообщение 6",
        "сообщение 7",
        "сообщение 8",
        "сообщение 9",
    ]


async def test_history_respects_char_budget(repo: Repository) -> None:
    """Окно режется и по количеству, и по объёму — токены стоят денег."""
    await repo.add_message(1, "user", "x" * 500)
    await repo.add_message(1, "assistant", "y" * 500)
    await repo.add_message(1, "user", "свежее короткое")

    history = await repo.get_history(1, limit=10, max_chars=600)

    assert [m.content for m in history] == ["y" * 500, "свежее короткое"]


async def test_history_is_isolated_per_chat(repo: Repository) -> None:
    await repo.add_message(1, "user", "чат один")
    await repo.add_message(2, "user", "чат два")

    assert [m.content for m in await repo.get_history(1, limit=10, max_chars=1000)] == ["чат один"]
    assert [m.content for m in await repo.get_history(2, limit=10, max_chars=1000)] == ["чат два"]


async def test_clear_history(repo: Repository) -> None:
    await repo.add_messages(1, [("user", "а"), ("assistant", "б")])

    removed = await repo.clear_history(1)

    assert removed == 2
    assert await repo.get_history(1, limit=10, max_chars=1000) == []


async def _make_lead(repo: Repository, *, chat_id: int = 1, contact: str = "+79991234567"):
    return await repo.create_lead(
        chat_id=chat_id,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact=contact,
        contact_normalized=contact,
        dates_or_timing="12–19 августа",
        service_details="Toyota RAV4",
        budget="до 100 GEL",
        summary="Нужен кроссовер на неделю",
        raw_payload={"client_name": "Иван"},
    )


async def test_create_and_read_lead(repo: Repository) -> None:
    lead = await _make_lead(repo)

    assert lead.id > 0
    assert lead.admin_notified is False
    assert await repo.count_leads() == 1
    assert (await repo.get_lead(lead.id)) == lead


async def test_find_recent_duplicate(repo: Repository) -> None:
    lead = await _make_lead(repo)

    same = await repo.find_recent_duplicate(
        chat_id=1, contact_normalized="+79991234567", window_minutes=180
    )
    other_contact = await repo.find_recent_duplicate(
        chat_id=1, contact_normalized="+70000000000", window_minutes=180
    )
    other_chat = await repo.find_recent_duplicate(
        chat_id=2, contact_normalized="+79991234567", window_minutes=180
    )
    disabled = await repo.find_recent_duplicate(
        chat_id=1, contact_normalized="+79991234567", window_minutes=0
    )

    assert same is not None and same.id == lead.id
    assert other_contact is None
    assert other_chat is None
    assert disabled is None


async def test_pending_notifications_flow(repo: Repository) -> None:
    lead = await _make_lead(repo)

    assert [x.id for x in await repo.list_pending_notifications()] == [lead.id]

    await repo.mark_lead_notified(lead.id)

    assert await repo.list_pending_notifications() == []
