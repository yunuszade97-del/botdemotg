"""Операции с БД: пользователи, история диалога, лиды."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db.database import Database
from app.db.models import HistoryMessage, Lead

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- пользователи -------------------------------------------------------
    async def upsert_user(
        self,
        *,
        chat_id: int,
        tg_user_id: int | None,
        username: str | None,
        full_name: str | None,
    ) -> None:
        now = _utcnow()
        await self._db.connection.execute(
            """
            INSERT INTO users (chat_id, tg_user_id, username, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                tg_user_id = excluded.tg_user_id,
                username   = excluded.username,
                full_name  = excluded.full_name,
                updated_at = excluded.updated_at
            """,
            (chat_id, tg_user_id, username, full_name, now, now),
        )
        await self._db.connection.commit()

    # --- история диалога ----------------------------------------------------
    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self._db.connection.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, _utcnow()),
        )
        await self._db.connection.commit()

    async def add_messages(self, chat_id: int, items: list[tuple[str, str]]) -> None:
        """Пишет несколько ходов одной транзакцией (реплика юзера + ответ бота)."""
        now = _utcnow()
        await self._db.connection.executemany(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(chat_id, role, content, now) for role, content in items],
        )
        await self._db.connection.commit()

    async def get_history(
        self, chat_id: int, *, limit: int, max_chars: int
    ) -> list[HistoryMessage]:
        """Последние `limit` сообщений в хронологическом порядке.

        Дополнительно режет окно по суммарной длине: 14 коротких реплик и 14
        простыней по 2000 символов — совершенно разные счета за токены.
        """
        cursor = await self._db.connection.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        selected: list[HistoryMessage] = []
        budget = max_chars
        for row in rows:  # от свежих к старым, пока хватает бюджета символов
            content = row["content"]
            if len(content) > budget and selected:
                break
            budget -= len(content)
            selected.append(HistoryMessage(role=row["role"], content=content))
        selected.reverse()
        return selected

    async def clear_history(self, chat_id: int) -> int:
        cursor = await self._db.connection.execute(
            "DELETE FROM messages WHERE chat_id = ?", (chat_id,)
        )
        await self._db.connection.commit()
        return cursor.rowcount or 0

    # --- лиды ---------------------------------------------------------------
    async def find_recent_duplicate(
        self, *, chat_id: int, contact_normalized: str, window_minutes: int
    ) -> Lead | None:
        """Ищет свежий лид того же клиента с тем же контактом.

        Модель часто вызывает save_qualified_lead повторно на каждом
        следующем сообщении; без этой проверки менеджер получает серию
        одинаковых «горячих лидов».
        """
        if window_minutes <= 0:
            return None
        threshold = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat(timespec="seconds")
        cursor = await self._db.connection.execute(
            """
            SELECT * FROM leads
            WHERE chat_id = ? AND contact_normalized = ? AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (chat_id, contact_normalized, threshold),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return Lead.from_row(row) if row else None

    async def create_lead(
        self,
        *,
        chat_id: int,
        tg_user_id: int | None,
        username: str | None,
        client_name: str,
        phone_or_contact: str,
        contact_normalized: str,
        dates_or_timing: str,
        service_details: str,
        budget: str | None,
        summary: str,
        raw_payload: dict,
    ) -> Lead:
        cursor = await self._db.connection.execute(
            """
            INSERT INTO leads (
                chat_id, tg_user_id, username, client_name, phone_or_contact,
                contact_normalized, dates_or_timing, service_details, budget,
                summary, raw_payload, admin_notified, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                chat_id,
                tg_user_id,
                username,
                client_name,
                phone_or_contact,
                contact_normalized,
                dates_or_timing,
                service_details,
                budget,
                summary,
                json.dumps(raw_payload, ensure_ascii=False),
                _utcnow(),
            ),
        )
        await self._db.connection.commit()
        lead_id = cursor.lastrowid
        await cursor.close()
        stored = await self.get_lead(int(lead_id))
        assert stored is not None  # только что вставили
        logger.info("Лид #%s сохранён (chat_id=%s)", stored.id, chat_id)
        return stored

    async def get_lead(self, lead_id: int) -> Lead | None:
        cursor = await self._db.connection.execute(
            "SELECT * FROM leads WHERE id = ?", (lead_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return Lead.from_row(row) if row else None

    async def mark_lead_notified(self, lead_id: int) -> None:
        await self._db.connection.execute(
            "UPDATE leads SET admin_notified = 1 WHERE id = ?", (lead_id,)
        )
        await self._db.connection.commit()

    async def mark_lead_webhook_sent(self, lead_id: int) -> None:
        await self._db.connection.execute(
            "UPDATE leads SET webhook_delivered = 1 WHERE id = ?", (lead_id,)
        )
        await self._db.connection.commit()

    async def list_pending_webhooks(self, limit: int = 50) -> list[Lead]:
        """Лиды, не доставленные во внешнюю систему (сбой сети/рестарт)."""
        cursor = await self._db.connection.execute(
            "SELECT * FROM leads WHERE webhook_delivered = 0 ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [Lead.from_row(row) for row in rows]

    async def list_pending_notifications(self, limit: int = 50) -> list[Lead]:
        """Лиды, о которых админ ещё не оповещён (падение сети/рестарт)."""
        cursor = await self._db.connection.execute(
            "SELECT * FROM leads WHERE admin_notified = 0 ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [Lead.from_row(row) for row in rows]

    async def count_leads(self) -> int:
        cursor = await self._db.connection.execute("SELECT COUNT(*) AS c FROM leads")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    async def last_leads(self, limit: int = 5) -> list[Lead]:
        cursor = await self._db.connection.execute(
            "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [Lead.from_row(row) for row in rows]

    # --- суточные лимиты расходов на LLM ------------------------------------
    async def count_llm_calls_today(self, chat_id: int) -> int:
        cursor = await self._db.connection.execute(
            "SELECT llm_calls FROM usage_daily WHERE day = ? AND chat_id = ?",
            (_today(), chat_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["llm_calls"]) if row else 0

    async def count_llm_calls_today_global(self) -> int:
        cursor = await self._db.connection.execute(
            "SELECT COALESCE(SUM(llm_calls), 0) AS total FROM usage_daily WHERE day = ?",
            (_today(),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["total"]) if row else 0

    async def register_llm_call(self, chat_id: int) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO usage_daily (day, chat_id, llm_calls) VALUES (?, ?, 1)
            ON CONFLICT(day, chat_id) DO UPDATE SET llm_calls = llm_calls + 1
            """,
            (_today(), chat_id),
        )
        await self._db.connection.commit()

    # --- удаление персональных данных ---------------------------------------
    async def purge_old_messages(self, days: int) -> int:
        """Чистит историю старше N дней. 0 — не удалять."""
        if days <= 0:
            return 0
        cursor = await self._db.connection.execute(
            "DELETE FROM messages WHERE created_at < ?", (_days_ago(days),)
        )
        await self._db.connection.commit()
        return cursor.rowcount or 0

    async def purge_old_leads(self, days: int) -> int:
        if days <= 0:
            return 0
        cursor = await self._db.connection.execute(
            "DELETE FROM leads WHERE created_at < ?", (_days_ago(days),)
        )
        await self._db.connection.commit()
        return cursor.rowcount or 0

    async def purge_old_usage(self, days: int = 30) -> int:
        threshold = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        cursor = await self._db.connection.execute(
            "DELETE FROM usage_daily WHERE day < ?", (threshold,)
        )
        await self._db.connection.commit()
        return cursor.rowcount or 0

    async def forget_chat(self, chat_id: int) -> tuple[int, int]:
        """Полностью удаляет данные одного пользователя (запрос на удаление ПД).

        Возвращает (удалено сообщений, удалено заявок).
        """
        conn = self._db.connection
        cursor = await conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        messages = cursor.rowcount or 0
        cursor = await conn.execute("DELETE FROM leads WHERE chat_id = ?", (chat_id,))
        leads = cursor.rowcount or 0
        await conn.execute("DELETE FROM usage_daily WHERE chat_id = ?", (chat_id,))
        await conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        await conn.commit()
        return messages, leads

    # --- аналитика ----------------------------------------------------------
    async def count_users(self) -> int:
        cursor = await self._db.connection.execute("SELECT COUNT(*) AS c FROM users")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    async def count_dialogs(self) -> int:
        """Чаты, в которых был хотя бы один обмен репликами."""
        cursor = await self._db.connection.execute(
            "SELECT COUNT(DISTINCT chat_id) AS c FROM messages"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    async def count_leads_since(self, days: int) -> int:
        cursor = await self._db.connection.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE created_at >= ?", (_days_ago(days),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    async def all_leads(self) -> list[Lead]:
        cursor = await self._db.connection.execute("SELECT * FROM leads ORDER BY id ASC")
        rows = await cursor.fetchall()
        await cursor.close()
        return [Lead.from_row(row) for row in rows]
