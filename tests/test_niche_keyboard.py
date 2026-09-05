"""Inline-клавиатура выбора направления в режиме витрины."""

from __future__ import annotations

from app.bot.keyboards import niche_keyboard, parse_niche_callback
from app.config import BASE_DIR
from app.core.niches import build_registry
from app.core.profile import load_profile


def _registry(*slugs: str):
    return build_registry([load_profile(slug, base_dir=BASE_DIR) for slug in slugs])


def test_keyboard_has_one_button_per_niche() -> None:
    registry = _registry("tours", "rent_car")

    markup = niche_keyboard(registry)

    rows = markup.inline_keyboard
    assert len(rows) == 2
    buttons = [row[0] for row in rows]
    labels = {button.text for button in buttons}
    assert labels == {n.profile.label for n in registry}
    for button in buttons:
        assert button.callback_data.startswith("niche:")


def test_parse_niche_callback_reads_slug() -> None:
    assert parse_niche_callback("niche:tours") == "tours"


def test_parse_niche_callback_rejects_foreign_data() -> None:
    assert parse_niche_callback("something:else") is None
    assert parse_niche_callback("contact") is None
