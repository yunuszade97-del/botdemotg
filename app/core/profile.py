"""Профиль ниши: витрина бизнеса, подставляемая в промпт без правок кода.

Один профиль — один файл `profiles/<name>.toml`. Переключение ниши это
`PROFILE=<name>` в `.env` и перезапуск: демонстрировать бота автопрокату,
агентству недвижимости и экскурсионке можно с одной кодовой базы.

Формат — TOML, потому что его читает стандартный `tomllib` (Python 3.11+),
без зависимостей и без самописного парсера. Прайс и FAQ живут отдельным
markdown-файлом: его правит менеджер клиента, и ошибка экранирования там
не должна ронять загрузку конфига.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROFILES_DIRNAME = "profiles"


class ProfileError(ValueError):
    """Профиль не найден или заполнен некорректно."""


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    """Всё, что отличает одну нишу от другой. Код при смене ниши не меняется."""

    slug: str
    name: str
    business: str
    city: str
    working_hours: str
    response_time: str
    knowledge_file: Path
    welcome: str
    # Что обязательно выяснить именно в этой нише. Пустой список допустим:
    # промпт тогда работает по общей схеме «имя → запрос → сроки → контакт».
    qualify: tuple[str, ...]
    # Примеры реплик клиента для демонстрации и ручного прогона.
    demo_script: tuple[str, ...]
    # Человекочитаемая метка для кнопки выбора и карточки лида в режиме
    # витрины: `business` — это фраза для промпта («аренда квартир и
    # апартаментов»), а не то, что помещается на кнопку.
    label: str = ""


def profiles_dir(base_dir: Path) -> Path:
    return base_dir / PROFILES_DIRNAME


def available_profiles(base_dir: Path) -> list[str]:
    """Имена профилей, пригодные для `PROFILE=`."""
    directory = profiles_dir(base_dir)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.toml"))


def _require_str(data: dict[str, object], key: str, slug: str, *, required: bool) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ProfileError(f"профиль {slug!r}: поле {key!r} должно быть строкой")
    value = value.strip()
    if required and not value:
        raise ProfileError(f"профиль {slug!r}: поле {key!r} обязательно")
    return value


def _string_list(data: dict[str, object], key: str, slug: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if isinstance(value, str):
        raise ProfileError(f"профиль {slug!r}: {key!r} — список строк, а не строка")
    if not isinstance(value, list):
        raise ProfileError(f"профиль {slug!r}: {key!r} должно быть списком строк")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProfileError(f"профиль {slug!r}: в {key!r} попал не-текст: {item!r}")
        item = item.strip()
        if item:
            items.append(item)
    return tuple(items)


def load_profile(slug: str, *, base_dir: Path) -> BusinessProfile:
    """Читает `profiles/<slug>.toml`.

    Любая проблема — исключение, а не тихий дефолт: бот, молча уехавший на
    чужой прайс, хуже бота, который не стартовал.
    """
    slug = slug.strip()
    if not slug:
        raise ProfileError("имя профиля пустое")
    if "/" in slug or "\\" in slug or slug.startswith("."):
        raise ProfileError(f"недопустимое имя профиля: {slug!r}")

    path = profiles_dir(base_dir) / f"{slug}.toml"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        known = ", ".join(available_profiles(base_dir)) or "ни одного"
        raise ProfileError(
            f"профиль {slug!r} не найден ({path}). Доступны: {known}"
        ) from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ProfileError(f"профиль {slug!r}: файл не читается как TOML — {exc}") from exc

    knowledge = _require_str(data, "knowledge_file", slug, required=True)
    knowledge_path = Path(knowledge)
    if not knowledge_path.is_absolute():
        knowledge_path = base_dir / knowledge_path

    name = _require_str(data, "name", slug, required=True)

    return BusinessProfile(
        slug=slug,
        name=name,
        business=_require_str(data, "business", slug, required=True),
        city=_require_str(data, "city", slug, required=False),
        working_hours=_require_str(data, "working_hours", slug, required=False)
        or "ежедневно 09:00–21:00",
        response_time=_require_str(data, "response_time", slug, required=False)
        or "5 минут",
        knowledge_file=knowledge_path,
        welcome=_require_str(data, "welcome", slug, required=False),
        qualify=_string_list(data, "qualify", slug),
        demo_script=_string_list(data, "demo_script", slug),
        label=_require_str(data, "label", slug, required=False) or name,
    )
