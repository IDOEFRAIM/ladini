# Cockpit `/admin/monitoring` — audit (Phase 0) et plan

## 1. Architecture initiale constatée

**Frontend (`ladinifront`, Next.js + Drizzle)** — `/admin/monitoring` = 5 onglets (Vue d'ensemble, Actions Agents, Conversations,
Santé Agents, Temps Réel). Routes `app/api/monitoring/*` (garde `requireAdmin`), services `features/monitoring/services/*`.
Tout repose sur **deux tables** : `intelligence.agent_actions` (file d'approbation) et `intelligence.conversations`. Le flux SSE
(`/api/monitoring/stream`) interroge ces deux tables toutes les 5 s. « Santé des agents » = agrégats de `agent_actions`.

**Backend (`ladini`)** — l'agent n'écrit **jamais** dans `conversations` ni `agent_actions` (seul `audit_logs`, pour la
vérification acheteur). La page est donc vide par construction.

Pipeline réel d'un tour :

```
webhook (Twilio / WhatsApp Cloud)  ──.delay()──►  Celery (queue interactive)
   └ dédup Redis (msg:{sid})                          process_agent_task            api/tasks.py
        claim_once / get_cached (Redis, sync)           └ Orchestrator.handle        orchestrator/orchestrator.py
                                                              ├ WorkspaceResolver / store (PostgreSQL)
                                                              └ graph.ainvoke (LangGraph)
                                                                   ├ interpréteur (LLM Gateway → Groq/Bedrock)
                                                                   ├ nœuds/flows → MCP  AgriDBMCPServer.call_tool  (SQLAlchemy async)
                                                                   └ rendu de la réponse (LLM éventuel)
                                                     ResponseDispatcher.dispatch → WhatsApp/Twilio    api/response_dispatch.py
```

Points de passage uniques identifiés (là où l'on instrumente, sans toucher à la logique métier) :

| Mesure | Point unique |
|---|---|
| Tour (durée, issue, intention, workflow, étape) | `process_agent_task` + sortie de `Orchestrator.handle` (`final` : `current_goal`, `goal_status`, `status`, `detected_intent`, `interpreter_confidence`, `pending_interaction`, `tool_execution_history`) |
| File Celery | signal `before_task_publish` (en-tête `ladini_enqueued_at`) → `task.request` |
| SQL | évènements du moteur (`before/after_cursor_execute`) posés dans `core/database.py::init_db` |
| Redis | `Redis.execute_command` (redis-py, sync **et** asyncio) : un seul wrapper couvre les ~6 clients indépendants du dépôt |
| MCP | `AgriDBMCPServer.call_tool` (`infrastructure/mcp/runtime.py`) — goulot unique de tous les outils |
| LLM | `telemetry.record_generation` (appelée par le LLM Gateway ET par les adaptateurs Groq/Bedrock → on ne compte que les évènements du gateway quand il englobe l'appel, pour éviter le double comptage) |
| WhatsApp/Twilio | `ResponseDispatcher.dispatch` |

Télémétrie existante : Langfuse (désactivé par défaut) + OTel/Prometheus, `trace_id` déjà propagé API → Celery → graphe → LLM.
Endpoints de santé Python : `/health`, `/health/live`, `/health/ready`, `/metrics`.

## 2. Décisions d'architecture

1. **PostgreSQL = source de vérité du cockpit** ; Langfuse n'est qu'un lien (`trace_id`), jamais une dépendance.
2. **Tables** (Drizzle = source, migration additive `0001`, miroir SQLAlchemy) : `intelligence.agent_turns`,
   `agent_tool_calls`, et `agent_llm_calls` (ajout justifié : « quel provider LLM est lent » exige provider/modèle/fallback par
   appel — agrégés dans `agent_turns` ils seraient inexploitables).
3. **Écriture best-effort, en ligne, APRÈS l'envoi de la réponse** (timeout court, exceptions avalées et comptées) plutôt qu'un
   `create_task()` incontrôlé (perdu à l'arrêt du worker) ou un bus supplémentaire (Redis Stream + consumer = nouvelle
   infrastructure). Coût : quelques ms de temps worker (pas de latence utilisateur). Limite assumée : un crash du worker
   entre l'envoi et l'écriture perd le tour — acceptable pour de l'observabilité ; le compteur d'échecs le rend visible.
4. **Read models côté Next.js (Drizzle, agrégation SQL côté serveur)** sous `/api/admin/monitoring/*`, gardés par `requireAdmin` :
   le frontend possède déjà l'authentification admin et l'accès Drizzle ; Python n'a pas d'API admin. Une requête agrégée par
   vue, pas de N+1.
5. **Confidentialité** : téléphone jamais stocké en clair (`phone_hash` HMAC + `phone_last4` pour l'affichage `+226 •••• 4582`) ;
   extraits de messages redactés et tronqués ; aucun payload d'outil stocké ; rétention 30 j (`AGENT_MONITORING_RETENTION_DAYS`),
   purge batchée par tâche Celery Beat.
6. **Session (conversation)** = suite de tours d'un même utilisateur séparés de moins de 30 min ; `conversation_id` est une clé de
   regroupement calculée (pas de FK : `intelligence.conversations` n'est pas alimentée par l'agent).
7. **Workflow** = `current_goal` de l'agent (catalogue `INTENT_CONFIG`) ; **étape** = `pending_interaction.kind` ;
   état = `goal_status` (`ACTIVE`, `WAITING_INPUT`, `WAITING_CONFIRMATION`, `EXECUTING`, `COMPLETED`, `FAILED`, `INTERRUPTED`).
   « Abandonné » = workflow non terminé et sans tour depuis ≥ N minutes (seuil configurable).
8. **Aucune métrique business sans définition fiable** : chaque formule est documentée ; sinon « non disponible ».

## 3. Phases

0 audit · 1 schéma · 2 instrumentation · 3 read models · 4 vue d'ensemble · 5 conversations · 6 workflows + outils · 7 performance ·
8 business · 9 santé · 10 SSE · 11 sécurité + rétention · 12 tests, mesures, docs. Commits logiques séparés dans chaque dépôt.
