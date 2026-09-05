"""check_profile и check_knowledge из app.preflight: одиночный режим и витрина.

Одиночный режим — то, что видит перед деплоем клиент с одной нишей, поэтому
тесты на него фиксируют ровно тот текст, что был до появления витрины.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings
from app.preflight import FAIL, OK, WARN, check_knowledge, check_profile


def _settings(**overrides) -> Settings:
    base = dict(
        bot_token="123:TEST",
        admin_chat_ids="777",
        llm_api_key="key",
        showcase_profiles="",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- Одиночный режим: снимок поведения до витрины -------------------------


async def test_check_profile_single_mode_no_profile_warns_as_before() -> None:
    settings = _settings()

    result = await check_profile(settings)

    assert result.status == WARN
    assert result.detail.startswith("PROFILE не задан, поля берутся из COMPANY_*")


async def test_check_profile_single_mode_with_profile_unchanged() -> None:
    settings = _settings(profile="rent_car")
    profile = settings.business_profile
    assert profile is not None

    result = await check_profile(settings)

    assert result.status == OK
    assert result.detail == (
        f"{profile.slug}: {profile.name} — {profile.business}, "
        f"{len(profile.qualify)} вопросов квалификации"
    )


async def test_check_knowledge_single_mode_missing_file_unchanged(tmp_path) -> None:
    settings = _settings(knowledge_file=tmp_path / "missing.md")

    result = await check_knowledge(settings)

    assert result.status == WARN
    assert result.detail == (
        f"{settings.knowledge_file} пуст или не найден — "
        f"бот не сможет называть цены и условия"
    )


async def test_check_knowledge_single_mode_template_unchanged(tmp_path) -> None:
    kfile = tmp_path / "knowledge.md"
    kfile.write_text("# Заголовок\n\n<!-- заполните -->\n", encoding="utf-8")
    settings = _settings(knowledge_file=kfile)

    result = await check_knowledge(settings)

    assert result.status == WARN
    assert result.detail == (
        f"{settings.knowledge_file} выглядит незаполненным шаблоном — "
        f"впишите прайс, условия и FAQ"
    )


async def test_check_knowledge_single_mode_ok_unchanged(tmp_path) -> None:
    kfile = tmp_path / "knowledge.md"
    text = "\n".join(f"строка {i}" for i in range(6))
    kfile.write_text(text, encoding="utf-8")
    settings = _settings(knowledge_file=kfile)

    result = await check_knowledge(settings)

    assert result.status == OK
    assert result.detail == f"{settings.knowledge_file}, {len(text)} символов"


# --- Режим витрины ----------------------------------------------------------


async def test_check_profile_showcase_lists_all_niches() -> None:
    settings = _settings(showcase_profiles="rent_car, real_estate, tours")

    result = await check_profile(settings)

    assert result.status == OK
    for niche in settings.showcase_niches:
        assert niche.slug in result.detail
        assert niche.name in result.detail
        assert str(len(niche.qualify)) in result.detail


async def test_check_profile_showcase_no_warn_about_missing_profile() -> None:
    settings = _settings(showcase_profiles="rent_car, real_estate, tours")

    result = await check_profile(settings)

    assert "PROFILE не задан" not in result.detail


async def test_check_profile_showcase_warns_on_empty_qualify_names_the_niche() -> None:
    settings = _settings(showcase_profiles="rent_car, tours")
    niches = settings.showcase_niches
    settings._showcase_niches = tuple(  # type: ignore[attr-defined]
        replace(n, qualify=()) if n.slug == "tours" else n for n in niches
    )

    result = await check_profile(settings)

    assert result.status == WARN
    assert "tours" in result.detail
    assert "rent_car" in result.detail


async def test_check_profile_showcase_fails_on_oversized_callback_data() -> None:
    """slug + префикс `niche:` длиннее 64 байт — Telegram отклонит кнопку целиком."""
    settings = _settings(showcase_profiles="rent_car, tours")
    niches = settings.showcase_niches
    oversized_slug = "a" * 64
    settings._showcase_niches = tuple(  # type: ignore[attr-defined]
        replace(n, slug=oversized_slug) if n.slug == "tours" else n for n in niches
    )

    result = await check_profile(settings)

    assert result.status == FAIL
    assert oversized_slug in result.detail


async def test_check_knowledge_showcase_ok_when_all_niches_filled() -> None:
    settings = _settings(showcase_profiles="rent_car, real_estate, tours")

    result = await check_knowledge(settings)

    assert result.status == OK


async def test_check_knowledge_showcase_names_the_empty_niche(monkeypatch) -> None:
    settings = _settings(showcase_profiles="rent_car, tours")

    def fake_read_knowledge(profile):
        if profile.slug == "tours":
            return ""
        return "\n".join(f"строка {i}" for i in range(6))

    monkeypatch.setattr("app.preflight.read_knowledge", fake_read_knowledge)

    result = await check_knowledge(settings)

    assert result.status == WARN
    assert "tours" in result.detail
    assert "rent_car" not in result.detail
