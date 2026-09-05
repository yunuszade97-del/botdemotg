"""Точка входа: FastAPI + бот в режиме webhook или polling.

Переключение — переменной окружения USE_WEBHOOK:
  * False (по умолчанию) — long polling в фоновой задаче, удобно локально;
  * True  — Telegram шлёт апдейты на POST {WEBHOOK_BASE_URL}{WEBHOOK_PATH}.

HTTP-сервер поднимается в обоих режимах: /healthz нужен и polling-деплою
(проверки живости у Railway, Fly, Docker и т.п.).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, FastAPI, Header, Request, Response, status
from pydantic import ValidationError

from app.bot.factory import (
    build_services,
    create_bot,
    create_dispatcher,
    setup_bot_commands,
    verify_admin_chats,
)
from app.config import Settings, get_settings
from app.core.niches import build_registry
from app.db.crud import Repository
from app.db.database import Database
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# Держим ссылки на фоновые задачи обработки апдейтов (см. telegram_webhook).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    setup_logging(settings.log_level)
    settings.validate_runtime()

    # Всё, что открыто на старте, закрывается через стек, поэтому провал на
    # середине инициализации освобождает ресурсы так же, как штатная остановка.
    # Это не аккуратность ради аккуратности: aiosqlite обслуживает соединение
    # НЕ-демоническим потоком, и незакрытая база держит интерпретатор после
    # «Application startup failed. Exiting.» — процесс остаётся жив навсегда,
    # порт не слушает, а systemd/Docker/панель видят живой процесс и не
    # перезапускают его. Бот лежит молча, и в логах только одна строка.
    async with contextlib.AsyncExitStack() as stack:
        try:
            database = Database(settings.db_path)
            await database.connect()
            stack.push_async_callback(database.close)
            repo = Repository(database)

            niches = (
                build_registry(settings.showcase_niches) if settings.showcase_niches else None
            )

            bot = create_bot(settings)
            stack.push_async_callback(bot.session.close)

            llm, notifier, lead_webhook, conversation = build_services(
                settings=settings, bot=bot, repo=repo, niches=niches
            )
            stack.push_async_callback(llm.close)
            stack.push_async_callback(lead_webhook.close)

            dispatcher = create_dispatcher(
                settings=settings, repo=repo, conversation=conversation, niches=niches
            )
            stack.push_async_callback(dispatcher.workflow_data["aggregator"].close)

            app.state.settings = settings
            app.state.bot = bot
            app.state.dispatcher = dispatcher

            me = await bot.get_me()
            logger.info("Бот @%s запущен (id=%s)", me.username, me.id)
            await setup_bot_commands(bot, settings)

            # Недостижимый админ-чат — это молча теряемые лиды, поэтому проверяем
            # на старте, а не в момент первой заявки.
            with contextlib.suppress(Exception):
                await verify_admin_chats(bot, settings.admin_ids)

            # Лиды, о которых админ не узнал из-за прошлого сбоя, досылаем на старте.
            with contextlib.suppress(Exception):
                await notifier.flush_pending()
            if lead_webhook.enabled:
                logger.info("Выгрузка лидов включена: %s", settings.lead_webhook_url)
                with contextlib.suppress(Exception):
                    await lead_webhook.flush_pending()

            retention_task = asyncio.create_task(
                _retention_loop(repo, settings), name="retention-cleanup"
            )
            stack.push_async_callback(_cancel_task, retention_task)

            if settings.use_webhook:
                await bot.set_webhook(
                    url=settings.webhook_url,
                    secret_token=settings.effective_webhook_secret,
                    drop_pending_updates=settings.drop_pending_updates,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                )
                stack.push_async_callback(_drop_webhook, bot)
                logger.info("Webhook установлен: %s", settings.webhook_url)
            else:
                await bot.delete_webhook(drop_pending_updates=settings.drop_pending_updates)
                polling_task = asyncio.create_task(
                    dispatcher.start_polling(
                        bot,
                        allowed_updates=dispatcher.resolve_used_update_types(),
                        # Сигналы остаются за uvicorn. aiogram по умолчанию
                        # вешает свои обработчики SIGTERM/SIGINT через
                        # loop.add_signal_handler, а тот не добавляет обработчик,
                        # а ЗАМЕНЯЕТ поставленный uvicorn. Тогда SIGTERM
                        # останавливает только polling: uvicorn не узнаёт, что
                        # пора выключаться, и процесс живёт дальше с оглохшим
                        # ботом, пока супервизор не добьёт его по таймауту.
                        handle_signals=False,
                        # Сессию закрывает стек выше — иначе aiogram закроет её
                        # у ещё работающего приложения.
                        close_bot_session=False,
                    ),
                    name="telegram-polling",
                )
                # Упавший polling не роняет ни uvicorn, ни процесс: без этого
                # исключение оседает в задаче и не попадает даже в лог.
                polling_task.add_done_callback(_log_polling_exit)
                app.state.polling_task = polling_task
                stack.push_async_callback(_stop_polling, dispatcher, polling_task)
                logger.info("Режим long polling запущен")
        except Exception:
            # Без exc_info: трассировку следом печатает uvicorn, дублировать её
            # незачем — здесь нужна строка, объясняющая, куда смотреть.
            logger.error(
                "Старт не удался, процесс завершается. Частые причины: "
                "недействительный BOT_TOKEN, недоступный api.telegram.org, "
                "ошибка в .env или в профиле ниши."
            )
            raise

        yield

    logger.info("Остановка завершена")


async def _cancel_task(task: asyncio.Task) -> None:
    """Отменяет фоновую задачу и дожидается её конца, не роняя остановку."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - о падении задачи уже сообщил её колбэк
        logger.debug("Задача %s завершилась с ошибкой", task.get_name(), exc_info=True)


