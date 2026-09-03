"""Доставка лида во внешнюю систему обычным HTTP-вебхуком.

Осознанный отказ от SDK конкретной CRM. Один URL в настройках подключается
к Make / Zapier / n8n / Google Apps Script / собственному бэкенду, а уже
оттуда — в Google Sheets, amoCRM, Bitrix24 или что угодно ещё. Смена системы
у клиента становится изменением настройки, а не переписыванием кода.

Гарантия та же, что и у уведомления админа: лид уже лежит в БД, поэтому
неудачная доставка не теряет заявку — строка остаётся с webhook_delivered = 0
и будет дослана при следующем старте.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from app.db.crud import Repository
from app.db.models import Lead

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SIGNATURE_HEADER = "X-Lead-Signature"


def build_payload(lead: Lead, company: str) -> dict[str, Any]:
    """Плоская и стабильная структура: её будут разбирать в no-code сервисах."""
    return {
        "event": "lead.created",
        "company": company,
        "lead": {
            "id": lead.id,
            "created_at": lead.created_at,
            "client_name": lead.client_name,
            "phone_or_contact": lead.phone_or_contact,
            "contact_key": lead.contact_normalized,
            "dates_or_timing": lead.dates_or_timing,
            "service_details": lead.service_details,
            "budget": lead.budget,
            "summary": lead.summary,
            "telegram_chat_id": lead.chat_id,
            "telegram_user_id": lead.tg_user_id,
            "telegram_username": lead.username,
        },
    }


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256, чтобы принимающая сторона отличала наши запросы от чужих."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class LeadWebhookSender:
    def __init__(
        self,
        *,
        url: str,
        secret: str = "",
        company: str = "",
        timeout: float = 10.0,
        repo: Repository | None = None,
        client: httpx.AsyncClient | None = None,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._url = url
        self._secret = secret
        self._company = company
        self._timeout = timeout
        self._repo = repo
        self._retry_base_delay = retry_base_delay
        # Один долгоживущий клиент вместо клиента на запрос: переиспользуется
        # соединение, не тратится TLS-рукопожатие на каждый лид.
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def send(self, lead: Lead) -> bool:
        if not self.enabled:
            return False

        body = json.dumps(
            build_payload(lead, self._company), ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._secret:
            headers[SIGNATURE_HEADER] = sign(body, self._secret)

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._get_client().post(
                    self._url, content=body, headers=headers
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Вебхук лида #%s: попытка %s не удалась (%s)",
                    lead.id,
                    attempt + 1,
                    exc,
                )
            else:
                if response.is_success:
                    if self._repo is not None:
                        await self._repo.mark_lead_webhook_sent(lead.id)
                    logger.info("Лид #%s отправлен во внешнюю систему", lead.id)
                    return True
                # 4xx не лечится повтором: неверный URL, отозванный ключ, битый
                # маршрут в no-code сервисе. Повторять смысла нет.
                if 400 <= response.status_code < 500:
                    logger.error(
                        "Вебхук лида #%s отклонён: HTTP %s. Проверьте LEAD_WEBHOOK_URL",
                        lead.id,
                        response.status_code,
                    )
                    return False
                logger.warning(
                    "Вебхук лида #%s: HTTP %s (попытка %s)",
                    lead.id,
                    response.status_code,
                    attempt + 1,
                )

            if attempt < MAX_ATTEMPTS - 1 and self._retry_base_delay:
                await asyncio.sleep(self._retry_base_delay * 2**attempt)

        logger.error(
            "Лид #%s не доставлен во внешнюю систему — остаётся в очереди", lead.id
        )
        return False

    async def flush_pending(self, limit: int = 50) -> int:
        """Досылает лиды, доставка которых не прошла раньше."""
        if not self.enabled or self._repo is None:
            return 0
        pending = await self._repo.list_pending_webhooks(limit)
        if not pending:
            return 0
        logger.info("Досылаю во внешнюю систему %s лидов", len(pending))
        return sum([await self.send(lead) for lead in pending])
