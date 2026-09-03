"""Выгрузка лида во внешнюю систему и миграции существующей БД."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from app.bot.services.lead_webhook import (
    SIGNATURE_HEADER,
    LeadWebhookSender,
    build_payload,
    sign,
)
from app.db.crud import Repository
from app.db.database import Database
from app.db.models import Lead


async def _make_lead(repo: Repository, chat_id: int = 1) -> Lead:
    return await repo.create_lead(
        chat_id=chat_id,
        tg_user_id=42,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+7 999 123-45-67",
        contact_normalized="tel:9991234567",
        dates_or_timing="12–19 августа",
        service_details="Toyota RAV4",
        budget="до 100 GEL",
        summary="Кроссовер на неделю",
        raw_payload={},
    )


def _sender(repo: Repository, handler, **kwargs) -> LeadWebhookSender:
    transport = httpx.MockTransport(handler)
    return LeadWebhookSender(
        url="https://hooks.example.com/lead",
        company="TestCo",
        repo=repo,
        client=httpx.AsyncClient(transport=transport),
        retry_base_delay=0.0,  # тесты не должны ждать реальных пауз между ретраями
        **kwargs,
    )


async def test_disabled_when_url_is_empty(repo: Repository) -> None:
    """Без настройки бот работает как раньше — лиды только в Telegram."""
    sender = LeadWebhookSender(url="", repo=repo)

    assert sender.enabled is False
    assert await sender.send(await _make_lead(repo)) is False


async def test_successful_delivery_marks_lead(repo: Repository) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    lead = await _make_lead(repo)
    sender = _sender(repo, handler)

    assert await sender.send(lead) is True

    payload = json.loads(captured[0].content)
    assert payload["event"] == "lead.created"
    assert payload["company"] == "TestCo"
    assert payload["lead"]["client_name"] == "Иван"
    assert payload["lead"]["phone_or_contact"] == "+7 999 123-45-67"
    assert payload["lead"]["telegram_username"] == "ivan"

    stored = await repo.get_lead(lead.id)
    assert stored is not None and stored.webhook_delivered is True


async def test_payload_keeps_cyrillic_readable(repo: Repository) -> None:
    """No-code сервисы показывают тело запроса как есть — \\u0418 нечитаемо."""
    lead = await _make_lead(repo)

    body = json.dumps(build_payload(lead, "TestCo"), ensure_ascii=False)

    assert "Иван" in body


async def test_signature_lets_receiver_verify_sender(repo: Repository) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    sender = _sender(repo, handler, secret="topsecret")
    await sender.send(await _make_lead(repo))

    request = captured[0]
    expected = hmac.new(b"topsecret", request.content, hashlib.sha256).hexdigest()
    assert request.headers[SIGNATURE_HEADER] == f"sha256={expected}"


async def test_no_signature_header_without_secret(repo: Repository) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    await _sender(repo, handler).send(await _make_lead(repo))

    assert SIGNATURE_HEADER not in captured[0].headers


async def test_client_error_is_not_retried(repo: Repository) -> None:
    """404 или 401 повтором не лечится — это неверный URL или отозванный ключ."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404)

    lead = await _make_lead(repo)

    assert await _sender(repo, handler).send(lead) is False
    assert attempts == 1
    stored = await repo.get_lead(lead.id)
    assert stored is not None and stored.webhook_delivered is False


async def test_server_error_is_retried(repo: Repository) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200) if attempts == 3 else httpx.Response(503)

    assert await _sender(repo, handler).send(await _make_lead(repo)) is True
    assert attempts == 3


async def test_failed_delivery_keeps_lead_in_queue(repo: Repository) -> None:
    """Недоступный приёмник не должен терять заявку."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("приёмник недоступен")

    lead = await _make_lead(repo)

    assert await _sender(repo, handler).send(lead) is False
    assert [x.id for x in await repo.list_pending_webhooks()] == [lead.id]


async def test_flush_pending_resends_after_restart(repo: Repository) -> None:
    await _make_lead(repo)
    await _make_lead(repo, chat_id=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    sent = await _sender(repo, handler).flush_pending()

    assert sent == 2
    assert await repo.list_pending_webhooks() == []


def test_sign_is_stable() -> None:
    assert sign(b"payload", "secret") == sign(b"payload", "secret")
    assert sign(b"payload", "secret") != sign(b"payload", "other")


# --- миграции ------------------------------------------------------------------


async def test_migration_adds_column_to_existing_database(tmp_path: Path) -> None:
    """Обновление кода на работающем боте не должно ронять его на новой колонке."""
    import aiosqlite

    path = tmp_path / "legacy.sqlite3"
    # Схема предыдущего релиза: без webhook_delivered.
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, tg_user_id INTEGER, username TEXT,
                client_name TEXT NOT NULL, phone_or_contact TEXT NOT NULL,
                contact_normalized TEXT NOT NULL, dates_or_timing TEXT NOT NULL,
                service_details TEXT NOT NULL, budget TEXT, summary TEXT NOT NULL,
                raw_payload TEXT NOT NULL, admin_notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO leads (chat_id, client_name, phone_or_contact,
                contact_normalized, dates_or_timing, service_details, summary,
                raw_payload, created_at)
            VALUES (1, 'Старый лид', '+79991234567', 'tel:9991234567', 'август',
                    'RAV4', 'из прошлой версии', '{}', '2026-01-01T00:00:00+00:00')
            """
        )
        await conn.commit()

    db = Database(path)
    await db.connect()
    try:
        repo = Repository(db)
        leads = await repo.all_leads()
        assert len(leads) == 1
        assert leads[0].client_name == "Старый лид"
        # Старые заявки не считаются доставленными — они уйдут при досылке.
        assert leads[0].webhook_delivered is False
    finally:
        await db.close()


async def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.sqlite3"
    for _ in range(2):
        db = Database(path)
        await db.connect()
        await db.close()

    db = Database(path)
    await db.connect()
    try:
        assert await Repository(db).count_leads() == 0
    finally:
        await db.close()
