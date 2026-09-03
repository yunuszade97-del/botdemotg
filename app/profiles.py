"""CLI по нишам: `python -m app.profiles`.

Загрузчик одного профиля живёт в `app/core/profile.py`; здесь — только показ
того, что получится. Нужен, чтобы перед демонстрацией клиенту увидеть готовый
системный промпт целиком, а не догадываться, что попало в модель.

    python -m app.profiles              # список доступных ниш
    python -m app.profiles show tours   # карточка профиля и сценарий демо
    python -m app.profiles prompt tours # системный промпт как его увидит модель
"""

from __future__ import annotations

import sys

from app.config import BASE_DIR
from app.core.profile import BusinessProfile, ProfileError, available_profiles, load_profile
from app.core.prompts import build_system_prompt


def _knowledge(profile: BusinessProfile) -> str:
    try:
        return profile.knowledge_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _system_prompt(profile: BusinessProfile) -> str:
    return build_system_prompt(
        company_name=profile.name,
        company_business=profile.business,
        company_city=profile.city,
        working_hours=profile.working_hours,
        knowledge_base=_knowledge(profile),
        qualify_fields=profile.qualify,
    )


def cmd_list() -> int:
    names = available_profiles(BASE_DIR)
    if not names:
        print(f"В {BASE_DIR / 'profiles'} нет ни одного *.toml")
        return 1
    print("Доступные ниши (PROFILE=<имя> в .env):\n")
    for name in names:
        try:
            profile = load_profile(name, base_dir=BASE_DIR)
        except ProfileError as exc:
            print(f"  {name:<14} ✗ {exc}")
            continue
        print(f"  {name:<14} {profile.name} — {profile.business}")
    print("\nПодробнее: python -m app.profiles show <имя>")
    return 0


def cmd_show(name: str) -> int:
    profile = load_profile(name, base_dir=BASE_DIR)
    knowledge = _knowledge(profile)
    print(f"Профиль:      {profile.slug}")
    print(f"Компания:     {profile.name}")
    print(f"Направление:  {profile.business}")
    print(f"Город:        {profile.city or '—'}")
    print(f"Часы работы:  {profile.working_hours}")
    print(f"Ответ за:     {profile.response_time}")
    status = f"{len(knowledge)} символов" if knowledge else "ПУСТО — бот не назовёт цены"
    print(f"База знаний:  {profile.knowledge_file} ({status})")
    if profile.qualify:
        print("\nВыясняет в диалоге:")
        for item in profile.qualify:
            print(f"  - {item}")
    if profile.welcome:
        print("\nПриветствие:")
        for line in profile.welcome.splitlines():
            print(f"  {line}")
    if profile.demo_script:
        print("\nСценарий демо (отправьте боту по одному сообщению):")
        for step, line in enumerate(profile.demo_script, start=1):
            print(f"  {step}. {line}")
    print(f"\nСистемный промпт: python -m app.profiles prompt {profile.slug}")
    return 0


def cmd_prompt(name: str) -> int:
    print(_system_prompt(load_profile(name, base_dir=BASE_DIR)))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"list", "-l", "--list"}:
        return cmd_list()
    command, *rest = args
    if command in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    if command not in {"show", "prompt"} or not rest:
        print(__doc__)
        return 2
    try:
        return cmd_show(rest[0]) if command == "show" else cmd_prompt(rest[0])
    except ProfileError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - точка входа
    raise SystemExit(main())
