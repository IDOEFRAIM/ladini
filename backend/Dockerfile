# Utiliser une version stable et légère
FROM python:3.13-slim AS base

# Empêcher Python de générer des .pyc et forcer l'affichage des logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="/opt/poetry/bin:$PATH"

# Make network installs more resilient
ENV POETRY_HTTP_TIMEOUT=300 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_NO_CACHE_DIR=1

# Installation des dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

WORKDIR /app

# --- ETAPE CRUCIALE POUR LE CACHE ---
# On ne copie que les fichiers de dépendances d'abord
COPY pyproject.toml poetry.lock ./

# On installe les dépendances (Docker mettra cette couche en cache)
RUN poetry config virtualenvs.create false || true
# Retry loop for network instability when installing dependencies
RUN set -eux; \
    n=0; \
    until [ "$n" -ge 4 ]; do \
        poetry install --no-root --no-interaction --no-ansi --only main && break; \
        n=$((n+1)); \
        echo "poetry install failed, retrying ($n)..."; \
        sleep 15; \
    done; \
    if [ "$n" -ge 4 ]; then echo "poetry install failed after retries"; exit 1; fi

# Maintenant on copie le reste du code
COPY . .

# Installation du projet lui-même
RUN set -eux; \
    n=0; \
    until [ "$n" -ge 4 ]; do \
        poetry install --no-interaction --no-ansi --only main && break; \
        n=$((n+1)); \
        echo "poetry install (project) failed, retrying ($n)..."; \
        sleep 15; \
    done; \
    if [ "$n" -ge 4 ]; then echo "poetry install (project) failed after retries"; exit 1; fi

EXPOSE 8000
CMD ["python", "-m", "src.main"] # Adapte selon ton point d'entrée