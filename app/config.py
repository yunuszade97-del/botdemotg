"""Конфигурация приложения (pydantic-settings, источник — .env / переменные окружения)."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -----------------------------------------------------------
    bot_token: str = Field(..., description="Токен бота от @BotFather")
    admin_chat_ids: str = Field(
        ...,
        description="ID чатов для уведомлений о лидах. Несколько — через запятую.",
    )
    parse_mode: Literal["HTML", "MarkdownV2"] = "HTML"

    # --- Режим доставки апдейтов -------------------------------------------
    use_webhook: bool = False
    webhook_base_url: str = Field(
        default="",
        description="Публичный https-адрес, например https://bot.example.com",
    )
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = Field(
        default="",
        description="secret_token для setWebhook. Пустой — сгенерируется из bot_token.",
    )
    drop_pending_updates: bool = True
    host: str = "0.0.0.0"
    port: int = 8080

    # --- LLM ----------------------------------------------------------------
    llm_api_key: str = Field(..., description="Ключ OpenAI / OpenRouter / совместимого шлюза")
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenRouter: https://openrouter.ai/api/v1",
    )
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.4
    llm_max_tokens: int = 700
    llm_timeout: float = 45.0
    llm_max_retries: int = 2
    llm_max_tool_rounds: int = 3

    # --- База данных --------------------------------------------------------
    db_path: Path = BASE_DIR / "data" / "bot.sqlite3"

    # --- Контекст диалога ---------------------------------------------------
    history_limit: int = Field(default=14, ge=2, le=60)
    history_max_chars: int = Field(default=12_000, ge=1_000)
    max_user_message_chars: int = Field(default=2_000, ge=100)

    # --- Доступ -------------------------------------------------------------
    # По умолчанию бот работает только в личных чатах: в группе он отвечал бы
    # на каждое сообщение платным вызовом LLM.
    allow_group_chats: bool = False

    # --- Анти-спам ----------------------------------------------------------
    throttle_enabled: bool = True
    throttle_min_interval: float = Field(default=1.2, ge=0)
    throttle_messages_per_minute: int = Field(default=15, ge=1)

    # --- Дневные лимиты расходов на LLM -------------------------------------
    # Троттлинг ограничивает частоту, но не объём: 15 сообщений в минуту это
    # 21 600 платных вызовов в сутки с одного аккаунта. 0 — лимит выключен.
    daily_llm_calls_per_user: int = Field(default=60, ge=0)
    daily_llm_calls_global: int = Field(default=3_000, ge=0)

    # --- Дедупликация лидов -------------------------------------------------
    lead_dedup_window_minutes: int = Field(default=180, ge=0)

    # --- Хранение персональных данных ---------------------------------------
    # 0 — хранить бессрочно (осознанное решение, а не значение по умолчанию).
    retention_days_messages: int = Field(default=30, ge=0)
    retention_days_leads: int = Field(default=365, ge=0)
    retention_cleanup_hours: int = Field(default=24, ge=1)

    # --- Дневные лимиты расходов на LLM -------------------------------------
    # Троттлинг ограничивает частоту, но не общий объём: 15 сообщений в минуту
    # это 21600 платных вызовов в сутки с одного аккаунта.
    daily_llm_calls_per_user: int = Field(default=60, ge=0)
    daily_llm_calls_global: int = Field(default=3000, ge=0)

    # --- Хранение персональных данных ---------------------------------------
    # 0 — не удалять никогда.
    retention_days_messages: int = Field(default=30, ge=0)
    retention_days_leads: int = Field(default=365, ge=0)
    retention_interval_hours: int = Field(default=24, ge=1)

    # --- Профиль бизнеса (подставляется в системный промпт) -----------------
    company_name: str = "Наша компания"
    company_business: str = "аренда автомобилей"
    company_city: str = ""
    working_hours: str = "ежедневно 09:00–21:00"
    manager_response_time: str = "5 минут"
    knowledge_file: Path = BASE_DIR / "content" / "knowledge.md"
    welcome_message: str = ""

    # --- Логирование --------------------------------------------------------
    log_level: str = "INFO"

    @field_validator("llm_base_url", "webhook_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("webhook_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def admin_ids(self) -> list[int]:
        """`admin_chat_ids` в виде списка. Строкой — чтобы не требовать JSON в .env."""
        ids: list[int] = []
        for chunk in self.admin_chat_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError as exc:  # pragma: no cover - защита от опечатки в .env
                raise ValueError(f"ADMIN_CHAT_IDS: {chunk!r} не является числом") from exc
        if not ids:
            raise ValueError("ADMIN_CHAT_IDS не должен быть пустым")
        return ids

    @property
    def effective_webhook_secret(self) -> str:
        """Секрет для проверки заголовка X-Telegram-Bot-Api-Secret-Token."""
        if self.webhook_secret:
            return self.webhook_secret
        import hashlib

        return hashlib.sha256(self.bot_token.encode()).hexdigest()[:48]

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url}{self.webhook_path}"

    def knowledge_base(self) -> str:
        """Прайс/FAQ/условия из markdown-файла. Файл не обязателен."""
        try:
            return self.knowledge_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def validate_runtime(self) -> None:
        """Проверки, которые нельзя выразить в аннотациях полей."""
        self.admin_ids  # noqa: B018 - бросит ValueError при некорректном значении
        if self.use_webhook and not self.webhook_base_url:
            raise ValueError("USE_WEBHOOK=true требует заполненного WEBHOOK_BASE_URL")
        if self.use_webhook and not self.webhook_base_url.startswith("https://"):
            raise ValueError("Telegram принимает вебхуки только по https")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
