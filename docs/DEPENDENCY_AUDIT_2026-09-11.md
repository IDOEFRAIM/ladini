# Audit des dépendances — 2026-09-11 (phase déploiement)

Objectif : ne garder que ce qui est **réellement utilisé** par `src/ladini`
(le seul paquet livré) et par `tests/`. Méthode : `deptry src` + grep
exhaustif des `import` / `from … import` top-level, croisé avec les usages
**runtime** (CLI dans le compose / Dockerfile) que deptry ne voit pas.

## Résultat

| Avant | Après |
|---|---|
| ~65 dépendances runtime | **35 runtime** + 7 dev |
| `torch` (~2 Go), scipy/numpy via scikit-learn, 3 libs de logging | supprimés |
| `poetry.lock` du 17 août (désynchronisé de `pyproject.toml`) | régénéré |
| image : `poetry install` **sans lock** + `pip install` flottant par-dessus | `poetry install --only main` **avec lock**, aucun `pip install` |

---

## Supprimé — 0 import dans `src/ladini` ni `tests/` (vérifié)

| Paquet | Pourquoi c'était là | Pourquoi c'est mort |
|---|---|---|
| `torch` `^2.2` | ancien pipeline RAG (embeddings) | ~2 Go ; aucun `import torch`. Le RAG vit dans `src/futur/` (non livré) ou l'ancien paquet `agriconnect` (supprimé au renommage). |
| `scikit-learn` `^1.5` | reranking RAG | tire scipy+numpy ; aucun `import sklearn` |
| `numpy` `^2.1` | maths RAG | aucun `import numpy` ; `rapidfuzz` n'en a pas besoin |
| `pandas` `^2.1` | analyse données ingestion | aucun `import pandas` |
| `pgvector` `^0.3` | store vectoriel Postgres (RAG) | aucun `import pgvector` (7 occurrences = commentaires) |
| `prisma` `^0.11` | ORM alternatif | on est 100 % SQLAlchemy async ; aucun `import prisma` |
| `langchain` / `langchain-community` / `langchain-openai` / `langchain-groq` `^0.3` | couche LangChain historique | le runtime agent utilise **langgraph** + **langchain-core** (transitif, désormais explicite) + les SDK bruts `groq` / `openai`. Aucun `import langchain*`. |
| `structlog` `^24.4` | logging structuré | `core/logger.py` utilise `logging` stdlib. Aucun `import structlog`. |
| `loguru` `^0.7` | logging | idem |
| `python-json-logger` `^2.0` | formatter JSON | idem — 3 libs de logging pour zéro usage |
| `langsmith` `>=0.1` | tracing LangChain | Langfuse Cloud est le choix (`LANGFUSE_*`) ; `langsmith` n'était qu'un passthrough d'env jamais importé |
| `azure-cognitiveservices-speech` `^1.40` | TTS/STT | `USE_AZURE_SPEECH=False` par défaut ; aucun `import azure.*` |
| `azure-storage-blob` `^12.23` | stockage | Supabase Storage est le stockage (`services/storage/supabase_storage.py`) |
| `azure-identity` `^1.19` | auth Azure | avec les 2 ci-dessus |
| `earthengine-api` `^1.7.10` | Google Earth Engine | utilisé UNIQUEMENT par `infra/aws/ladini_daily_advice/` (Lambda séparée, `requirements.txt` propre). Le backend ne l'importe pas. |
| `shapely` `^2.0` | géométrie | `core/geofencing.py` fait un haversine manuel ; aucun `import shapely` |
| `geopy` `^2.4` | géocodage | aucun `import geopy` |
| `aiohttp` `^3.9` | client HTTP | `httpx` partout ; aucun `import aiohttp` |
| `requests` `^2.32` | client HTTP sync | `httpx` partout ; aucun `import requests` |
| `psycopg2-binary` `^2.9` | driver Postgres sync | `asyncpg` est le seul driver. ⚠️ **Si/quand Alembic est branché** : soit configurer `env.py` en async (asyncpg), soit ré-ajouter `psycopg2-binary` pour les migrations sync. |
| `tenacity` `^9.0` | ret/backoff | le code a sa propre logique (LLM Gateway, MCP client) ; aucun `import tenacity` |
| `apscheduler` `^3.11` | ordonnanceur | Celery **Beat** est l'ordonnanceur (`workers/beat_schedule.py`) ; aucun `import apscheduler` |
| `ftfy` `^6.2` | réparation de texte | aucun `import ftfy` |
| `aiofiles` `^23.1` | I/O fichier async | aucun `import aiofiles` |
| `pillow` `^10.0` | images | le pipeline photo passe les octets tels quels (`twilio_media.py` → `supabase_storage.py`) ; aucun `import PIL` |
| `tqdm` `^4.66` | barres de progression | scripts CLI d'ingestion uniquement ; aucun `import tqdm` dans `src/ladini` |
| `scikit-learn`, `numpy`, `pandas`, `scipy` | (récapitulé) | tout le sous-graphe data-science part avec `torch`/`scikit-learn` |

