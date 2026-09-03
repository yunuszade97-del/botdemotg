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

    database = Database(settings.db_path)
    await database.connect()
    repo = Repository(database)

    bot = create_bot(settings)
    llm, notifier, lead_webhook, conversation = build_services(
        settings=settings, bot=bot, repo=repo
    )
    dispatcher = create_dispatcher(settings=settings, repo=repo, conversation=conversation)

    app.state.settings = settings
    app.state.bot = bot
    app.state.dispatcher = dispatcher

    me = await bot.get_me()
    logger.info("Бот @%s запущен (id=%s)", me.username, me.id)
    await setup_bot_commands(bot)

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

    polling_task: asyncio.Task | None = None
    if settings.use_webhook:
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.effective_webhook_secret,
            drop_pending_updates=settings.drop_pending_updates,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        logger.info("Webhook установлен: %s", settings.webhook_url)
    else:
        await bot.delete_webhook(drop_pending_updates=settings.drop_pending_updates)
        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types()
            ),
            name="telegram-polling",
        )
        logger.info("Режим long polling запущен")

    try:
        yield
    finally:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        if polling_task is not None:
            await dispatcher.stop_polling()
            polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling_task
        if settings.use_webhook:
            with contextlib.suppress(Exception):
                await bot.delete_webhook()
        await dispatcher.workflow_data["aggregator"].close()
        await llm.close()
        await lead_webhook.close()
        await bot.session.close()
        await database.close()
        logger.info("Остановка завершена")


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
async def healthz() -> dict[str, str]:
    settings: Settings = get_settings()
    return {
        "status": "ok",
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
