"""Реестр скомпилированных ниш для режима витрины.

Один бот показывает клиенту несколько направлений на выбор: ниша перестаёт
быть свойством процесса (`PROFILE=` в `.env`) и становится свойством чата.
Здесь — только сборка: чтение базы знаний и компиляция системного промпта
для каждого профиля. Модуль не знает про Settings и про aiogram, чтобы
оставаться пригодным и для CLI, и для рантайма.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from app.core.profile import BusinessProfile
from app.core.prompts import build_system_prompt


@dataclass(frozen=True, slots=True)
class Niche:
    profile: BusinessProfile
    system_prompt: str


def read_knowledge(profile: BusinessProfile) -> str:
    try:
        return profile.knowledge_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def compile_system_prompt(profile: BusinessProfile) -> str:
    return build_system_prompt(
        company_name=profile.name,
        company_business=profile.business,
        company_city=profile.city,
        working_hours=profile.working_hours,
        knowledge_base=read_knowledge(profile),
        qualify_fields=profile.qualify,
    )


class NicheRegistry:
    """Готовые ниши по slug. Промпты уже скомпилированы — доступ не блокирует event loop."""

    def __init__(self, niches: Sequence[Niche]) -> None:
        self._by_slug = {niche.profile.slug: niche for niche in niches}

    def get(self, slug: str | None) -> Niche | None:
        """Неизвестный slug — не ошибка, а деградация в «ниша не выбрана»."""
        if slug is None:
            return None
        return self._by_slug.get(slug)

    @property
    def enabled(self) -> bool:
        return bool(self._by_slug)

    def __iter__(self) -> Iterator[Niche]:
        return iter(self._by_slug.values())

    def __len__(self) -> int:
        return len(self._by_slug)


def build_registry(profiles: Sequence[BusinessProfile]) -> NicheRegistry:
    """Компилирует промпты один раз, при старте — не лениво при первом обращении.

    Чтение базы знаний синхронное; ленивая компиляция утащила бы блокирующий
    I/O в event loop на каждое сообщение чата.
    """
    return NicheRegistry(
        [Niche(profile=profile, system_prompt=compile_system_prompt(profile)) for profile in profiles]
    )
