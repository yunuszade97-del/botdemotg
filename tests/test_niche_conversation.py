"""Оркестратор в режиме витрины: ниша — свойство чата, а не процесса."""

from __future__ import annotations

import json

import pytest

from app.bot.services.conversation import ConversationService, TurnContext
from app.config import BASE_DIR
from app.core.niches import build_registry
from app.core.profile import load_profile
from app.core.prompts import NEED_NICHE_REPLY
from app.core.tools import SAVE_LEAD_TOOL_NAME
from app.db.crud import Repository
from tests.conftest import FakeLLM, FakeNotifier, text_response, tool_response
from tests.test_conversation import VALID_ARGS


def _registry():
    profiles = [load_profile(slug, base_dir=BASE_DIR) for slug in ("tours", "rent_car")]
    return build_registry(profiles)


def _service(settings, repo, responses):
    llm = FakeLLM(responses)
    notifier = FakeNotifier()
    service = ConversationService(
        settings=settings,
        repo=repo,
        llm=llm,
        notifier=notifier,  # type: ignore[arg-type]
        niches=_registry(),
    )
    return service, llm, notifier


async def test_prompt_comes_from_chat_niche(settings, repo: Repository) -> None:
    registry = _registry()
    tours = registry.get("tours")
    assert tours is not None

    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")

    service, llm, _ = _service(settings, repo, [text_response("Привет!")])
    ctx = TurnContext(chat_id=1)

    await service.handle_message(ctx, "Здравствуйте")

    assert llm.calls[0][0]["content"] == tours.system_prompt
    assert llm.calls[0][0]["content"] != service.system_prompt


async def test_different_chats_get_different_prompts(settings, repo: Repository) -> None:
    """Промпт компилировался в конструкторе один раз — тут проверяем, что кэш не подложил старый."""
    registry = _registry()
    tours = registry.get("tours")
    rent_car = registry.get("rent_car")
    assert tours is not None and rent_car is not None

    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")
    await repo.upsert_user(chat_id=2, tg_user_id=2, username=None, full_name=None)
    await repo.set_chat_profile(2, "rent_car")

    service, llm, _ = _service(
        settings, repo, [text_response("Ответ 1"), text_response("Ответ 2")]
    )

    await service.handle_message(TurnContext(chat_id=1), "Привет")
    await service.handle_message(TurnContext(chat_id=2), "Привет")

    assert llm.calls[0][0]["content"] == tours.system_prompt
    assert llm.calls[1][0]["content"] == rent_car.system_prompt
    assert llm.calls[0][0]["content"] != llm.calls[1][0]["content"]


async def test_manager_response_time_comes_from_niche(settings, repo: Repository) -> None:
    # tours и rent_car совпадают со сроком по умолчанию ("5 минут") — берём
    # real_estate, у которого срок другой, чтобы отличие было доказательным.
    real_estate = load_profile("real_estate", base_dir=BASE_DIR)
    registry = build_registry([real_estate])
    assert real_estate.response_time != settings.manager_response_time

    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "real_estate")

    llm = FakeLLM([tool_response(SAVE_LEAD_TOOL_NAME, VALID_ARGS), text_response("Спасибо!")])
    notifier = FakeNotifier()
    service = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier, niches=registry  # type: ignore[arg-type]
    )

    await service.handle_message(TurnContext(chat_id=1), "Хочу заявку")

    # Второй запрос к модели содержит tool-ответ с payload сохранённого лида.
    tool_message = llm.calls[1][-1]
    payload = json.loads(tool_message["content"])
    assert payload["manager_response_time"] == real_estate.response_time


async def test_niche_not_selected_blocks_llm_and_history(settings, repo: Repository) -> None:
    service, llm, _ = _service(settings, repo, [])

    result = await service.handle_message(TurnContext(chat_id=1), "Здравствуйте")

    assert result.reply == NEED_NICHE_REPLY
    assert result.need_niche is True
    assert llm.calls == []
    assert await repo.get_history(1, limit=10, max_chars=10_000) == []
    assert await repo.count_llm_calls_today(1) == 0


