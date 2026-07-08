# Domain models

Canonical data structures shared across the application. This package centralizes the business entities (users, farms, crops, orders, etc.) so that services and graphs speak the same language.

## Contents

| File | Role |
| --- | --- |
| `models.py` | Pydantic/dataclass-style models and enums used by DB services and agent graphs. Acts as schema of record for the app layer.

## Guidelines

- Keep pure data definitions here; no I/O or DB calls.
- Prefer explicit enums/TypedDicts over loose dicts to reduce runtime errors.
- Backward-compatible changes first (additive), then migrations.
