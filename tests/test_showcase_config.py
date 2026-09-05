"""Загрузка ниш витрины в Settings: SHOWCASE_PROFILES.

Ключевой инвариант: у клиента с одной нишей витрина не должна включиться
сама. Отсюда — явная передача showcase_profiles во всех фикстурах, а не
опора на дефолт: переменную SHOWCASE_PROFILES, экспортированную в шелле,
_ignore_dotenv не отвязывает (она про .env, а не про окружение процесса).
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        bot_token="123:TEST",
        admin_chat_ids="777",
        llm_api_key="key",
        showcase_profiles="",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_showcase_disabled_by_default() -> None:
    settings = _settings()

    assert settings.showcase_enabled is False
    assert settings.showcase_niches == ()


def test_unknown_niche_raises_like_unknown_profile() -> None:
    with pytest.raises(ValueError, match="Доступны"):
        _settings(showcase_profiles="no-such-niche")


def test_order_is_preserved_and_duplicates_dropped() -> None:
    settings = _settings(showcase_profiles="tours, rent_car, tours, real_estate")

    assert [n.slug for n in settings.showcase_niches] == ["tours", "rent_car", "real_estate"]


def test_showcase_enabled_when_niches_present() -> None:
    settings = _settings(showcase_profiles="tours")

    assert settings.showcase_enabled is True
    assert len(settings.showcase_niches) == 1


def test_showcase_does_not_touch_profile_business_fields() -> None:
    """SHOWCASE_PROFILES не подставляет первую нишу в бизнес-поля Settings."""
    settings = _settings(showcase_profiles="tours")

    assert settings.company_name == "Наша компания"
    assert settings.company_business == "аренда автомобилей"
    assert settings.business_profile is None


def test_profile_still_expands_when_showcase_is_set() -> None:
    settings = _settings(profile="rent_car", showcase_profiles="tours, real_estate")

    assert settings.business_profile is not None
    assert settings.company_business == settings.business_profile.business
    assert [n.slug for n in settings.showcase_niches] == ["tours", "real_estate"]


def test_single_mode_fields_unaffected_by_empty_showcase() -> None:
    """Пустой showcase_profiles — все семь полей одиночного режима как раньше."""
    settings = _settings(profile="tours")

    assert settings.business_profile is not None
    assert settings.company_name == settings.business_profile.name
    assert settings.company_business == settings.business_profile.business
    assert settings.company_city == settings.business_profile.city
    assert settings.working_hours == settings.business_profile.working_hours
    assert settings.manager_response_time == settings.business_profile.response_time
    assert settings.knowledge_file == settings.business_profile.knowledge_file
    assert settings.welcome_message == settings.business_profile.welcome
