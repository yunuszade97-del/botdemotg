FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Зависимости отдельным слоем: правка кода не заставляет ставить их заново.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY content ./content
COPY profiles ./profiles

# Процесс не должен работать от root: пробой в зависимости не должен давать
# прав на всю файловую систему контейнера.
RUN useradd --create-home --uid 10001 leadbot \
    && mkdir -p /app/data \
    && chown -R leadbot:leadbot /app
USER leadbot

# SQLite-файл живёт в томе, иначе лиды исчезнут вместе с контейнером.
VOLUME ["/app/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PORT','8080')}/healthz\", timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
