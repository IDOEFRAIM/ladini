# Architecture Agents — DRY Refactoring

## Vue d'ensemble

```
ladini/agents/           ← NOYAU PARTAGÉ (DRY)
├── __init__.py               ← Exports publics
├── dispatcher.py             ← Intent→Action dispatch + execution avec fallback
├── gateway.py                ← DataGateway unifiée (MCP → DB fallback)
├── identity.py               ← Résolution identité phone-first
└── onboarding.py             ← Machine d'état onboarding (COLLECT_NAME → ZONE → CREATE)
```

## Principes

| Principe | Implémentation |
|----------|----------------|
| **DRY** | Un seul point d'identité (`resolve_identity`), un seul onboarding (`run_onboarding_step`) |
| **Résilience** | `DataGateway`: MCP → DB fallback automatique ; `execute_prepared_action`: fallback chaîné |
| **Découplage** | Les agents fournissent des **adaptateurs minces** (LLM callback, state mapping) |
| **Postel's Law** | Accepte tout format d'état, normalise en interne |

## Modules

### 1. `gateway.py` — DataGateway

Facade unique pour l'accès aux données. Supporte 3 stratégies MCP (`call_db`, `call_tool`, `db_client`).

```python
gateway = DataGateway(mcp_runtime=runtime, db_service=db)
user = await gateway.call("get_user_by_phone", phone="+226...")
```

### 2. `identity.py` — Résolution d'identité

Élimine le pattern dupliqué phone-first / user_id.

```python
identity = await resolve_identity(gateway, phone=phone, user_id=uid)
# → identity.user_id, identity.phone, identity.farms, identity.declared_crops
```

### 3. `onboarding.py` — Machine d'état

Steps: `COLLECT_NAME → COLLECT_ZONE → CREATE_PROFILE → DONE`

```python
result = await run_onboarding_step(ob_state, user_text, gateway, llm_extract=callback)
updates = result.to_state_updates()  # → dict prêt à merger dans l'état agent
```

### 4. `dispatcher.py` — Dispatch d'actions

Registry d'intents + préparation + exécution avec fallback.

```python
DISPATCHER.register_action("STOCK_SUMMARY", ActionSpec(...))
prepared = await DISPATCHER.prepare("STOCK_SUMMARY", payload, ctx)
result = await execute_prepared_action(prepared, mcp_runtime=rt)
```

## Intégration par agent

| Agent | Utilise |
|-------|---------|
| **Formation** | `resolve_identity` + `DataGateway` dans `load_profile_node` ; `execute_formation_action` pour tools |
| **MarketCoach** | `run_onboarding_step` + `DataGateway` dans `onboarding_node` ; `MARKET_DISPATCHER` pour actions |

## Flux onboarding unifié

```
Utilisateur arrive (WhatsApp)
    │
    ▼
[phone connue?] ──non──→ ASK_CLARIFICATION (AG-UI FormInput)
    │ oui
    ▼
resolve_identity(gateway, phone=...)
    │
    ├─ resolved=True → PROFILE_LOADED (agent continue)
    │
    └─ is_new_user=True → run_onboarding_step(...)
         │
         COLLECT_NAME → COLLECT_ZONE → CREATE_PROFILE → DONE
```
