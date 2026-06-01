FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    requests \
    beautifulsoup4 \
    pypdf \
    sqlalchemy \
    psycopg2-binary \
    pydantic \
    pydantic-settings \
    pgvector \
    python-json-logger \
    tqdm

COPY backend/src /app/backend/src
COPY backend/sources /app/backend/sources

RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/data/pdfs \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "agriconnect.domain.ingestion.worker", "--mode", "harvest", "--max-queue-items", "0"]