async def _stop_polling(dispatcher: Dispatcher, task: asyncio.Task) -> None:
    # Штатная остановка — не отказ, поэтому колбэк снимаем до неё.
    task.remove_done_callback(_log_polling_exit)
    if not task.done():
        # RuntimeError — если polling уже остановился сам; на остановке это
        # не новость, а шум.
        with contextlib.suppress(RuntimeError):
            await dispatcher.stop_polling()
    await _cancel_task(task)


async def _drop_webhook(bot: Bot) -> None:
    with contextlib.suppress(Exception):
        await bot.delete_webhook()


def _log_polling_exit(task: asyncio.Task) -> None:
    """Polling, завершившийся сам по себе, — это оглохший бот.

    Процесс при этом жив, порт слушает, healthcheck зелёный. Без явной записи
    в лог такой отказ выглядит как «бот молчит без причины».
    """
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("Polling остановлен с ошибкой: %s", exc, exc_info=exc)
    else:
        logger.error("Polling остановлен — бот больше не получает апдейты")


async def _retention_loop(repo: Repository, settings: Settings) -> None:
    """Периодически удаляет данные старше срока хранения.

    Первый проход — сразу на старте: если бот стоял выключенным, накопившееся
    должно уйти при первом же запуске, а не через сутки работы.
    """
    interval = settings.retention_cleanup_hours * 3600
    while True:
        try:
            messages = await repo.purge_old_messages(settings.retention_days_messages)
            leads = await repo.purge_old_leads(settings.retention_days_leads)
            await repo.purge_old_usage()
            if messages or leads:
                logger.info(
                    "Retention: удалено сообщений=%s, заявок=%s", messages, leads
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновая уборка не должна ронять бота
            logger.exception("Ошибка очистки устаревших данных")
        await asyncio.sleep(interval)


router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict[str, str]:
    settings: Settings = get_settings()
    # Живой процесс не равен работающему боту: polling может умереть, оставив
    # HTTP-сервер отвечать «ok». Такой ответ — ложь, из-за которой ни панель,
    # ни внешний мониторинг не видят, что бот перестал получать апдейты.
    polling: asyncio.Task | None = getattr(request.app.state, "polling_task", None)
    polling_alive = polling is None or not polling.done()
    if not polling_alive:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if polling_alive else "degraded",
        "mode": "webhook" if settings.use_webhook else "polling",
        "model": settings.llm_model,
    }


async def telegram_webhook(
    request: Request,
    response: Response,
    secret_token: str | None = Header(default=None, alias=SECRET_HEADER),
) -> Response:
    # Конфигурацию берём из кэша, а не из app.state: проверка подлинности
    # не должна зависеть от того, успел ли отработать lifespan. Без неё
    # эндпойнт открыт всему интернету — любой сможет прислать поддельный
    # апдейт от имени Telegram.
    settings: Settings = get_settings()
    expected = settings.effective_webhook_secret
    if not secret_token or not hmac.compare_digest(secret_token, expected):
        logger.warning("Отклонён вебхук с неверным secret_token")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    bot = getattr(request.app.state, "bot", None)
    dispatcher = getattr(request.app.state, "dispatcher", None)
    if bot is None or dispatcher is None:
        # Приложение ещё поднимается: 503 заставит Telegram повторить апдейт.
        logger.warning("Вебхук получен до готовности приложения")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        payload = await request.json()
    except ValueError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        update = Update.model_validate(payload, context={"bot": bot})
    except ValidationError:
        logger.warning("Вебхук с некорректной структурой апдейта")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # Обработка — в фоне: ответ LLM занимает секунды, а Telegram при
    # медленном ответе начнёт ретраить тот же апдейт.
    task = asyncio.create_task(dispatcher.feed_update(bot, update))
    # Сильная ссылка обязательна: asyncio хранит задачи только слабо,
    # и сборщик мусора способен убить обработку апдейта на середине.
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(_log_update_failure)

    response.status_code = status.HTTP_200_OK
    return response


def _log_update_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("Ошибка обработки вебхука: %s", exc, exc_info=exc)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Lead Generation Telegram Bot",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    app.add_api_route(
        settings.webhook_path, telegram_webhook, methods=["POST"], include_in_schema=False
    )
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
