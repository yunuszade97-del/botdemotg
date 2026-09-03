#!/usr/bin/env bash
# Бэкап базы лидов. В cron: 0 3 * * * /opt/leadbot/deploy/backup.sh
#
# Копировать файл SQLite обычным cp нельзя: при включённом WAL легко получить
# копию на середине транзакции. Connection.backup() делает согласованный
# снимок на работающей базе — останавливать бота не нужно.
#
# Используется python из venv, а не консольная утилита sqlite3: python на
# машине с ботом есть всегда, sqlite3 CLI на голом VPS часто отсутствует.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/leadbot}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
DB_PATH="${DB_PATH:-$APP_DIR/data/bot.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

command -v "$PYTHON" > /dev/null || PYTHON="$(command -v python3)"

if [[ ! -f "$DB_PATH" ]]; then
    echo "Базы нет: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
TARGET="$BACKUP_DIR/bot-$(date -u +%Y%m%d-%H%M%S).sqlite3.gz"

DB_PATH="$DB_PATH" TARGET="$TARGET" "$PYTHON" - <<'PY'
import gzip
import os
import shutil
import sqlite3
import tempfile

source = os.environ["DB_PATH"]
target = os.environ["TARGET"]

with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
    snapshot = tmp.name

try:
    with sqlite3.connect(source) as src, sqlite3.connect(snapshot) as dst:
        src.backup(dst)
    with open(snapshot, "rb") as raw, gzip.open(target, "wb") as archive:
        shutil.copyfileobj(raw, archive)
finally:
    os.unlink(snapshot)
PY

find "$BACKUP_DIR" -name 'bot-*.sqlite3.gz' -mtime "+$KEEP_DAYS" -delete

echo "Бэкап готов: $TARGET ($(du -h "$TARGET" | cut -f1))"
