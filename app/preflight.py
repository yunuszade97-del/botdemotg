"""Проверка готовности к запуску: `python -m app.preflight`.

Отвечает на единственный вопрос — можно ли включать бота прямо сейчас.
Каждая проверка ловит отказ, который иначе всплывёт в проде и будет стоить
живых заявок: непринятый токен, недостижимый админ-чат, модель без
поддержки tool calling, недоступная для записи база.

Код возврата 0 — можно запускать, 1 — нет.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.bot.factory import create_bot
from app.bot.keyboards import NICHE_CALLBACK_PREFIX
from app.config import BASE_DIR, Settings, get_settings
from app.core.niches import read_knowledge
from app.core.profile import available_profiles
from app.core.llm_client import LLMClient, LLMError
from app.core.tools import SAVE_LEAD_TOOL_NAME, TOOLS
from app.db.crud import Repository
from app.db.database import Database

OK = "  OK  "
WARN = " WARN "
FAIL = " FAIL "


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == FAIL


async def check_config(settings: Settings) -> CheckResult:
    try:
        settings.validate_runtime()
    except ValueError as exc:
        return CheckResult("Конфигурация", FAIL, str(exc))
    mode = "webhook" if settings.use_webhook else "polling"
    return CheckResult(
        "Конфигурация", OK, f"режим {mode}, админов: {len(settings.admin_ids)}"
    )


async def check_database(settings: Settings) -> CheckResult:
    db = Database(settings.db_path)
    try:
        await db.connect()
        repo = Repository(db)
        # Полный цикл записи и чтения: права на файл проверяются только так.
        await repo.add_message(0, "user", "preflight")
        await repo.clear_history(0)
    except Exception as exc:  # noqa: BLE001 - показываем пользователю причину
        return CheckResult("База данных", FAIL, f"{settings.db_path}: {exc}")
    finally:
        await db.close()
    return CheckResult("База данных", OK, str(settings.db_path))


async def check_telegram(settings: Settings) -> tuple[CheckResult, CheckResult]:
    bot = create_bot(settings)
    try:
        try:
            me = await bot.get_me()
        except Exception as exc:  # noqa: BLE001
            token = CheckResult("Telegram: токен", FAIL, f"{exc}")
            return token, CheckResult("Telegram: админ-чаты", FAIL, "проверка пропущена")

        token = CheckResult("Telegram: токен", OK, f"@{me.username} (id={me.id})")

        unreachable: list[int] = []
        for admin_id in settings.admin_ids:
            try:
                await bot.get_chat(admin_id)
            except Exception:  # noqa: BLE001 - причина не важна, важен факт
                unreachable.append(admin_id)

        if unreachable:
            admins = CheckResult(
                "Telegram: админ-чаты",
                FAIL,
                f"недоступны: {unreachable}. Откройте бота с этих аккаунтов "
                f"и нажмите /start — иначе лиды туда не дойдут",
            )
        else:
            admins = CheckResult(
                "Telegram: админ-чаты", OK, f"доступны: {settings.admin_ids}"
            )
        return token, admins
    finally:
        await bot.session.close()


async def check_llm(settings: Settings) -> tuple[CheckResult, CheckResult]:
    """Проверяет ключ и — отдельно — поддержку моделью tool calling.

    Модель без tool calling отвечает на вопросы, но не сохраняет ни одной
    заявки. Бот при этом выглядит полностью рабочим, поэтому проверка
    вынесена в отдельный пункт.
    """
    llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.0,
        max_tokens=200,
        timeout=settings.llm_timeout,
        max_retries=1,
    )
    probe = [
        {
            "role": "system",
            "content": (
                "Ты менеджер по продажам. Клиент дал все данные — "
                "вызови save_qualified_lead."
            ),
        },
        {
            "role": "user",
            "content": (
                "Меня зовут Иван, телефон +79991234567. "
                "Нужна машина на 12–19 августа, желательно кроссовер."
            ),
        },
    ]
    try:
        response = await llm.complete(probe, tools=TOOLS)
    except LLMError as exc:
        return (
            CheckResult("LLM: доступ", FAIL, f"{settings.llm_model}: {exc}"),
            CheckResult("LLM: tool calling", FAIL, "проверка пропущена"),
        )
    except Exception as exc:  # noqa: BLE001
        return (
            CheckResult("LLM: доступ", FAIL, f"{settings.llm_model}: {exc}"),
            CheckResult("LLM: tool calling", FAIL, "проверка пропущена"),
        )
    finally:
        await llm.close()

    access = CheckResult("LLM: доступ", OK, f"{settings.llm_model} отвечает")

    names = [call.name for call in response.tool_calls]
    if SAVE_LEAD_TOOL_NAME in names:
        args = next(c.arguments for c in response.tool_calls if c.name == SAVE_LEAD_TOOL_NAME)
        try:
            parsed = json.loads(args)
            preview = parsed.get("client_name", "?")
        except json.JSONDecodeError:
            preview = "аргументы не JSON"
        tools = CheckResult("LLM: tool calling", OK, f"инструмент вызван (имя: {preview})")
    else:
        tools = CheckResult(
            "LLM: tool calling",
            WARN,
            "модель не вызвала save_qualified_lead на явном примере — "
            "заявки могут не сохраняться, проверьте поддержку tools у модели",
        )
    return access, tools


async def check_lead_webhook(settings: Settings) -> CheckResult:
    """Дёргает приёмник тестовым лидом: настройка без проверки бесполезна."""
    if not settings.lead_webhook_url:
        return CheckResult(
            "Выгрузка лидов", WARN, "LEAD_WEBHOOK_URL не задан — только Telegram"
        )

    import httpx

    from app.bot.services.lead_webhook import SIGNATURE_HEADER, sign

    body = json.dumps(
        {
            "event": "lead.test",
            "company": settings.company_name,
            "lead": {"id": 0, "client_name": "Проверка связи"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if settings.lead_webhook_secret:
        headers[SIGNATURE_HEADER] = sign(body, settings.lead_webhook_secret)

    try:
        async with httpx.AsyncClient(timeout=settings.lead_webhook_timeout) as client:
            response = await client.post(
                settings.lead_webhook_url, content=body, headers=headers
            )
    except Exception as exc:  # noqa: BLE001 - показываем причину пользователю
        return CheckResult("Выгрузка лидов", FAIL, f"приёмник недоступен: {exc}")

    if response.is_success:
        return CheckResult(
            "Выгрузка лидов", OK, f"приёмник ответил HTTP {response.status_code}"
        )
    return CheckResult(
        "Выгрузка лидов",
        FAIL,
        f"приёмник ответил HTTP {response.status_code} — проверьте LEAD_WEBHOOK_URL",
    )


async def check_profile(settings: Settings) -> CheckResult:
    """Профиль ниши. Тихо уехавший на чужой прайс бот выглядит рабочим."""
    if settings.showcase_enabled:
        lines = []
        empty_qualify: list[str] = []
        oversized_callback: list[str] = []
        for niche in settings.showcase_niches:
            lines.append(
                f"{niche.slug}: {niche.name} — {niche.business}, "
                f"{len(niche.qualify)} вопросов квалификации"
            )
            if not niche.qualify:
                empty_qualify.append(niche.slug)
            callback_data = f"{NICHE_CALLBACK_PREFIX}{niche.slug}"
            if len(callback_data.encode()) > 64:
                oversized_callback.append(niche.slug)
        if oversized_callback:
            return CheckResult(
                "Профиль ниши",
                FAIL,
                f"slug слишком длинный для кнопки Telegram (лимит 64 байта "
                f"callback_data): {', '.join(oversized_callback)}",
            )
        detail = "; ".join(lines)
        if empty_qualify:
            return CheckResult(
                "Профиль ниши",
                WARN,
                f"{detail} — список qualify пуст у: {', '.join(empty_qualify)}, "
                f"по этим нишам квалификация пойдёт по общей схеме",
            )
        return CheckResult("Профиль ниши", OK, detail)

    if not settings.profile:
        known = ", ".join(available_profiles(BASE_DIR)) or "нет"
        return CheckResult(
            "Профиль ниши",
            WARN,
            f"PROFILE не задан, поля берутся из COMPANY_* — "
            f"компания «{settings.company_name}». Готовые ниши: {known}",
        )
    profile = settings.business_profile
    if profile is None:  # pragma: no cover - check_config уже упал бы
        return CheckResult("Профиль ниши", FAIL, f"{settings.profile!r} не загрузился")
    if not profile.qualify:
        return CheckResult(
            "Профиль ниши",
            WARN,
            f"{profile.slug}: {profile.name} — но список qualify пуст, "
            f"бот будет квалифицировать по общей схеме",
        )
    return CheckResult(
        "Профиль ниши",
        OK,
        f"{profile.slug}: {profile.name} — {profile.business}, "
        f"{len(profile.qualify)} вопросов квалификации",
    )


def _knowledge_verdict(knowledge: str) -> str:
    """'missing' | 'template' | 'ok' — общая эвристика для одиночного режима и витрины.

    Шаблон из репозитория состоит из заголовков и html-комментариев: бот на нём
    формально работает, но клиенту рассказать нечего.
    """
    if not knowledge:
        return "missing"
    meaningful = [
        line
        for line in knowledge.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ">", "<!--"))
    ]
    if len(meaningful) < 5:
        return "template"
    return "ok"


async def check_knowledge(settings: Settings) -> CheckResult:
    if settings.showcase_enabled:
        bad = []
        for niche in settings.showcase_niches:
            verdict = _knowledge_verdict(read_knowledge(niche))
            if verdict != "ok":
                bad.append(f"{niche.slug} ({verdict})")
        if bad:
            return CheckResult(
                "База знаний",
                WARN,
                f"впишите прайс, условия и FAQ: {', '.join(bad)}",
            )
        return CheckResult(
            "База знаний", OK, f"все {len(settings.showcase_niches)} ниш(и) заполнены"
        )

    knowledge = settings.knowledge_base()
    verdict = _knowledge_verdict(knowledge)
    if verdict == "missing":
        return CheckResult(
            "База знаний",
            WARN,
            f"{settings.knowledge_file} пуст или не найден — "
            f"бот не сможет называть цены и условия",
        )
    if verdict == "template":
        return CheckResult(
            "База знаний",
            WARN,
            f"{settings.knowledge_file} выглядит незаполненным шаблоном — "
            f"впишите прайс, условия и FAQ",
        )
    return CheckResult(
        "База знаний", OK, f"{settings.knowledge_file}, {len(knowledge)} символов"
    )


async def run_checks() -> list[CheckResult]:
    settings = get_settings()
    results = [await check_config(settings)]
    if results[0].failed:
        return results

    results.append(await check_database(settings))
    results.append(await check_profile(settings))
    results.append(await check_knowledge(settings))
    results.append(await check_lead_webhook(settings))

    token, admins = await check_telegram(settings)
    results.extend([token, admins])

    access, tools = await check_llm(settings)
    results.extend([access, tools])
    return results


def _render(results: list[CheckResult]) -> None:
    width = max(len(r.name) for r in results)
    print("\nПроверка готовности к запуску\n" + "─" * (width + 40))
    for result in results:
        print(f"[{result.status}] {result.name.ljust(width)}  {result.detail}")
    print("─" * (width + 40))


async def main() -> int:
    try:
        results = await run_checks()
    except Exception as exc:  # noqa: BLE001 - конфиг может не собраться вовсе
        print(f"[{FAIL}] Не удалось загрузить конфигурацию: {exc}")
        print("Проверьте .env — за образец возьмите .env.example")
        return 1

    _render(results)

    failed = [r for r in results if r.failed]
    warned = [r for r in results if r.status == WARN]

    if failed:
        print(f"\n❌ Запускать нельзя: {len(failed)} проверок не пройдено.\n")
        return 1
    if warned:
        print(f"\n⚠️  Можно запускать, но {len(warned)} замечани(е/я) стоит закрыть.\n")
        return 0
    print("\n✅ Всё готово — можно запускать: python -m app.main\n")
    return 0


def cli() -> None:
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    cli()
