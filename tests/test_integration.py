"""Сквозные тесты склейки: апдейт Telegram -> роутер -> хэндлер -> ответ.

Юнит-тесты не покрывают регистрацию роутеров, middlewares и маршруты
FastAPI, а именно там ломается сборка при рефакторинге.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User

from app.bot.factory import create_dispatcher
from app.bot.services.aggregator import MessageAggregator
from app.bot.services.conversation import ConversationService
from app.bot.services.notifier import AdminNotifier
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import FakeLLM, FakeNotifier, text_response, tool_response
from tests.test_conversation import VALID_ARGS

CHAT_ID = 555


class MockedSession(BaseSession):
    """Перехватывает исходящие вызовы Bot API вместо похода в сеть."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []

    async def close(self) -> None:  # pragma: no cover - вызывается при teardown
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout=None) -> Any:
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent if isinstance(m, SendMessage)]


@pytest.fixture
def mocked_session() -> MockedSession:
    return MockedSession()


@pytest.fixture
def bot(mocked_session: MockedSession) -> Bot:
    return Bot(token="123:TEST", session=mocked_session)


def _make_dispatcher(
    settings: Settings, repo: Repository, bot: Bot, responses, *, aggregation_delay: float = 0.0
):
    llm = FakeLLM(responses)
    notifier = FakeNotifier()
    conversation = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier  # type: ignore[arg-type]
    )
    dispatcher = create_dispatcher(
        settings=settings,
        repo=repo,
        conversation=conversation,
        aggregator=MessageAggregator(delay=aggregation_delay),
    )
    return dispatcher, llm, notifier


def _update(text: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван", username="ivan"),
            text=text,
        ),
    )


async def test_start_command_replies_with_welcome(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    settings.throttle_enabled = False
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _update("/start"))

    assert mocked_session.texts
    assert "TestCo" in mocked_session.texts[0]
    # /start должен зарегистрировать пользователя для будущих лидов.
    cursor = await repo._db.connection.execute(  # noqa: SLF001 - проверка побочного эффекта
        "SELECT username FROM users WHERE chat_id = ?", (CHAT_ID,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and row["username"] == "ivan"


async def test_text_message_goes_through_llm(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    settings.throttle_enabled = False
    dispatcher, llm, _ = _make_dispatcher(
        settings, repo, bot, [text_response("Есть RAV4 на эти даты.")]
    )

    await dispatcher.feed_update(bot, _update("Есть кроссовер на август?"))

    assert "Есть RAV4 на эти даты." in mocked_session.texts
    assert len(llm.calls) == 1


async def test_full_lead_flow_end_to_end(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    """Сообщение с контактом -> лид в БД -> уведомление админа -> ответ клиенту."""
    settings.throttle_enabled = False
    dispatcher, _, notifier = _make_dispatcher(
        settings,
        repo,
        bot,
        [
            tool_response("save_qualified_lead", VALID_ARGS),
            text_response("Спасибо, зафиксировал! Менеджер свяжется в течение 5 минут."),
        ],
    )

    await dispatcher.feed_update(bot, _update("Иван, +79991234567, RAV4 на 12–19 августа"))

    assert await repo.count_leads() == 1
    assert len(notifier.sent) == 1
    assert "Менеджер свяжется" in mocked_session.texts[-1]


async def test_throttling_blocks_burst(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    settings.throttle_enabled = True
    settings.throttle_min_interval = 60.0
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("первый ответ")])

    await dispatcher.feed_update(bot, _update("раз", update_id=1))
    await dispatcher.feed_update(bot, _update("два", update_id=2))

    # Второе сообщение не должно дойти до LLM — иначе спам жжёт бюджет.
    assert len(llm.calls) == 1
    assert "первый ответ" in mocked_session.texts


async def test_unsupported_content_gets_polite_reply(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    settings.throttle_enabled = False
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [])

    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван"),
            sticker=None,
            photo=[],
        ),
    )
    await dispatcher.feed_update(bot, update)

    assert llm.calls == []
    assert "только текст" in mocked_session.texts[0]


async def test_stats_is_admin_only(
    settings: Settings, repo: Repository, bot: Bot, mocked_session: MockedSession
) -> None:
    settings.throttle_enabled = False
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _update("/stats"))

    assert mocked_session.texts == []


def test_webhook_route_is_registered(webhook_client) -> None:
    routes = {getattr(r, "path", None) for r in webhook_client.app.routes}

    assert "/telegram/webhook" in routes


# --- Вебхук: проверка подлинности запроса --------------------------------------


@pytest.fixture
def webhook_client(monkeypatch):
    """TestClient без lifespan: приложение «ещё не поднялось»."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "777")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("WEBHOOK_PATH", "/telegram/webhook")

    from app.config import get_settings

    get_settings.cache_clear()
    import app.main as main_module

    try:
        yield TestClient(main_module.create_app())
    finally:
        get_settings.cache_clear()


def test_webhook_rejects_missing_secret(webhook_client) -> None:
    """Без проверки заголовка эндпойнт принимал бы поддельные апдейты от кого угодно."""
    assert webhook_client.post("/telegram/webhook", json={"update_id": 1}).status_code == 403


def test_webhook_rejects_wrong_secret(webhook_client) -> None:
    response = webhook_client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "guess"},
    )

    assert response.status_code == 403


def test_webhook_with_valid_secret_reports_not_ready_before_startup(webhook_client) -> None:
    """Проверка секрета не должна зависеть от того, отработал ли lifespan."""
    response = webhook_client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"},
    )

    assert response.status_code == 503


def test_healthz_reports_mode(webhook_client) -> None:
    payload = webhook_client.get("/healthz").json()

    assert payload["status"] == "ok"
    assert payload["mode"] == "polling"
