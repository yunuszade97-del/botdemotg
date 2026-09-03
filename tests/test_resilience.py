"""Устойчивость в бою: посторонние чаты, блокировка бота, алерты о сбоях."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from aiogram.methods import LeaveChat, SendMessage
from aiogram.types import Chat, Message, Update, User

from app.bot.services.notifier import AdminNotifier
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import text_response
from tests.test_integration import CHAT_ID, MockedSession, _make_dispatcher, _update

GROUP_ID = -1001234567890


def _group_update(text: str = "привет всем", chat_id: int = GROUP_ID) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="supergroup", title="Чат района"),
            from_user=User(id=999, is_bot=False, first_name="Кто-то"),
            text=text,
        ),
    )


async def test_bot_leaves_group_without_calling_llm(
    settings: Settings, repo: Repository
) -> None:
    """Бота добавили в группу — каждое сообщение стоило бы вызова LLM."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _group_update())

    assert llm.calls == [], "в группе не должно быть обращений к модели"
    assert any(isinstance(m, LeaveChat) for m in session.sent), "бот не вышел из чата"


async def test_group_is_allowed_when_explicitly_enabled(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    settings.allow_group_chats = True
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("отвечаю")])

    await dispatcher.feed_update(bot, _group_update())

    assert len(llm.calls) == 1
    assert not any(isinstance(m, LeaveChat) for m in session.sent)


async def test_admin_group_always_works(settings: Settings, repo: Repository) -> None:
    """ADMIN_CHAT_IDS может указывать на группу — там нужны /stats и /export."""
    settings.throttle_enabled = False
    settings.admin_chat_ids = str(GROUP_ID)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _group_update("/stats"))

    assert not any(isinstance(m, LeaveChat) for m in session.sent)
    assert any("Статистика" in t for t in session.texts)


async def test_private_chat_is_untouched(settings: Settings, repo: Repository) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("отвечаю")])

    await dispatcher.feed_update(bot, _update("привет"))

    assert len(llm.calls) == 1
    assert not any(isinstance(m, LeaveChat) for m in session.sent)


# --- алерты админу ------------------------------------------------------------


class RecordingSession(MockedSession):
    """Сессия, которая умеет падать по требованию."""

    fail = False

    async def make_request(self, bot, method, timeout=None):
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        return await super().make_request(bot, method, timeout)


async def test_alert_is_deduplicated(repo: Repository) -> None:
    """Упавшая LLM даёт ошибку на каждое сообщение каждого клиента."""
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777], alert_cooldown=900.0)

    first = await notifier.alert("llm_down", "Модель не отвечает")
    second = await notifier.alert("llm_down", "Модель не отвечает")

    assert first is True and second is False
    assert len([m for m in session.sent if isinstance(m, SendMessage)]) == 1


async def test_different_alert_keys_are_independent(repo: Repository) -> None:
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777])

    assert await notifier.alert("llm_down", "модель") is True
    assert await notifier.alert("db_error", "база") is True


async def test_alert_cooldown_expires(repo: Repository) -> None:
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777], alert_cooldown=0.0)

    assert await notifier.alert("llm_down", "модель") is True
    assert await notifier.alert("llm_down", "модель") is True


async def test_alert_escapes_html(repo: Repository) -> None:
    """Текст ошибки может содержать угловые скобки и сломать отправку."""
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(bot=bot, repo=repo, admin_ids=[777])

    await notifier.alert("llm_down", "ошибка <Response 500> у <provider>")

    body = session.texts[0]
    assert "<Response" not in body
    assert "&lt;Response" in body


# --- регрессия: первое сообщение сразу после старта процесса -------------------


async def test_first_message_after_restart_is_not_throttled(
    settings: Settings, repo: Repository
) -> None:
    """monotonic() считается от произвольной точки, а не от старта процесса.

    Со сравнением «now - 0.0 < min_interval» первое сообщение каждого клиента
    отбрасывалось бы как спам — то есть сразу после каждого деплоя.
    """
    settings.throttle_enabled = True
    settings.throttle_min_interval = 3600.0  # заведомо больше, чем uptime машины
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("привет")])

    await dispatcher.feed_update(bot, _update("первое сообщение"))

    assert len(llm.calls) == 1, "первое сообщение не должно попадать под троттлинг"
    assert "привет" in session.texts


async def test_first_alert_after_restart_is_delivered(repo: Repository) -> None:
    """Та же ловушка в кулдауне алертов: первый алерт нельзя проглатывать."""
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    notifier = AdminNotifier(
        bot=bot, repo=repo, admin_ids=[777], alert_cooldown=86_400.0
    )

    assert await notifier.alert("llm_down", "Модель не отвечает") is True
    assert len([m for m in session.sent if isinstance(m, SendMessage)]) == 1
