"""Инициализация и жизненный цикл соединения с SQLite."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from app.db.models import MIGRATIONS, SCHEMA

logger = logging.getLogger(__name__)


class Database:
    """Тонкая обёртка над одним долгоживущим соединением aiosqlite.

    aiosqlite исполняет запросы в выделенном потоке через очередь, поэтому
    одно соединение безопасно шарить между корутинами. WAL включён, чтобы
    чтения не блокировались записью.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        logger.info("SQLite готова: %s", self._path)

    async def _migrate(self) -> None:
        """Добавляет колонки, появившиеся после первого релиза.

        CREATE TABLE IF NOT EXISTS не меняет существующую таблицу, поэтому
        обновление кода на работающем боте иначе падало бы на первом запросе
        к новой колонке.
        """
        assert self._conn is not None
        for table, column, ddl in MIGRATIONS:
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            if column in columns:
                continue
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            logger.info("Миграция: в %s добавлена колонка %s", table, column)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Соединение с SQLite закрыто")
