"""Жизненный цикл процесса: провал старта, владение сигналами, честность /healthz.

Отказы этого слоя не видит ни один тест сервисов и хэндлеров: бот перестаёт
быть ботом, оставаясь живым процессом с зелёным healthcheck'ом.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import User
from fastapi import FastAPI, Response

from app.config import get_settings


class _StartupSession(BaseSession):
    """Отвечает на вызовы Bot API без сети; getMe можно заставить упасть."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        super().__init__()
        self._fail = fail

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout=None) -> Any:
        if self._fail is not None:
            raise self._fail
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Bot", username="test_bot")
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""


def _sqlite_worker_threads() -> int:
    """Рабочие потоки aiosqlite. Они не демонические — см. тест ниже."""
    return sum("_connection_worker_thread" in t.name for t in threading.enumerate())


@pytest.fixture
def main_module(monkeypatch, tmp_path: Path):
    """app.main с настроенным окружением.

    Импорт — внутри фикстуры: модуль собирает FastAPI прямо при импорте, и без
    заполненных переменных Settings падает ещё на сборе тестов.
    """
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "777")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.sqlite3"))
    get_settings.cache_clear()

    import app.main as module

    yield module
    get_settings.cache_clear()


def _patch_bot(monkeypatch, main_module, session: _StartupSession) -> None:
    monkeypatch.setattr(
        main_module,
        "create_bot",
        lambda settings: Bot(token=settings.bot_token, session=session),
    )


async def test_failed_startup_releases_the_database(main_module, monkeypatch) -> None:
    """Провал старта обязан закрыть SQLite, иначе процесс не может умереть.

    aiosqlite обслуживает соединение НЕ-демоническим потоком. Незакрытая база
    держит интерпретатор после «Application startup failed. Exiting.»: процесс
    остаётся живым навсегда, порт не слушает, а systemd/Docker/панель видят
    живой процесс и перезапуск не запускают. Снаружи это «бот лег и не
    поднимается сам», причём в логах — одна строка без объяснения.
    """
    _patch_bot(
        monkeypatch,
        main_module,
        _StartupSession(
            fail=TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
        ),
    )
    before = _sqlite_worker_threads()

    with pytest.raises(TelegramUnauthorizedError):
        async with main_module.lifespan(FastAPI()):
            pytest.fail("старт не должен был дойти до готовности")

    for _ in range(50):  # поток завершается сразу после close(), но не мгновенно
        if _sqlite_worker_threads() == before:
            break
        await asyncio.sleep(0.01)
    assert _sqlite_worker_threads() == before


async def test_polling_leaves_signals_to_uvicorn(main_module, monkeypatch) -> None:
    """aiogram не должен перехватывать SIGTERM у uvicorn.

    loop.add_signal_handler не добавляет обработчик, а ЗАМЕНЯЕТ поставленный
    ранее. С handle_signals=True сигнал останавливает только polling: uvicorn
    не узнаёт, что пора выключаться, и процесс продолжает работать с оглохшим
    ботом — отвечая при этом «ok» на /healthz.
    """
    captured: dict[str, Any] = {}

    async def fake_start_polling(self, *bots: Bot, **kwargs: Any) -> None:
        captured.update(kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    _patch_bot(monkeypatch, main_module, _StartupSession())

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # даём задаче polling начаться
        assert captured["handle_signals"] is False
        # Сессию бота закрывает lifespan; иначе aiogram закроет её у ещё
        # работающего приложения.
        assert captured["close_bot_session"] is False


async def test_shutdown_stops_polling_task(main_module, monkeypatch) -> None:
    """После остановки не должно оставаться работающей задачи polling."""

    async def fake_start_polling(self, *bots: Bot, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    _patch_bot(monkeypatch, main_module, _StartupSession())

    app = FastAPI()
    async with main_module.lifespan(app):
        task = app.state.polling_task
        assert not task.done()

    assert task.done()


async def test_healthz_degrades_when_polling_is_dead(main_module) -> None:
    """«ok» при мёртвом polling — ложь, из-за которой сбой не видит мониторинг.

    Процесс жив и порт слушает, поэтому ни Docker HEALTHCHECK, ни внешний
    аптайм-монитор не заметят, что бот перестал получать апдейты.
    """

    async def boom() -> None:
        raise RuntimeError("polling упал")

    task = asyncio.create_task(boom())
    with contextlib.suppress(RuntimeError):
        await task

    app = FastAPI()
    app.state.polling_task = task
    response = Response()

    payload = await main_module.healthz(SimpleNamespace(app=app), response)

    assert payload["status"] == "degraded"
    assert response.status_code == 503


async def test_healthz_is_ok_while_polling_runs(main_module) -> None:
    async def idle() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(idle())
    app = FastAPI()
    app.state.polling_task = task
    response = Response()
    try:
        payload = await main_module.healthz(SimpleNamespace(app=app), response)
    finally:
        task.cancel()

    assert payload["status"] == "ok"
    assert payload["mode"] == "polling"
