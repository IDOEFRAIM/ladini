# Core package

Foundational runtime primitives: configuration, database, logging, security, tracing, and LLM factory. These modules are imported by API, workers and graphs; keep them small, deterministic and framework-agnostic.

## Contents

| File | Role |
| --- | --- |
| `settings.py` | Central application settings (env, secrets, feature flags). Used by API, workers and all market_coach graphs. Single source — nothing else defines app config. |
| `database.py` | Database engine/session management, connection pooling, helpers. Single source for DB access config. |
| `logger.py` | Canonical logger factory (`get_logger`) + `setup_logging()` (Sentry init). Import `get_logger` from here — nowhere else. |
| `security.py` | App-level security helpers (API key validation, request IDs, input sanitization). |
| `tracing.py` | LangSmith tracing integration (`init_tracing`, `trace_agent`, `get_tracing_config`). Not currently wired into the live app startup — available for reuse when observability work resumes. |
| `get_llm.py` | Internal factory that builds the Groq-backed LLM client singleton. Implementation detail — do not import directly. |
| `llm.py` | **Public LLM facade.** `from ladini.core.llm import get_llm, get_groq_sdk` — the only sanctioned import path for LLM access. |

Removed as dead weight (2026-07-12 cleanup): `cloud_settings.py` (never imported, duplicated `settings.py`), `logging.py` (duplicate logger factory — callers migrated to `logger.py`), `watcher.py` and `setup.py` (pre-LangGraph legacy referencing modules that no longer exist: `services.db_handler`, `protocols.mcp`, `protocols.ag_ui`, `core.db`, `futur.tools.sentinelle`).

## Responsibilities

- Provide a single, stable source of truth for config and infra primitives.
- Keep third-party SDK wiring here, not in business logic.
- Avoid circular imports: other packages may import from `core`, but `core` should not depend on high-level app code.
