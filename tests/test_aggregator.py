"""Склейка подряд идущих сообщений — защита от потери контакта в очереди."""

from __future__ import annotations

import asyncio

from aiogram import Bot

from app.bot.services.aggregator import MessageAggregator
from app.config import Settings
from app.db.crud import Repository
from tests.conftest import text_response, tool_response
from tests.test_conversation import VALID_ARGS
from tests.test_integration import MockedSession, _make_dispatcher, _update

DELAY = 0.05  # достаточно, чтобы поймать логику, и незаметно для прогона


async def test_consecutive_messages_become_one_turn() -> None:
    aggregator = MessageAggregator(delay=DELAY)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    await aggregator.add(1, "Иван", flush)
    await aggregator.add(1, "+7 999 123-45-67", flush)
    await aggregator.add(1, "на 12–19 августа", flush)
    await asyncio.sleep(DELAY * 4)

    assert flushed == ["Иван\n+7 999 123-45-67\nна 12–19 августа"]


async def test_pause_between_messages_makes_two_turns() -> None:
    aggregator = MessageAggregator(delay=DELAY)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    await aggregator.add(1, "первый вопрос", flush)
    await asyncio.sleep(DELAY * 4)
    await aggregator.add(1, "второй вопрос", flush)
    await asyncio.sleep(DELAY * 4)

    assert flushed == ["первый вопрос", "второй вопрос"]


async def test_chats_do_not_mix() -> None:
    aggregator = MessageAggregator(delay=DELAY)
    flushed: list[tuple[int, str]] = []

    def make_flush(chat_id: int):
        async def flush(text: str) -> None:
            flushed.append((chat_id, text))

        return flush

    await aggregator.add(1, "я первый", make_flush(1))
    await aggregator.add(2, "я второй", make_flush(2))
    await asyncio.sleep(DELAY * 4)

    assert sorted(flushed) == [(1, "я первый"), (2, "я второй")]


async def test_zero_delay_processes_immediately() -> None:
    aggregator = MessageAggregator(delay=0.0)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    await aggregator.add(1, "сразу", flush)

    assert flushed == ["сразу"]  # без ожидания


async def test_buffer_is_trimmed_to_max_chars() -> None:
    aggregator = MessageAggregator(delay=DELAY, max_chars=20)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    for i in range(5):
        await aggregator.add(1, f"сообщение-{i}", flush)
    await asyncio.sleep(DELAY * 4)

    assert len(flushed[0]) <= 30
    assert "сообщение-4" in flushed[0]  # последнее не теряется


async def test_flush_error_does_not_crash_the_loop() -> None:
    """Задача агрегатора живёт вне цепочки хэндлеров — исключение некому ловить."""
    aggregator = MessageAggregator(delay=DELAY)

    async def boom(text: str) -> None:
        raise RuntimeError("сломалось")

    await aggregator.add(1, "текст", boom)
    await asyncio.sleep(DELAY * 4)

    ok: list[str] = []

    async def flush(text: str) -> None:
        ok.append(text)

    await aggregator.add(1, "следующее", flush)
    await asyncio.sleep(DELAY * 4)

    assert ok == ["следующее"]


async def test_cancel_discards_pending_queue() -> None:
    """Нужен при смене ниши: недоставленное сообщение не должно флашиться в новый промпт."""
    aggregator = MessageAggregator(delay=10.0)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    await aggregator.add(1, "не должно уйти", flush)
    aggregator.cancel(1)
    await asyncio.sleep(0)

    assert flushed == []


async def test_cancel_is_safe_without_pending_timer() -> None:
    """delay=0: ни буфера, ни таймера для чата нет — cancel не должен падать."""
    aggregator = MessageAggregator(delay=0.0)

    aggregator.cancel(1)  # не должно бросить исключение


async def test_close_cancels_pending_timers() -> None:
    aggregator = MessageAggregator(delay=10.0)
    flushed: list[str] = []

    async def flush(text: str) -> None:
        flushed.append(text)

    await aggregator.add(1, "не должно уйти", flush)
    await aggregator.close()
    await asyncio.sleep(0)

    assert flushed == []


# --- сквозной сценарий ---------------------------------------------------------


async def test_phone_in_second_message_is_not_lost(
    settings: Settings, repo: Repository
) -> None:
    """Клиент прислал имя и телефон двумя сообщениями подряд.

    Раньше второе сообщение отбрасывалось троттлингом, и контакт терялся —
    ровно то, ради чего бот существует.
    """
    settings.throttle_enabled = False
    settings.message_aggregation_delay = DELAY
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, notifier = _make_dispatcher(
        settings,
        repo,
        bot,
        [tool_response("save_qualified_lead", VALID_ARGS), text_response("Записал!")],
        aggregation_delay=DELAY,
    )

    await dispatcher.feed_update(bot, _update("Иван", update_id=1))
    await dispatcher.feed_update(bot, _update("+7 999 123-45-67", update_id=2))
    await dispatcher.feed_update(bot, _update("нужен RAV4 на август", update_id=3))
    await asyncio.sleep(DELAY * 6)

    # Три сообщения — один вызов модели вместо трёх.
    assert len(llm.calls) == 2  # основной проход + ответ после инструмента
    sent_to_llm = llm.calls[0][-1]["content"]
    assert "Иван" in sent_to_llm and "+7 999 123-45-67" in sent_to_llm
    assert await repo.count_leads() == 1
    assert len(notifier.sent) == 1
