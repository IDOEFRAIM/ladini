# Core package

Foundational runtime primitives: configuration, database, logging, security, tracing, and LLM factory. These modules are imported by API, workers and graphs; keep them small, deterministic and framework-agnostic.

## Contents

| File | Role |
| --- | --- |
| `settings.py` | Central application settings (env, secrets, feature flags). Used by API and workers. |
| `cloud_settings.py` | Cloud-specific settings (buckets, queues, endpoints). Consumed by `settings.py`. |
| `database.py` | Database engine/session management, connection pooling, helpers. Single source for DB access config. |
| `logger.py` | Logger configuration (formatters/handlers) for structured logs. |
| `logging.py` | Thin compatibility layer around the logger setup. |
| `security.py` | App-level security helpers (hashing, token/signature utilities, guards). |
| `tracing.py` | Tracing/metrics integration (OpenTelemetry-style hooks where available). |
| `watcher.py` | Lightweight file/service watcher for dev and background tasks. |
| `get_llm.py` | Factory to build configured LLM clients according to settings (provider/model/adapter). |
| `llm.py` | Minimal LLM client abstraction used by orchestrators or graphs. |
| `setup.py` | Bootstrapping utilities used in dev/ops scripts (keep minimal in prod). |

## Responsibilities

- Provide a single, stable source of truth for config and infra primitives.
- Keep third-party SDK wiring here, not in business logic.
- Avoid circular imports: other packages may import from `core`, but `core` should not depend on high-level app code.
