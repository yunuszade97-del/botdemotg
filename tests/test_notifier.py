from __future__ import annotations

from aiogram import Bot

from app.bot.services.notifier import AdminNotifier, render_lead
from app.config import BASE_DIR
from app.core.niches import build_registry
from app.core.profile import load_profile
from app.db.crud import Repository
from app.db.models import Lead
from tests.test_integration import MockedSession


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


# --- режим витрины: направление в карточке лида ---------------------------


def test_render_without_niche_label_is_unchanged() -> None:
    """Одиночный режим (без ниши) — карточка не должна отличаться от прежней."""
    with_default = render_lead(_lead())
    explicit_none = render_lead(_lead(), niche_label=None)

    assert with_default == explicit_none
    assert "Направление" not in with_default


def test_render_with_niche_label_shows_direction() -> None:
    text = render_lead(_lead(), niche_label="Аренда авто")

    assert "Направление:</b> Аренда авто" in text


def test_render_escapes_niche_label() -> None:
    text = render_lead(_lead(), niche_label="<script>x</script>")

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_notify_shows_human_readable_niche_label(repo: Repository) -> None:
    registry = build_registry([load_profile("tours", base_dir=BASE_DIR)])
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777], niches=registry)
    lead = await repo.create_lead(
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="12–19 августа",
        service_details="Экскурсия по Батуми",
        budget=None,
        summary="Хочет экскурсию",
        raw_payload={},
        profile_slug="tours",
    )

    assert await notifier.notify(lead) is True

    text = session.texts[0]
    assert "Направление:</b>" in text
    assert registry.get("tours").profile.label in text
    assert "tours" not in text


async def test_notify_keeps_default_card_when_niches_registry_is_absent(repo: Repository) -> None:
    """Витрина выключена (реестра нет вовсе) — карточка не должна показывать сырой slug."""
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777])  # niches не передан
    lead = await repo.create_lead(
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="12–19 августа",
        service_details="Аренда авто",
        budget=None,
        summary="Хочет машину",
        raw_payload={},
        profile_slug="rent_car",
    )

    assert await notifier.notify(lead) is True

    text = session.texts[0]
    assert "Направление" not in text
    assert "rent_car" not in text


async def test_notify_falls_back_to_raw_slug_for_unknown_niche(repo: Repository) -> None:
    """Ниша удалена из конфига, а старый лид с её slug остался — отправка не должна падать."""
    registry = build_registry([load_profile("tours", base_dir=BASE_DIR)])
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777], niches=registry)
    lead = await repo.create_lead(
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="12–19 августа",
        service_details="Аренда квартиры",
        budget=None,
        summary="Хочет квартиру",
        raw_payload={},
        profile_slug="real_estate_removed",
    )

    assert await notifier.notify(lead) is True

    text = session.texts[0]
    assert "Направление:</b> real_estate_removed" in text
