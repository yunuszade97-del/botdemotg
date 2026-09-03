"""Доставка лида владельцу бизнеса в Telegram."""

from __future__ import annotations

import asyncio
import logging
from html import escape

import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.db.crud import Repository
from app.db.models import Lead

logger = logging.getLogger(__name__)

LEAD_TEMPLATE = """\
🔥 <b>НОВЫЙ ГОРЯЧИЙ ЛИД!</b>

👤 <b>Имя:</b> {client_name}
📞 <b>Контакт:</b> {contact}
📅 <b>Сроки/Даты:</b> {dates}
🚗 <b>Запрос:</b> {service}
💰 <b>Бюджет:</b> {budget}
📝 <b>Суть:</b> {summary}
💬 <b>Профиль:</b> {profile}

<i>Заявка #{lead_id} · {created_at}</i>"""


def _profile_link(lead: Lead) -> str:
    """Ссылка на профиль клиента.

    tg://user?id=… открывается только у тех, кто уже видел пользователя,
    поэтому при наличии @username даём оба варианта.
    """
    parts: list[str] = []
    if lead.username:
        parts.append(f"@{escape(lead.username)}")
    if lead.tg_user_id:
        parts.append(f'<a href="tg://user?id={lead.tg_user_id}">открыть чат</a>')
    return " · ".join(parts) if parts else "—"


def render_lead(lead: Lead) -> str:
    """Готовое HTML-сообщение для админа. Все поля экранируются."""
    return LEAD_TEMPLATE.format(
        client_name=escape(lead.client_name),
        contact=escape(lead.phone_or_contact),
        dates=escape(lead.dates_or_timing),
        service=escape(lead.service_details),
        budget=escape(lead.budget) if lead.budget else "не обсуждался",
        summary=escape(lead.summary),
        profile=_profile_link(lead),
        lead_id=lead.id,
        created_at=escape(lead.created_at.replace("T", " ")),
    )


class AdminNotifier:
    """Рассылает лид всем админам.

    Лид уже лежит в БД к моменту вызова, поэтому сбой доставки не теряет
    заявку: строка остаётся с admin_notified = 0 и будет дослана при
    следующем старте (`flush_pending`).
    """

    def __init__(
        self,
        bot: Bot,
        repo: Repository,
        admin_ids: list[int],
        *,
        alert_cooldown: float = 900.0,
    ) -> None:
        self._bot = bot
        self._repo = repo
        self._admin_ids = admin_ids
        self._alert_cooldown = alert_cooldown
        self._last_alert: dict[str, float] = {}  # ключ -> момент прошлой отправки

    async def notify(self, lead: Lead) -> bool:
        text = render_lead(lead)
        delivered = False
        for admin_id in self._admin_ids:
            if await self._send(admin_id, text):
                delivered = True
        if delivered:
            await self._repo.mark_lead_notified(lead.id)
        else:
            logger.error(
                "Лид #%s не доставлен ни одному админу — останется в очереди", lead.id
            )
        return delivered

    async def _send(self, chat_id: int, text: str) -> bool:
        for attempt in range(3):
            try:
                await self._bot.send_message(
                    chat_id, text, parse_mode="HTML", disable_web_page_preview=True
                )
                return True
            except TelegramRetryAfter as exc:
                logger.warning("Flood-limit для %s: ждём %sс", chat_id, exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError as exc:
                logger.error(
                    "Не удалось отправить лид админу %s (попытка %s): %s",
                    chat_id,
                    attempt + 1,
                    exc,
                )
                if attempt == 2:
                    return False
                await asyncio.sleep(2**attempt)
        return False

    async def flush_pending(self, limit: int = 50) -> int:
        """Досылает лиды, оповещение по которым не прошло раньше."""
        pending = await self._repo.list_pending_notifications(limit)
        if not pending:
            return 0
        logger.info("Досылаю %s недоставленных лидов", len(pending))
        sent = 0
        for lead in pending:
            if await self.notify(lead):
                sent += 1
        return sent

    async def alert(self, key: str, text: str) -> bool:
        """Технический алерт админу с защитой от лавины повторов.

        Когда падает LLM, ошибка приходит на каждое сообщение каждого клиента.
        Без дедупа админ получит сотню одинаковых сообщений и отключит
        уведомления — ровно тогда, когда они нужнее всего.
        """
        now = time.monotonic()
        # Сентинел None, а не 0.0: monotonic() отсчитывается от произвольной
        # точки, и на свежезапущенной машине разница с нулём меньше кулдауна —
        # первый же алерт был бы проглочен ровно тогда, когда он нужен.
        previous = self._last_alert.get(key)
        if previous is not None and now - previous < self._alert_cooldown:
            return False
        self._last_alert[key] = now

        body = f"⚠️ <b>Проблема в работе бота</b>\n\n{escape(text)}"
        sent = False
        for admin_id in self._admin_ids:
            if await self._send(admin_id, body):
                sent = True
        return sent
