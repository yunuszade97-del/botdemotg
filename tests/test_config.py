from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        bot_token="123:TEST",
        admin_chat_ids="777",
        llm_api_key="key",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_admin_ids_parses_list() -> None:
    assert _settings(admin_chat_ids="777, 888;999").admin_ids == [777, 888, 999]


def test_admin_ids_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _settings(admin_chat_ids="abc").admin_ids


def test_webhook_secret_is_derived_when_empty() -> None:
    settings = _settings()

    assert len(settings.effective_webhook_secret) == 48
    assert settings.effective_webhook_secret == _settings().effective_webhook_secret


def test_explicit_webhook_secret_wins() -> None:
    assert _settings(webhook_secret="s3cret").effective_webhook_secret == "s3cret"


def test_webhook_url_is_normalized() -> None:
    settings = _settings(
        webhook_base_url="https://bot.example.com/", webhook_path="telegram/hook"
    )

    assert settings.webhook_url == "https://bot.example.com/telegram/hook"


def test_validate_runtime_requires_https_base_url() -> None:
    with pytest.raises(ValueError, match="WEBHOOK_BASE_URL"):
        _settings(use_webhook=True).validate_runtime()

    with pytest.raises(ValueError, match="https"):
        _settings(use_webhook=True, webhook_base_url="http://bot.example.com").validate_runtime()


def test_validate_runtime_passes_for_polling() -> None:
    _settings().validate_runtime()


def test_knowledge_base_missing_file_is_empty(tmp_path) -> None:
    assert _settings(knowledge_file=tmp_path / "nope.md").knowledge_base() == ""
