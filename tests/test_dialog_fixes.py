"""Три дефекта диалогового хэндлера перед демо: HTML-парсинг, тишина при
сбое склейки и потеря подписи к медиа."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from app.bot.handlers.errors import ERROR_REPLY
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import text_response
from tests.test_aggregator import DELAY
from tests.test_integration import CHAT_ID, MockedSession, _make_dispatcher, _update


def _media_update(*, caption: str | None, update_id: int = 1, kind: str = "photo") -> Update:
    from aiogram.types import PhotoSize

    kwargs: dict = {}
    if kind == "photo":
        kwargs["photo"] = [
            PhotoSize(file_id="f1", file_unique_id="u1", width=10, height=10)
        ]
    elif kind == "voice":
        from aiogram.types import Voice

        kwargs["voice"] = Voice(file_id="v1", file_unique_id="vu1", duration=3)
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван", username="ivan"),
            caption=caption,
            **kwargs,
        ),
    )


# --- Дефект 1: HTML-парсинг ответа LLM -----------------------------------------


async def test_llm_reply_with_angle_bracket_reaches_client(
    settings: Settings, repo: Repository
) -> None:
    """Символ «<» в ответе модели раньше валил отправку через parse_mode=HTML."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(
        settings, repo, bot, [text_response("Стаж вождения < 2 лет не подходит.")]
    )

    await dispatcher.feed_update(bot, _update("Подойдёт новичок?"))

    sent = [m for m in session.sent if isinstance(m, SendMessage)][-1]
    assert sent.text == "Стаж вождения < 2 лет не подходит."
    # Default("parse_mode") — неразрешённый сентинел с HTML из DefaultBotProperties;
    # для сырого ответа LLM он должен быть явно отключён.
    assert not isinstance(sent.parse_mode, Default)
    assert sent.parse_mode is None


# --- Дефект 2: сбой обработки склеенных сообщений не должен быть немым --------


async def test_aggregator_flush_failure_still_replies_to_client(
    settings: Settings, repo: Repository
) -> None:
    """Исключение в фоновой задаче агрегатора не доходит до error-роутера aiogram —
    клиент должен получить то же извинение, что и при обычном сбое хэндлера."""
    settings.throttle_enabled = False
    settings.message_aggregation_delay = DELAY
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    # Пустой список ответов: первый же вызов FakeLLM.complete упадёт AssertionError.
    dispatcher, llm, _ = _make_dispatcher(
        settings, repo, bot, [], aggregation_delay=DELAY
    )

    await dispatcher.feed_update(bot, _update("привет", update_id=1))
    await asyncio.sleep(DELAY * 6)

    assert session.texts, "клиент остался без ответа при сбое склеенной обработки"
    assert session.texts[-1] == ERROR_REPLY


# --- Дефект 3: подпись к медиа не должна теряться ------------------------------


async def test_photo_with_caption_goes_through_llm(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(
        settings, repo, bot, [text_response("Уточните даты, пожалуйста.")]
    )

    await dispatcher.feed_update(bot, _media_update(caption="Нужен RAV4 на август"))

    assert len(llm.calls) == 1
    assert "RAV4" in llm.calls[0][-1]["content"]
    assert "Уточните даты" in session.texts[-1]


async def test_photo_without_caption_still_gets_unsupported_reply(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _media_update(caption=None))

    assert llm.calls == []
    assert "только текст" in session.texts[0]


async def test_voice_with_caption_is_still_unsupported(
    settings: Settings, repo: Repository
) -> None:
    """Голосовые не должны уходить в LLM даже если формально есть подпись."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(
        bot, _media_update(caption="Нужен RAV4", kind="voice")
    )

    assert llm.calls == []
    assert "только текст" in session.texts[0]
