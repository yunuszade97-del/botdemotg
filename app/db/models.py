"""Схема SQLite и датаклассы строк.

Осознанно без ORM: три таблицы и десяток запросов не окупают SQLAlchemy,
а типизированные датаклассы дают ту же безопасность на границе слоёв.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    tg_user_id  INTEGER,
    username    TEXT,
    full_name   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id DESC);

CREATE TABLE IF NOT EXISTS leads (
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
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_chat ON leads (chat_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_leads_contact ON leads (contact_normalized);
"""


@dataclass(slots=True, frozen=True)
class HistoryMessage:
    role: str
    content: str

    def as_llm_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True, frozen=True)
class Lead:
    id: int
    chat_id: int
    tg_user_id: int | None
    username: str | None
    client_name: str
    phone_or_contact: str
    contact_normalized: str
    dates_or_timing: str
    service_details: str
    budget: str | None
    summary: str
    admin_notified: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Lead":
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            tg_user_id=row["tg_user_id"],
            username=row["username"],
            client_name=row["client_name"],
            phone_or_contact=row["phone_or_contact"],
            contact_normalized=row["contact_normalized"],
            dates_or_timing=row["dates_or_timing"],
            service_details=row["service_details"],
            budget=row["budget"],
            summary=row["summary"],
            admin_notified=bool(row["admin_notified"]),
            created_at=row["created_at"],
        )