## Déplacé vers le groupe `dev` (jamais dans l'image de prod)

`pytest` (était en **dépendance principale**), `pytest-asyncio`, `pytest-cov`,
`ruff`, `mypy`, `pyyaml` (seulement `tests/evals/runners/`), **`deptry`**
(ajouté — audit des dépendances en CI).

L'image de production fait `poetry install --only main` → le groupe `dev`
n'y entre pas.

## Ajouté — importé directement mais tiré en transitif (DEP003)

| Paquet | Importé par | Fournisseur transitif |
|---|---|---|
| `typing-extensions` `^4.12` | `graphs/.../core/state.py`, `flows/{buyer,producer}/state.py` | langgraph |
| `langchain-core` `^0.3` | `workspace/checkpointer.py` | langgraph |
| `mcp` `>=1.0` | `protocols/mcp/servers/{db_server,http_server}.py` | fastmcp |
| `starlette` `^0.40` | `protocols/mcp/servers/http_server.py` | fastapi |

`botocore` (importé par `core/get_llm.py` pour ses types d'exception) reste
implicite via `boto3` — ignoré explicitement dans `[tool.deptry.per_rule_ignores]`.

## Versions alignées

| Paquet | Avant | Après | Raison |
|---|---|---|---|
| `sentry-sdk` | `^1.39` | `^2.0` | v1 en fin de vie ; l'API utilisée (`init` + `LoggingIntegration`) est identique v1↔v2 et le code est `try/except` |
| `mypy` (dev) | `>=2.3.1,<3.0.0` | `^1.11` | **mypy 2.x n'existe pas** (dernière = 1.13). La borne d'origine était invalide → aucune version ne satisfaisait, le lock ne pouvait pas se régénérer proprement |
| `pytest-cov` (dev) | `>=7.1.0,<8.0.0` | `^7.1` | inchangé sur le fond, normalisé |

## Conservé (deptry le signale mais c'est un usage RUNTIME, pas un import)

| Paquet | Où |
|---|---|
| `gunicorn` | `CMD` de `infra/docker/Dockerfile.api` |
| `flower` | service `flower` du compose (`celery … flower`) |
| `alembic` | `scripts/deploy.sh` (`alembic upgrade head`) — outil de migration cible |
| `python-multipart` | requis par FastAPI pour `Form(...)` (`api/routes/twilio_webhook.py`) |
| `uvicorn[standard]` | `CMD` de `Dockerfile.mcp` + dev |
| `boto3` | `core/get_llm.py` — `import boto3` **paresseux** (chemin Bedrock natif) |

## Garde-fou

`deptry src` est maintenant un **step bloquant** de la CI (`.github/workflows/cicd.yml`,
job `test`) + `poetry check --lock` (cohérence `pyproject.toml` ↔ `poetry.lock`).
Une dépendance déclarée-mais-inutilisée fait échouer la PR.
