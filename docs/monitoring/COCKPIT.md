# Cockpit `/admin/monitoring` — architecture finale, confidentialité, exploitation

Plan et audit initial : `MONITORING_COCKPIT_PLAN.md` · définitions des métriques : `METRICS.md`.

## 1. Architecture

```
Webhook ──.delay()──► Celery  ──► process_agent_task ─────────────────────────────────────────► PostgreSQL
 (signal before_task_publish     │  TurnRecorder (ContextVar) ← hooks : SQL, Redis, MCP, LLM, dispatch     intelligence.agent_turns
  pose ladini_enqueued_at)       │  écriture APRÈS l'envoi, timeout court, jamais bloquante                          ├ agent_tool_calls (CASCADE)
                                 └──► Orchestrator.handle → note_final / note_error                                 └ agent_llm_calls  (CASCADE)
Celery Beat ──► workers.agent_telemetry_retention (purge batchée)

Next.js (ladinifront)  /api/admin/monitoring/*  ── agrégations SQL serveur (Drizzle) ──► PostgreSQL
   overview · conversations[/id] · workflows · tools[/name] · performance · business · health · stream (SSE)
   UI : features/monitoring/cockpit/ui  (7 onglets, sélecteur de période global, mode temps réel)
```

**Schéma** : Drizzle est la source (migration additive `0001_agent_telemetry`) ; miroir SQLAlchemy `domain/telemetry.py` vérifié par
`tests/schema/` (types, nullabilité, défauts, FK, index, **contraintes CHECK**). Aucun DDL runtime.

**Pourquoi des read models dans Next.js** : le frontend possède déjà l'authentification admin (`requireAdmin`) et l'accès Drizzle ;
le backend Python n'a pas d'API admin. Chaque vue = une fonction serveur qui agrège en SQL (jamais de dizaines de petites requêtes
côté navigateur).

**Écriture best-effort (compromis)** : écriture *en ligne* après le dispatch, sous timeout (`AGENT_MONITORING_WRITE_TIMEOUT_SECONDS`,
2 s), exceptions avalées et comptées (`write_failures()`), plutôt qu'un `create_task()` non maîtrisé (perdu à l'arrêt du worker) ou un
bus dédié (nouvelle infrastructure). Coût : quelques ms de temps worker, jamais de latence utilisateur. Limite assumée : un crash du
worker entre l'envoi et l'écriture perd le tour.

## 2. Points d'instrumentation (backend)

| Mesure | Point | Fichier |
|---|---|---|
| Tour, issue, intention, workflow, étape | `Orchestrator.handle` (`note_final` / `note_error`) | `orchestrator/orchestrator.py` |
| Cycle du tour, envoi, statut de réponse, durée WhatsApp | `process_agent_task` | `api/tasks.py` |
| File Celery | signal `before_task_publish` (en-tête) + lecture dans le worker | `core/turn_telemetry.py`, `api/celery_app.py` |
| SQL (nb, cumul, max ; jamais texte ni paramètres) | évènements du moteur async | `core/database.py` |
| Redis (nb, cumul) | wrapper unique de `Redis.execute_command` (sync + asyncio) | `core/turn_telemetry.py` |
| Outils MCP (nom, catégorie, durée, statut) | `AgriDBMCPServer.call_tool` (goulot unique) | `infrastructure/mcp/runtime.py` |
| LLM (provider, modèle, durée, repli, tokens ; sans double comptage) | `record_generation` | `core/telemetry.py` |

## 3. Endpoints

`GET /api/admin/monitoring/…` — tous réservés `ADMIN`/`SUPERADMIN` (401/403 avant toute lecture), période `?period=1h|24h|7d|30d|custom&from&to`
(custom ≤ 90 j), pagination `?limit&offset` (≤ 100), filtres validés (400 explicite), `Cache-Control: no-store`, 500 générique (aucun détail interne).

| Endpoint | Contenu |
|---|---|
| `overview` | KPI, funnel, séries, intentions, santé résumée |
| `conversations` (+ `/{id}`) | sessions filtrables ; timeline d'une session |
| `workflows` (`?workflow=`) | parcours + étapes atteintes |
| `tools` (+ `/{name}`) | statistiques par outil ; détail (sans payloads) |
| `performance` | percentiles, fenêtres, waterfall, histogramme, tours lents, providers LLM |
| `business` | KPI business, entonnoir vendeur, métriques non disponibles |
| `health` (`?windowMinutes`) | état par dépendance, seuils appliqués |
| `stream` | SSE : nouveaux tours, compteurs 5 min, changements d'état de santé |

## 4. Confidentialité et rétention

