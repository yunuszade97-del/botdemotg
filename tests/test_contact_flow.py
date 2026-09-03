"""Инструмент request_phone_button, обработчик ошибок и админские команды."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import Chat, Contact, Message, Update, User

from app.bot.keyboards import SHARE_CONTACT_TEXT
from app.bot.services.conversation import TurnContext
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import text_response, tool_response
from tests.test_conversation import VALID_ARGS
from tests.test_integration import CHAT_ID, MockedSession, _make_dispatcher, _update

CTX = TurnContext(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван Петров")


# --- request_phone_button ------------------------------------------------------


async def test_request_phone_button_sets_flag(make_service) -> None:
    service, llm, _ = make_service(
        [
            tool_response("request_phone_button", json.dumps({"reason": "подтвердить бронь"})),
            text_response("Оставьте номер — подтвержу наличие."),
        ]
    )

    result = await service.handle_message(CTX, "Хочу RAV4 на август")

    assert result.request_contact is True
    assert result.lead_id is None
    payload = json.loads(llm.calls[1][-1]["content"])
    assert payload["status"] == "button_shown"


async def test_request_phone_button_falls_back_when_model_stays_silent(
    make_service,
) -> None:
    """Кнопка без текста выглядит как сбой — подставляем осмысленную просьбу."""
    service, _, _ = make_service(
        [
            tool_response("request_phone_button", json.dumps({"reason": "уточнить наличие"})),
            text_response(""),
        ]
    )

    result = await service.handle_message(CTX, "Хочу RAV4")

    assert result.request_contact is True
    assert result.reply.strip() != ""
    assert "номер" in result.reply.lower()


async def test_keyboard_is_attached_when_model_asks_for_contact(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(
        settings,
        repo,
        bot,
        [
            tool_response("request_phone_button", json.dumps({"reason": "бронь"})),
            text_response("Оставьте номер, пожалуйста."),
        ],
    )

    await dispatcher.feed_update(bot, _update("Хочу RAV4 на август"))

    sent = [m for m in session.sent if isinstance(m, SendMessage)][-1]
    assert sent.reply_markup is not None
    assert sent.reply_markup.keyboard[0][0].text == SHARE_CONTACT_TEXT
    assert sent.reply_markup.keyboard[0][0].request_contact is True


async def test_keyboard_is_removed_after_lead_is_saved(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(
        settings,
        repo,
        bot,
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Готово!")],
    )

    await dispatcher.feed_update(bot, _update("Иван, +79991234567, RAV4 12–19 августа"))

    sent = [m for m in session.sent if isinstance(m, SendMessage)][-1]
    assert sent.reply_markup is not None
    assert getattr(sent.reply_markup, "remove_keyboard", False) is True


# --- контакт из кнопки Telegram ------------------------------------------------


def _contact_update(phone: str = "+79991234567") -> Update:
    return Update(
        update_id=99,
        message=Message(
            message_id=99,
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван", username="ivan"),
            contact=Contact(phone_number=phone, first_name="Иван", user_id=CHAT_ID),
        ),
    )


async def test_shared_contact_goes_through_llm_when_available(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(
        settings,
        repo,
        bot,
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Принято!")],
    )

    await dispatcher.feed_update(bot, _contact_update())

    assert len(llm.calls) == 2
    assert "+79991234567" in llm.calls[0][-1]["content"]
    assert await repo.count_leads() == 1


async def test_shared_contact_is_saved_even_when_limit_is_exhausted(
    settings: Settings, repo: Repository
) -> None:
    """Клиент нажал кнопку — заявка обязана дойти, даже если LLM недоступна."""
    settings.throttle_enabled = False
    settings.daily_llm_calls_per_user = 1
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, notifier = _make_dispatcher(
        settings, repo, bot, [text_response("первый ответ")]
    )

    await dispatcher.feed_update(bot, _update("Есть RAV4?"))
    await dispatcher.feed_update(bot, _contact_update())

    assert len(llm.calls) == 1  # на контакт модель уже не вызывалась
    assert await repo.count_leads() == 1
    assert len(notifier.sent) == 1
    lead = (await repo.last_leads(1))[0]
    assert lead.contact_normalized == "tel:9991234567"


# --- обработчик ошибок ---------------------------------------------------------


async def test_handler_crash_still_answers_the_user(
    settings: Settings, repo: Repository, monkeypatch
) -> None:
    """Молчание бота — это потерянный лид, поэтому ошибка не должна быть немой."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [text_response("не дойдёт")])

    import app.bot.handlers.dialog as dialog_module

    async def boom(*args, **kwargs):
        raise RuntimeError("что-то сломалось внутри хэндлера")

    monkeypatch.setattr(dialog_module, "_respond", boom)

    await dispatcher.feed_update(bot, _update("привет"))

    texts = session.texts
    assert texts, "пользователь остался без ответа"
    assert "не так" in texts[-1] or "телефон" in texts[-1]


# --- админские команды ---------------------------------------------------------


@pytest.fixture
def admin_settings(settings: Settings) -> Settings:
    settings.throttle_enabled = False
    settings.admin_chat_ids = str(CHAT_ID)
    return settings


async def test_stats_shows_conversion_for_admin(
    admin_settings: Settings, repo: Repository
) -> None:
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(
        admin_settings,
        repo,
        bot,
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Готово!")],
    )
    await dispatcher.feed_update(bot, _update("Иван, +79991234567, RAV4 в августе"))

    await dispatcher.feed_update(bot, _update("/stats", update_id=2))

    stats = session.texts[-1]
    assert "Лидов всего" in stats and "конверсия" in stats


async def test_export_sends_csv_document(
    admin_settings: Settings, repo: Repository
) -> None:
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(
        admin_settings,
        repo,
        bot,
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Готово!")],
    )
    await dispatcher.feed_update(bot, _update("Иван, +79991234567, RAV4 в августе"))

    await dispatcher.feed_update(bot, _update("/export", update_id=2))

    documents = [m for m in session.sent if isinstance(m, SendDocument)]
    assert len(documents) == 1
    payload = documents[0].document.data.decode("utf-8-sig")
    assert "client_name" in payload.splitlines()[0]
    assert "Иван" in payload


async def test_export_is_admin_only(settings: Settings, repo: Repository) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _update("/export"))

    assert not [m for m in session.sent if isinstance(m, SendDocument)]
    assert session.texts == []


async def test_forget_command_wipes_user_data(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [text_response("ответ")])
    await dispatcher.feed_update(bot, _update("привет"))

    await dispatcher.feed_update(bot, _update("/forget", update_id=2))

    assert await repo.get_history(CHAT_ID, limit=10, max_chars=9999) == []
    assert "удалил" in session.texts[-1].lower()
