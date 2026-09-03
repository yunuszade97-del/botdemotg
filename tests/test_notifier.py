from __future__ import annotations

from app.bot.services.notifier import render_lead
from app.db.models import Lead


def _lead(**overrides) -> Lead:
    base = dict(
        id=7,
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="12–19 августа",
        service_details="Toyota RAV4",
        budget="до 100 GEL",
        summary="Кроссовер на неделю",
        admin_notified=False,
        webhook_delivered=False,
        created_at="2026-09-03T12:00:00+00:00",
    )
    base.update(overrides)
    return Lead(**base)  # type: ignore[arg-type]


def test_render_contains_all_fields() -> None:
    text = render_lead(_lead())

    assert "НОВЫЙ ГОРЯЧИЙ ЛИД" in text
    for expected in ("Иван", "+79991234567", "12–19 августа", "Toyota RAV4", "до 100 GEL"):
        assert expected in text
    assert "@ivan" in text
    assert "tg://user?id=42" in text


def test_render_escapes_html_injection() -> None:
    """Имя приходит из генерации LLM — незакрытый тег ломает отправку."""
    text = render_lead(_lead(client_name="<b>Иван</b> <script>x</script>"))

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_render_handles_missing_budget_and_username() -> None:
    text = render_lead(_lead(budget=None, username=None, tg_user_id=None))

    assert "не обсуждался" in text
    assert "Профиль:</b> —" in text