* **Accès** : administrateurs uniquement (garde partagée avec le reste de l'admin).
* **Téléphone** : jamais stocké en clair. `phone_hash` = HMAC-SHA256 avec un poivre (`AGENT_MONITORING_PHONE_PEPPER` ; repli dérivé de
  `DATABASE_URL`) pour regrouper les sessions ; `phone_last4` pour l'affichage `+226 •••• 4582`. **À faire en production : définir un poivre dédié.**
* **Messages** : extraits de 280 caractères maximum, numéros / emails / jetons masqués (`[tel]`, `[email]`, `[jeton]`) ; **rien** n'est conservé
  pour les contextes sensibles (code OTP de livraison, mot de passe : `[contenu masqué]`).
* **Outils** : nom, catégorie, durée, statut, code d'erreur — **aucun argument ni résultat**. **LLM** : provider, modèle, durée, tokens — jamais prompt ni réponse.
* **SQL** : jamais le texte des requêtes ni leurs paramètres.
* **Rétention** : `AGENT_MONITORING_RETENTION_DAYS` (30 par défaut). Purge Celery Beat toutes les 6 h : lots de 2 000 lignes, un COMMIT par lot,
  `lock_timeout` 2 s / `statement_timeout` 20 s, ≤ 100 lots par exécution (le reliquat est repris), valeur ≤ 0 ignorée (ne peut pas vider la table).
* **Écriture** : `agent_turns.user_id` → `auth.users` `ON DELETE SET NULL` (la suppression d'un compte ne casse pas l'historique, qui expire seul).
* **Actions mutantes admin** (reset de session, relance de notification, prise en charge humaine) : **non implémentées** — première version en lecture seule.
  À l'ajout : authentifiées, autorisées (`requireAdmin`), auditées (`intelligence.audit_logs`), idempotentes.

## 5. Configuration

| Variable | Où | Défaut |
|---|---|---|
| `AGENT_MONITORING_ENABLED` | backend | `true` |
| `AGENT_MONITORING_RETENTION_DAYS` | backend | `30` |
| `AGENT_MONITORING_SESSION_GAP_MINUTES` | backend | `30` |
| `AGENT_MONITORING_WRITE_TIMEOUT_SECONDS` | backend | `2.0` |
| `AGENT_MONITORING_PHONE_PEPPER` | backend | (repli dérivé) — **à définir** |
| `MONITORING_THRESHOLDS` | frontend (JSON partiel) | voir `thresholds.ts` |
| `MONITORING_BACKEND_HEALTH_URL` | frontend | non défini → sonde API `UNKNOWN` |
| `LANGFUSE_TRACE_URL_TEMPLATE` | frontend | non défini → pas de lien de trace |

Seuils par défaut : erreurs d'outils ≥ 5 % / 15 % ; erreurs LLM ≥ 5 % / 20 % ; p95 d'un tour ≥ 5 s / 10 s ; SQL p95 ≥ 200 / 800 ms ;
Redis p95 ≥ 100 / 400 ms ; échecs WhatsApp ≥ 2 % / 10 % ; session bloquée 10 min, abandonnée 30 min.

## 6. Mesures de performance du cockpit

`npx tsx scripts/monitoring-bench.ts` (frontend) sur **200 000 tours, 133 000 appels d'outils, 200 000 appels LLM** (30 jours), PostgreSQL local, médiane de 3 :

| Vue | 24 h | 7 j | 30 j |
|---|---:|---:|---:|
| Vue d'ensemble | 130 ms | 530 ms | 3,4 s |
| Conversations (page de 25) | 44 ms | 400 ms | 1,2 s |
| Workflows | 17 ms | 77 ms | 460 ms |
| Outils | 14 ms | 95 ms | 530 ms |
| Performance | 446 ms | 1,2 s | 2,7 s |
| Business | 24 ms | 67 ms | 154 ms |
| Santé (fenêtre 60 min) | 144 ms | | |
| **SSE : un tick de 5 s (2 requêtes)** | **3 ms** | | |

`EXPLAIN ANALYZE` : les agrégats sur 24 h utilisent `agent_turns_created_idx` ; sur 30 j ils balayent toute la table (attendu, 100 % des lignes).
Optimisations appliquées d'après les mesures : percentiles en **un seul tri** (`percentile_cont(ARRAY[…])`, 3× moins de tris), un balayage
au lieu de deux pour les intentions (gain ≈ 2× sur 24 h et 7 j). Flux SSE : lecture ≤ 50 lignes via l'index de date, santé recalculée toutes les 30 s.

**Seuil de décision — rollups** : à ce volume (≈ 6 600 tours/jour) les vues 24 h/7 j restent sous ~1,2 s et 30 j sous ~3,4 s. Une table de rollup
journalière (`agent_turn_daily`) ne se justifie **que** si la vue 30 j dépasse ~5 s (≳ 500 000 tours / 30 j) ; elle n'est donc pas construite (pas de
complexité prématurée). Le volume réel attendu au lancement est très inférieur.

## 7. Risques restants

1. **Poivre du téléphone** non défini en prod → repli dérivé de `DATABASE_URL` (moins isolé). Définir `AGENT_MONITORING_PHONE_PEPPER`.
2. **Workers / Beat sans sonde directe** : leur état est *dérivé* (attente en file, retards d'outbox) et affiché comme tel.
3. **Tour perdu si le worker meurt entre l'envoi et l'écriture** (compromis assumé, §1) ; `write_failures()` n'est pas encore exposé en métrique Prometheus.
4. **Un extrait de message peut contenir une donnée personnelle non détectée** (nom, adresse) : d'où l'accès admin seul, les 280 caractères, et la rétention de 30 j.
5. **Étapes de workflow** : celles réellement rencontrées (`pending_interaction.kind`) ; un but qui se termine en un tour n'a pas de funnel interne.
6. **Sessions** : regroupement par écart de 30 min ; un utilisateur qui change de sujet dans la fenêtre reste dans la même session.
7. **`conversations` / `agent_actions`** (anciennes tables lues par l'ancien écran) restent inutilisées par l'agent ; l'ancien code du composant n'est plus routé.
8. **Migration en production** : `drizzle migrate` (frontend) **avant** de déployer le backend qui écrit ces tables ; la base EU actuelle n'a pas encore `0001`.
