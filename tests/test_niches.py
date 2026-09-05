"""Реестр скомпилированных ниш режима витрины.

Промпт компилируется один раз при сборке реестра — чтение базы знаний
синхронное, и ленивая компиляция утащила бы блокирующий I/O в event loop
на каждое сообщение чата.
"""

from __future__ import annotations

from app.config import BASE_DIR
from app.core.niches import build_registry, compile_system_prompt
from app.core.profile import load_profile
from app.profiles import cmd_prompt


def _profiles(*slugs: str):
    return [load_profile(slug, base_dir=BASE_DIR) for slug in slugs]


def test_prompt_is_compiled_once_at_build_time(monkeypatch) -> None:
    profile = load_profile("tours", base_dir=BASE_DIR)
    reads = 0
    original_read_text = type(profile.knowledge_file).read_text

    def counting_read_text(self, *args, **kwargs):
        nonlocal reads
        reads += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(profile.knowledge_file), "read_text", counting_read_text)

    registry = build_registry([profile])
    assert reads == 1

    # Повторные обращения к уже собранному реестру не должны снова читать файл.
    registry.get("tours")
    registry.get("tours")
    assert reads == 1


def test_unknown_slug_returns_none() -> None:
    registry = build_registry(_profiles("tours"))

    assert registry.get("no-such-niche") is None
    assert registry.get(None) is None


def test_different_niches_have_different_prompts() -> None:
    registry = build_registry(_profiles("tours", "rent_car"))

    tours = registry.get("tours")
    rent_car = registry.get("rent_car")
    assert tours is not None and rent_car is not None
    assert tours.system_prompt != rent_car.system_prompt


def test_registry_enabled_reflects_content() -> None:
    assert build_registry([]).enabled is False
    assert build_registry(_profiles("tours")).enabled is True


def test_registry_iterates_and_counts() -> None:
    registry = build_registry(_profiles("tours", "rent_car"))

    assert len(registry) == 2
    assert {niche.profile.slug for niche in registry} == {"tours", "rent_car"}


def test_registry_prompt_matches_cli_output(capsys) -> None:
    """`python -m app.profiles prompt` обещает «вот что увидит модель» — сверяем буквально."""
    profile = load_profile("tours", base_dir=BASE_DIR)
    registry = build_registry([profile])

    cmd_prompt("tours")
    printed = capsys.readouterr().out

    assert registry.get("tours").system_prompt == printed[:-1]  # print() добавил \n
    assert registry.get("tours").system_prompt == compile_system_prompt(profile)