async def test_showcase_disabled_keeps_single_mode_prompt(settings, repo: Repository) -> None:
    llm = FakeLLM([text_response("Ответ")])
    notifier = FakeNotifier()
    service = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier  # type: ignore[arg-type]
    )

    await service.handle_message(TurnContext(chat_id=1), "Здравствуйте")

    assert service.showcase_enabled is False
    assert llm.calls[0][0]["content"] == service.system_prompt


async def test_switch_niche_clears_history(settings, repo: Repository) -> None:
    service, llm, _ = _service(settings, repo, [])
    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")
    await repo.add_message(1, "user", "старое сообщение")

    niche = await service.switch_niche(1, "rent_car")

    assert niche is not None and niche.profile.slug == "rent_car"
    assert await repo.get_chat_profile(1) == "rent_car"
    assert await repo.get_history(1, limit=10, max_chars=10_000) == []


async def test_switch_niche_unknown_slug_returns_none_and_does_nothing(
    settings, repo: Repository
) -> None:
    service, llm, _ = _service(settings, repo, [])
    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")
    await repo.add_message(1, "user", "старое сообщение")

    niche = await service.switch_niche(1, "no-such-slug")

    assert niche is None
    assert await repo.get_chat_profile(1) == "tours"
    assert len(await repo.get_history(1, limit=10, max_chars=10_000)) == 1


class _RepoOrderSpy:
    """Обёртка вокруг Repository, фиксирующая порядок вызовов clear_history/set_chat_profile."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self.calls: list[str] = []

    async def clear_history(self, chat_id: int) -> int:
        self.calls.append("clear_history")
        return await self._repo.clear_history(chat_id)

    async def set_chat_profile(self, chat_id: int, slug: str | None) -> None:
        self.calls.append("set_chat_profile")
        return await self._repo.set_chat_profile(chat_id, slug)

    def __getattr__(self, name):
        return getattr(self._repo, name)


async def test_switch_niche_clears_history_before_setting_profile(
    settings, repo: Repository
) -> None:
    """Порядок — не косметика: сбой между операциями должен оставлять безопасное состояние."""
    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")

    spy = _RepoOrderSpy(repo)
    llm = FakeLLM([])
    notifier = FakeNotifier()
    service = ConversationService(
        settings=settings,
        repo=spy,  # type: ignore[arg-type]
        llm=llm,
        notifier=notifier,  # type: ignore[arg-type]
        niches=_registry(),
    )

    await service.switch_niche(1, "rent_car")

    assert spy.calls == ["clear_history", "set_chat_profile"]


async def test_lead_saves_chat_niche(settings, repo: Repository) -> None:
    registry = _registry()
    await repo.upsert_user(chat_id=1, tg_user_id=1, username=None, full_name=None)
    await repo.set_chat_profile(1, "tours")

    service, llm, _ = _service(
        settings,
        repo,
        [tool_response(SAVE_LEAD_TOOL_NAME, VALID_ARGS), text_response("Спасибо!")],
    )

    result = await service.handle_message(TurnContext(chat_id=1), "Хочу заявку")

    assert result.lead_id is not None
    lead = await repo.get_lead(result.lead_id)
    assert lead is not None
    assert lead.profile_slug == "tours"


async def test_lead_without_llm_saves_chat_niche(settings, repo: Repository) -> None:
    await repo.upsert_user(chat_id=1, tg_user_id=1, username="ivan", full_name="Иван")
    await repo.set_chat_profile(1, "rent_car")

    llm = FakeLLM([])
    notifier = FakeNotifier()
    service = ConversationService(
        settings=settings,
        repo=repo,
        llm=llm,
        notifier=notifier,  # type: ignore[arg-type]
        niches=_registry(),
    )

    result = await service.capture_contact_without_llm(
        TurnContext(chat_id=1, full_name="Иван"), phone="+79991234567", name="Иван"
    )

    assert result.lead_id is not None
    lead = await repo.get_lead(result.lead_id)
    assert lead is not None
    assert lead.profile_slug == "rent_car"
