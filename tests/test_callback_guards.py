"""Инлайн-кнопки пока есть только в режиме витрины, но middleware и обработчик
ошибок должны быть готовы к callback_query и без неё — иначе нажатия пойдут
мимо ChatGuard, троттлинга и извинения при сбое.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, LeaveChat, SendMessage

from app.config import BASE_DIR, Settings
from app.core.niches import build_registry
from app.core.profile import load_profile
from app.db.crud import Repository
from tests.conftest import text_response
from tests.test_integration import CHAT_ID, MockedSession, _callback_update, _make_dispatcher, _update

GROUP_ID = -1001234567890


def test_registering_callback_middlewares_does_not_change_allowed_updates(
    settings: Settings, repo: Repository
) -> None:
    """allowed_updates у продакшен-вебхука не должен молча меняться от этой правки.

    resolve_used_update_types() выводится из зарегистрированных хэндлеров, а
    callback_query-хэндлеров в проекте пока нет — значит их и не должно быть
    в списке, даже когда на callback_query навешаны middleware.
    """
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    assert "callback_query" not in dispatcher.resolve_used_update_types()


def test_showcase_enabled_adds_callback_query_to_allowed_updates(
    settings: Settings, repo: Repository
) -> None:
    """С витриной появляется callback_query-хэндлер — allowed_updates должен это отразить,
    иначе Telegram не станет доставлять нажатия кнопок направления.
    """
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    niches = build_registry([load_profile("tours", base_dir=BASE_DIR)])
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=niches)

    assert "callback_query" in dispatcher.resolve_used_update_types()


async def test_callback_from_group_is_rejected_and_bot_leaves(
    settings: Settings, repo: Repository
) -> None:
    """ChatGuard должен реагировать на нажатие кнопки так же, как на сообщение."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(
        bot, _callback_update(chat_id=GROUP_ID, chat_type="supergroup")
    )

    assert llm.calls == [], "нажатие из группы не должно доходить до LLM"
    assert any(isinstance(m, LeaveChat) for m in session.sent), "бот не вышел из чата"
    # Извещение о выходе — это сообщение в чат, а не всплывающий тост на кнопке.
    leave_notice = [m for m in session.sent if isinstance(m, SendMessage)]
    assert leave_notice and "только в личных" in leave_notice[0].text
    assert not any(isinstance(m, AnswerCallbackQuery) for m in session.sent)


async def test_callback_shares_throttling_limits_with_messages(
    settings: Settings, repo: Repository
) -> None:
    """Нажатие сразу после сообщения от того же пользователя должно троттлиться."""
    settings.throttle_enabled = True
    settings.throttle_min_interval = 60.0
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("ответ")])

    await dispatcher.feed_update(bot, _update("привет", update_id=1))
    await dispatcher.feed_update(bot, _callback_update(update_id=2))

    # Второе обращение (кнопка) не должно доходить до LLM — тот же лимит, что и у сообщений.
    assert len(llm.calls) == 1
    warnings = [
        m
        for m in session.sent
        if isinstance(m, AnswerCallbackQuery) and m.text
    ]
    assert warnings, "должно быть предупреждение о троттлинге на нажатие"


async def test_repeated_throttled_taps_always_close_spinner(
    settings: Settings, repo: Repository
) -> None:
    """Предупреждение о троттлинге подавляется кулдауном, но спиннер на кнопке
    обязан гаснуть при каждом лишнем нажатии — иначе клиент видит зависшую
    загрузку у всех тапов после первого.
    """
    settings.throttle_enabled = True
    settings.throttle_min_interval = 60.0
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [text_response("ответ")])

    await dispatcher.feed_update(bot, _update("привет", update_id=1))
    await dispatcher.feed_update(bot, _callback_update(update_id=2))
    await dispatcher.feed_update(bot, _callback_update(update_id=3))

    assert len(llm.calls) == 1, "оба нажатия должны были троттлиться"
    answers = [m for m in session.sent if isinstance(m, AnswerCallbackQuery)]
    assert len(answers) == 2, "спиннер должен гаснуть на каждое нажатие, а не только на первое"
    assert answers[0].text, "первое нажатие получает предупреждение"
    assert not answers[1].text, "второе подавлено кулдауном, но спиннер всё равно закрыт"


async def test_callback_handler_error_apologizes_and_closes_spinner(
    settings: Settings, repo: Repository
) -> None:
    """Сбой в обработке нажатия должен закончиться извинением клиенту и погашенным спиннером."""
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    @dispatcher.callback_query()
    async def _boom(callback_query) -> None:
        raise RuntimeError("сбой в обработчике кнопки")

    await dispatcher.feed_update(bot, _callback_update(chat_id=CHAT_ID))

    from app.bot.handlers.errors import ERROR_REPLY

    assert ERROR_REPLY in session.texts
    assert any(isinstance(m, AnswerCallbackQuery) for m in session.sent)
