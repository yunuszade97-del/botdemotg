"""Профили ниш: подмена бизнес-блока без правок кода.

Тихая ошибка здесь самая дорогая: бот стартует, отвечает и продаёт — только
чужой прайс. Поэтому загрузчик обязан падать, а не подставлять умолчания.
"""

from __future__ import annotations

import pytest

from app.config import BASE_DIR, Settings
from app.core.profile import ProfileError, available_profiles, load_profile
from app.core.prompts import build_qualify_block, build_system_prompt

SHIPPED = ["real_estate", "rent_car", "tours"]


def _settings(**overrides) -> Settings:
    base = dict(bot_token="123:TEST", admin_chat_ids="777", llm_api_key="key")
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- сами файлы профилей ----------------------------------------------------


def test_shipped_profiles_are_discoverable() -> None:
    assert available_profiles(BASE_DIR) == SHIPPED


@pytest.mark.parametrize("slug", SHIPPED)
def test_shipped_profile_is_complete(slug: str) -> None:
    """Каждая поставляемая ниша готова к демо, а не наполовину заполнена."""
    profile = load_profile(slug, base_dir=BASE_DIR)

    assert profile.name and profile.business
    assert profile.qualify, "без списка qualify промпт теряет специфику ниши"
    assert profile.demo_script, "сценарий демо нужен для ручного прогона"
    assert profile.knowledge_file.is_file(), f"нет файла {profile.knowledge_file}"
    assert len(profile.knowledge_file.read_text(encoding="utf-8")) > 500


def test_unknown_profile_names_the_available_ones() -> None:
    with pytest.raises(ProfileError, match="rent_car"):
        load_profile("barbershop", base_dir=BASE_DIR)


def test_profile_name_cannot_escape_the_directory() -> None:
    with pytest.raises(ProfileError):
        load_profile("../../etc/passwd", base_dir=BASE_DIR)


def test_broken_toml_raises(tmp_path) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "bad.toml").write_text("name = ", encoding="utf-8")

    with pytest.raises(ProfileError, match="TOML"):
        load_profile("bad", base_dir=tmp_path)


def test_missing_required_field_raises(tmp_path) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "half.toml").write_text(
        'name = "X"\nbusiness = "y"\n', encoding="utf-8"
    )

    with pytest.raises(ProfileError, match="knowledge_file"):
        load_profile("half", base_dir=tmp_path)


def test_qualify_must_be_a_list(tmp_path) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "x.toml").write_text(
        'name = "X"\nbusiness = "y"\nknowledge_file = "k.md"\nqualify = "даты"\n',
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="список строк"):
        load_profile("x", base_dir=tmp_path)


# --- склейка профиля с настройками ------------------------------------------


def test_profile_fills_the_business_block() -> None:
    settings = _settings(profile="tours")

    assert settings.company_name == "Georgia Travel"
    assert settings.company_city == "Батуми"
    assert settings.knowledge_file.name == "tours.md"
    assert settings.welcome_message.startswith("Здравствуйте")
    assert settings.qualify_fields


def test_explicit_env_beats_profile() -> None:
    """Один шаблон ниши — много клиентов: город и название меняются точечно."""
    settings = _settings(profile="tours", company_city="Тбилиси", company_name="Ольга Тур")

    assert settings.company_city == "Тбилиси"
    assert settings.company_name == "Ольга Тур"
    assert settings.company_business == "экскурсии и трансферы по Грузии"


def test_blank_env_value_does_not_erase_profile() -> None:
    """`WELCOME_MESSAGE=` в .env для pydantic «задано», но перекрытием не является."""
    settings = _settings(profile="tours", welcome_message="", company_city="")

    assert settings.welcome_message.startswith("Здравствуйте")
    assert settings.company_city == "Батуми"


def test_without_profile_nothing_changes() -> None:
    settings = _settings()

    assert settings.business_profile is None
    assert settings.qualify_fields == ()
    assert settings.company_name == "Наша компания"


def test_unknown_profile_fails_at_startup() -> None:
    """Опечатка в PROFILE роняет старт и называет верные варианты."""
    with pytest.raises(ValueError, match="rent_car"):
        _settings(profile="no-such-niche")


# --- влияние на промпт ------------------------------------------------------


def test_qualify_block_is_empty_without_fields() -> None:
    assert build_qualify_block([]) == ""
    assert build_qualify_block(["  ", ""]) == ""


def test_system_prompt_carries_niche_questions() -> None:
    settings = _settings(profile="tours")
    prompt = build_system_prompt(
        company_name=settings.company_name,
        company_business=settings.company_business,
        company_city=settings.company_city,
        working_hours=settings.working_hours,
        knowledge_base=settings.knowledge_base(),
        qualify_fields=settings.qualify_fields,
    )

    assert "сколько человек" in prompt
    assert "Georgia Travel" in prompt
    # Ниша не должна отменять главное правило: контакт важнее полной анкеты.
    assert "Лид важнее полноты анкеты" in prompt


def test_two_niches_give_different_prompts() -> None:
    def prompt_for(slug: str) -> str:
        s = _settings(profile=slug)
        return build_system_prompt(
            company_name=s.company_name,
            company_business=s.company_business,
            company_city=s.company_city,
            working_hours=s.working_hours,
            knowledge_base=s.knowledge_base(),
            qualify_fields=s.qualify_fields,
        )

    assert prompt_for("tours") != prompt_for("rent_car")
    assert "стаж" in prompt_for("rent_car")
    assert "стаж" not in prompt_for("tours")
