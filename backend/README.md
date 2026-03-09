AgriConnect Backend
===================

This folder contains the backend service for AgriConnect. It provides A2A discovery
and routing, MCP integrations, RAG pipelines, and multi-channel rendering.

Run `poetry install` in this folder to create the virtualenv and install dependencies.

Local dependencies
------------------
- Redis is required for durable A2A messaging and Celery (default `redis://localhost:6379/0`).

Quick start (with Docker)
-------------------------
Start Redis and run the app locally:

```bash
# start Redis (quick)
docker run -d --name agriconnect-redis -p 6379:6379 redis:7

# install deps
cd backend
poetry install

# run the app from the package
cd src
poetry run python -m agriconnect.main
```

Running Celery workers
----------------------
If you want background workers for heavy tasks (TTS, indexing):

```bash
# start a worker using the Celery broker from REDIS_URL
poetry run celery -A agriconnect.workers.celery_app worker --loglevel=info
```

Environment and secrets
-----------------------
- Copy `src/agriconnect/.env.example` → `src/agriconnect/.env` and fill secrets.
- IMPORTANT: Do NOT commit `src/agriconnect/.env` to git. Rotate any keys if `.env` was previously committed.

To remove a committed .env and avoid leaking secrets:

```bash
git rm --cached src/agriconnect/.env
git commit -m "remove .env from repo"
```

Healthchecks & testing
----------------------
- App exposes `/health` for a quick liveness check. Confirm Redis and DB readiness via the same endpoint.
- Quick Redis smoke test (requires Redis running):

```bash
python - <<'PY'
from agriconnect.protocols.a2a.messaging import RedisBroker, A2AMessage
from agriconnect.core.settings import settings
rb=RedisBroker(settings.REDIS_URL)
msg=A2AMessage(sender_id='tester', receiver_id='agent_x', intent='PING', payload={'hello': 'world'})
rb.enqueue('agent_x', msg)
print('len=', rb.queue_length('agent_x'))
print('deq=', rb.dequeue('agent_x'))
PY
```

Notes
-----
- The A2A channel now prefers Redis when `REDIS_URL` or `CELERY_BROKER_URL` is configured; it falls back to an in-memory broker for development only.
- Add `REDIS_URL` to your deployment environment or `.env` (see `.env.example`).

Redis Cloud (redislabs) guidance
--------------------------------

If you want to use Redis Cloud (Redis Labs) instead of a local Redis instance:

- Use the TLS endpoint (recommended) — the URL typically looks like:
	`rediss://default:<PASSWORD>@<HOST>:<PORT>/0`.
- Put that value in `REDIS_URL` in your environment or in `backend/.env` (do not commit secrets).
- The project already supports `rediss://` and will pick `settings.REDIS_URL` automatically.
- Quick test script: `python backend/scripts/redis_cloud_example.py` (prints `PING -> True` on success).

Security notes:

- Rotate credentials regularly and restrict allowed CIDR/IPs in your Redis Cloud project's access control.
- Prefer `rediss://` (TLS) for production traffic.

If you want, I can add a `docker-compose.yml` that starts Redis + the backend for local development.
