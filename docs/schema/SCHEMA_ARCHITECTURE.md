# Architecture du schéma de données — source de vérité unique

```
Drizzle (dépôt frontend : src/db/schema/*.ts)      ← SEULE définition du schéma PostgreSQL
    │  drizzle-kit generate
    ▼
drizzle/NNNN_*.sql + meta/*_snapshot.json          ← migrations officielles
    │  npm run db:migrate   (déploiement explicite, contrôlé)
    ▼
PostgreSQL

backend/schema_contract/  = copie versionnée (dernier snapshot + migrations + journal)
    │  python backend/tests/schema/sync_contract.py --frontend <repo>
    ▼
SQLAlchemy (domain/*/models.py, domain/runtime_tables.py) = MIROIR vérifié, jamais source
```

## Règles

1. **Le backend Python ne modifie jamais le schéma.** Ni `create_all`, ni `CREATE/ALTER/DROP`, ni extension au
   démarrage. (`ensure_performance_indexes`, `SCHEMA_COLUMN_DDL`, `PERFORMANCE_INDEX_DDL`, `*_SCHEMA_DDL`,
   `ensure_extensions`, `WorkspaceStore._ensure_table` : supprimés. L'exécution de ce DDL à chaque démarrage de
   worker avait provoqué le deadlock de production du 2026-09-19.) Test : `test_no_runtime_ddl_in_backend_source`.
2. **Tout changement de schéma = migration Drizzle**, puis re-synchronisation du contrat, puis miroir SQLAlchemy.
3. **Le miroir est exact** : tables, colonnes, types (`text`, jamais `varchar`), nullabilité, défauts serveur,
   PK, FK (+`ON DELETE`/`ON UPDATE`), uniques, index (+prédicats partiels, méthode GIN). Test :
   `test_sqlalchemy_mirrors_drizzle_exactly` (statique) et `test_sqlalchemy_mirrors_the_real_postgres_schema`.
4. **Tables « site-only »** (`marketplace.seed_*`) : dans Drizzle/PostgreSQL, sans modèle Python, déclarées dans
   `SITE_ONLY_TABLES` (`tests/schema/conftest.py`). Toute autre table Drizzle doit avoir son miroir.
5. **Tables d'état runtime de l'agent** (drafts de précommande/appel d'offres/publication, idempotence MCP,
   workspaces) : déclarées dans Drizzle (`runtime.ts`), accédées en SQL brut, mirroirées en Core
   (`domain/runtime_tables.py`).

## Politique des clés étrangères

| Catégorie | Règle | `ON DELETE` |
|---|---|---|
| **A** — relation métier garantie par PostgreSQL | FK dans Drizzle **et** PostgreSQL **et** SQLAlchemy | `restrict` pour le transactionnel (commandes, paiements, enchères, offres, produits, profils) ; `cascade` pour le purement dérivé (sessions, comptes, appartenances, scores, sollicitations) ; `set null` pour les références facultatives / d'audit |
| **B** — relation logique, volontairement non contrainte | pas de FK, documentée ci-dessous | — |
| **C** — relation obsolète | supprimée avec sa table | — |

Relations **B** (non contraintes, avec raison) :

| Colonne | Raison |
|---|---|
| `order_status_history.actor_id` | **Identifiant polymorphe** : le backend y écrit un `Producer.id` (`producer.py`, paiement à la livraison / annulation) ET des ids d'utilisateur. Une FK vers `auth.users` casserait ces flux. À normaliser (écrire `producer.user_id`) puis contraindre. |
| `orders/auctions/order_disputes/payments.escrow_wallet_id` | identifiant de portefeuille **externe** (fournisseur de paiement) ; aucune table locale. |
| `orders.checkout_group_id` | clé de corrélation, pas une entité. |
| `*_drafts.conversation_id`, `preorder_drafts.order_id` | ids `text` d'état éphémère de conversation. |
| `agent_actions.batch_id/audit_trail_id/validated_by_id`, `conversations.audit_trail_id`, `audit_logs.entity_id` | identifiants de corrélation / polymorphes `text`. |
| `order_items.tier_id` | référence à un palier stocké dans `products.pricing_tiers` (JSONB). |

Relations **C** supprimées : tout ce qui pointait vers les 7 tables mortes (voir le rapport de nettoyage).

## Vérifications automatiques (`backend/tests/schema/`, exécutées en CI)

| Test | Garantit |
|---|---|
| `test_schema_contract_static.py` | SQLAlchemy == Drizzle ; tables mémoire abandonnées absentes du code ; FK indexées (ou justifiées) ; aucun DDL runtime ; aucun enum PG non miroité ; contrat synchronisé avec le dépôt frontend (si `LADINI_FRONTEND_DIR`) |
| `test_schema_postgres.py` | base vide → migrations → == snapshot Drizzle ; SQLAlchemy == PostgreSQL ; reconstruction reproductible ; extensions ; FK validées ; ORM insère sur le vrai schéma |
| `test_referential_integrity.py` | orphelins rejetés (27 FK), CASCADE/RESTRICT/SET NULL réels, unicités critiques |
| `test_sales_invariants.py` | sous concurrence réelle : un seul gagnant d'enchère, une seule commande par enchère, un seul paiement par référence fournisseur, transition de statut appliquée une fois, idempotence MCP et outbox exactement-une-fois |
| `test_query_efficiency.py` | pas de N+1 (nombre de requêtes constant) et résultat exact |

CI : `.github/workflows/cicd.yml` (job `test`, PostgreSQL 16 en service, `REQUIRE_SCHEMA_DB=1` : jamais de skip
silencieux). Côté frontend : `.github/workflows/schema.yml` (dérive, base vide → migrations → rejeu no-op → seed).
