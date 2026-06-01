FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy full agriconnect source tree so ingestion service can import domain modules.
COPY backend/src /app/backend/src
COPY backend/pyproject.toml /app/backend/pyproject.toml

ENV PYTHONPATH=/app/backend/src

RUN pip install --no-cache-dir \
    boto3 \
    requests \
    pydantic \
    pydantic-settings \
    sqlalchemy \
    pgvector \
    tqdm \
    pypdf \
    beautifulsoup4

CMD ["python", "-m", "agriconnect.services.ingestion_service.s3_event_consumer"]
