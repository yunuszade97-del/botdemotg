"""Хранение выбранной ниши: колонка `profile_slug` у чата и у лида.

Смена ниши должна переживать переподключение к БД и не задеваться соседними
операциями — особенно `upsert_user`, который выполняется на каждый /start и
на каждый присланный контакт.
"""

from __future__ import annotations

import aiosqlite

from app.config import Settings
from app.db.crud import Repository
from app.db.database import Database

# Схема до появления `profile_slug` — используется только для проверки миграции.
_OLD_SCHEMA = """
CREATE TABLE users (
    chat_id     INTEGER PRIMARY KEY,
    tg_user_id  INTEGER,
    username    TEXT,
    full_name   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL,
    tg_user_id          INTEGER,
    username            TEXT,
    client_name         TEXT NOT NULL,
    phone_or_contact    TEXT NOT NULL,
    contact_normalized  TEXT NOT NULL,
    dates_or_timing     TEXT NOT NULL,
    service_details     TEXT NOT NULL,
    budget              TEXT,
    summary             TEXT NOT NULL,
    raw_payload         TEXT NOT NULL,
    admin_notified      INTEGER NOT NULL DEFAULT 0,
    webhook_delivered   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
"""


async def _make_lead(repo: Repository, *, profile_slug: str | None = None):
    return await repo.create_lead(
        chat_id=1,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="+79991234567",
        dates_or_timing="12–19 августа",
        service_details="Toyota RAV4",
        budget="до 100 GEL",
        summary="Нужен кроссовер на неделю",
        raw_payload={"client_name": "Иван"},
        profile_slug=profile_slug,
    )


async def test_unknown_chat_has_no_profile(repo: Repository) -> None:
    assert await repo.get_chat_profile(1) is None


async def test_set_and_get_chat_profile(repo: Repository) -> None:
    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван")

    await repo.set_chat_profile(1, "tours")

    assert await repo.get_chat_profile(1) == "tours"


async def test_chat_profile_survives_reconnect(settings: Settings) -> None:
    db = Database(settings.db_path)
    await db.connect()
    repo = Repository(db)
    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван")
    await repo.set_chat_profile(1, "rent_car")
    await db.close()

    reopened = Database(settings.db_path)
    await reopened.connect()
    try:
        assert await Repository(reopened).get_chat_profile(1) == "rent_car"
    finally:
        await reopened.close()


async def test_upsert_user_does_not_reset_profile(repo: Repository) -> None:
    """/start повторно и каждый присланный контакт не должны сбрасывать нишу."""
    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван")
    await repo.set_chat_profile(1, "tours")

    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван Петров")

    assert await repo.get_chat_profile(1) == "tours"


async def test_forget_chat_removes_profile(repo: Repository) -> None:
    await repo.upsert_user(chat_id=1, tg_user_id=42, username="ivan", full_name="Иван")
    await repo.set_chat_profile(1, "tours")

    await repo.forget_chat(1)

    assert await repo.get_chat_profile(1) is None


async def test_lead_stores_and_reads_back_profile_slug(repo: Repository) -> None:
    lead = await _make_lead(repo, profile_slug="rent_car")

    assert lead.profile_slug == "rent_car"
    assert (await repo.get_lead(lead.id)).profile_slug == "rent_car"


async def test_lead_profile_slug_defaults_to_none(repo: Repository) -> None:
    lead = await _make_lead(repo)

    assert lead.profile_slug is None


async def test_migration_adds_columns_to_pre_existing_database(settings: Settings) -> None:
    """БД, созданная по схеме без колонки, должна получить её при следующем connect()."""
    conn = await aiosqlite.connect(settings.db_path)
    await conn.executescript(_OLD_SCHEMA)
    await conn.commit()
    await conn.close()

    db = Database(settings.db_path)
    await db.connect()
    try:
        repo = Repository(db)
        await repo.upsert_user(chat_id=1, tg_user_id=None, username=None, full_name=None)
        await repo.set_chat_profile(1, "tours")
        assert await repo.get_chat_profile(1) == "tours"

        lead = await _make_lead(repo, profile_slug="tours")
        assert lead.profile_slug == "tours"
    finally:
        await db.close()
