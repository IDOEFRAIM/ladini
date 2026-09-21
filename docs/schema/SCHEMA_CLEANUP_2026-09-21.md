# Nettoyage du schéma — rapport final (2026-09-21)

Branches : `audit/schema-baseline` (état initial) → `fix/schema-cleanup` (ce document). Dépôts : `ladini` (backend) et
`ladinifront` (frontend, Drizzle). État initial : `SCHEMA_AUDIT_BASELINE_2026-09-21.md` + matrice
`SCHEMA_DIVERGENCE_MATRIX_BASELINE_2026-09-21.md`. État final : `SCHEMA_DIVERGENCE_MATRIX_AFTER_2026-09-21.md`.
Architecture cible : `SCHEMA_ARCHITECTURE.md`.

## 1. État initial des divergences

| Comparaison | Avant | Après |
|---|---|---|
| Drizzle ↔ PostgreSQL (migrations rejouées) | 0 | 0 |
| SQLAlchemy ↔ Drizzle | **272** (120 types, 98 défauts, 59 FK, 11 index, 1 unique, 5 tables) | **0** hors 3 tables `seed_*` déclarées site-only (voulu) |
| SQLAlchemy ↔ PostgreSQL | idem | idem |
| Divergences de nullabilité | 0 | 0 |
| FK garanties par PostgreSQL | 28 (19 cœur + 9 `seed_*`) | **91** |
| Tables fantômes (créées par du DDL Python) | 5 | 0 |

La base EU réelle n'a pas servi de référence : vide, sans FK, sans journal de migrations (elle n'a pas été créée par
`drizzle migrate`). **À recréer** depuis le nouveau baseline (voir §13).

## 2. Fichiers modifiés (résumé)

**Frontend (`ladinifront`)** : `src/db/schema/{auth,governance,marketplace,intelligence,relations,index}.ts` (FK, index,
tables mortes retirées), `src/db/schema/runtime.ts` (nouveau), `drizzle/` (baseline unique régénéré),
`drizzle.config.ts`, `package.json`, `scripts/schema-drift-check.mjs` (nouveau), `scripts/baseline-drizzle.ts` et
`scripts/insert_baseline_migration.js` (supprimés), `.github/workflows/schema.yml` (nouveau), `docs/drizzle-migrations.md`.

**Backend (`ladini`)** : `domain/{catalog,governance,identity,intelligence,orders}/models.py` (miroir exact),
`domain/runtime_tables.py` (nouveau), `domain/models.py`, `infrastructure/mcp/{context,__init__,security}.py`,
`services/database/{buyer,common,d,search,mcp_idempotency_store,preorder_draft_store,procurement_draft_store,sales_publish_draft_store}.py`,
`workspace/store.py`, `core/database.py`, `api/tasks.py`, `schema_contract/` (nouveau), `tests/schema/` (nouveau),
`tests/unit/{test_mcp_context,test_workspace_store}.py` (adaptés), `scripts/{check_migrations.sh,test/run-scenarios.sh,test/e2e/bootstrap_db.py}`,
`.github/workflows/cicd.yml`, `pyproject.toml`/`poetry.lock` (+`psycopg2-binary` en dev), docs.

## 3. Tables supprimées

| Table | Pourquoi (usage vérifié dans Python **et** frontend) |
|---|---|
| `public.episodic_memories`, `public.user_farm_profiles` | direction produit « mémoire agronomique » abandonnée ; jamais créées en base. Modèles, `services/memory/`, outils MCP `build_context`/`enrich_state`/`record_interaction`, `MCPContextServer` supprimés. 0 référence résiduelle (test). |
| `governance.overlay_layers`, `zone_metrics`, `zone_settings` | aucune référence hors registre de modèles |
| `intelligence.ai_rating_reasonings`, `agent_context_memory` | idem |
| `marketplace.marketplace_ratings`, `batches` | idem |

Tables **conservées** malgré peu d'usage : `order_disputes` (utilisée par `escrow.py`), `expenses`, `warehouses`,
`stock_movements`, `demand_signals`, `moderation_events`.

## 4. Relations supprimées

Toutes les FK/relations ORM pointant vers les 7 tables mortes (dont `marketplace_ratings.order_id`, 3 relations
`Zone`, relations `TrustScore`/`Order`/`Stock`/`Farm`). Relations ORM retirées des modèles Python correspondants.

## 5. Clés étrangères ajoutées : **64** (91 au total, 27 préexistantes)

Voir `SCHEMA_ARCHITECTURE.md` pour la politique. Répartition finale des `ON DELETE` : 24 `RESTRICT`, 27 `NO ACTION`
(défaut PostgreSQL, équivalent, hérité des 18 FK d'origine), 29 `SET NULL`, 11 `CASCADE`.
Exemples : `auth.sessions/accounts.user_id` CASCADE ; `bids.auction_id`, `orders.buyer_id`, `payments.order_id`,
`deliveries.order_id`, `producers.user_id` RESTRICT ; `products.verified_by_id`, `users.zone_id` SET NULL ;
`solicitations.*` CASCADE (données dérivées) ; `auctions.winner_bid_id` RESTRICT (FK circulaire).
`ON UPDATE` : `NO ACTION` partout (les ids sont immuables).

## 6. Clés étrangères volontairement non ajoutées (catégorie B)

Voir la table dans `SCHEMA_ARCHITECTURE.md`. **Point d'attention réel** : `order_status_history.actor_id` reçoit un
`Producer.id` dans 3 flux de `producer.py` ; contraindre vers `auth.users` les aurait cassés. Non contraint ; à
normaliser (écrire `producer.user_id`) avant d'ajouter la FK.

## 7. Index

Ajoutés (Drizzle + miroir) : 4 GIN trigram (`ix_{products,market_offers,subcategories,zones}_*_trgm`, ex-DDL Python),
3 partiels sur `orders` (`ix_orders_paydunya_token` **unique**, `ix_orders_payment_expires_at`, `ix_orders_checkout_group`),
`bids_one_winner_per_auction_uq` (**unique partiel** : au plus un gagnant par enchère), `user_org_org_idx`,
`auctions_subcategory_idx`, `orders_client_idx`, `agent_actions_user_idx` (FK fréquemment jointes), + 6 index des tables
runtime. Supprimés : ceux des 7 tables mortes. Chaque FK sans index de tête est listée et justifiée dans
`FK_WITHOUT_INDEX_OK` (tables minuscules, jamais filtrées, ou suppression parent rare) ; le test échoue sinon.

## 8. Divergences Drizzle / SQLAlchemy restantes

0, à l'exception documentée des 3 tables `marketplace.seed_*` (site-only). Aucune divergence de type, nullabilité,
défaut, PK, FK, `ON DELETE`, unique, index.

## 9. Migrations produites

Un **baseline unique** `drizzle/0000_baseline.sql` (50 tables, 91 FK, schémas + `pg_trgm` inclus), qui remplace
l'historique `0000…0007`. Copie versionnée : `backend/schema_contract/migrations/`.

## 10. Tests ajoutés (`backend/tests/schema/`, 73 tests)

Cohérence Drizzle/SQLAlchemy/PostgreSQL, reconstruction depuis zéro (reproductible), FK validées, 26 orphelins rejetés,
CASCADE/RESTRICT/SET NULL, ~20 unicités, 6 invariants sous concurrence réelle (gagnant d'enchère unique, commande unique
par enchère, paiement unique par référence, transition de statut CAS, idempotence MCP, outbox), interdiction de DDL
runtime, absence de la fonctionnalité mémoire, requêtes sans N+1. Plus `sync_contract.py` et `audit_report.py`.
Frontend : `db:schema-check`, workflow `schema.yml`.

## 11. Performance

`BuyerMixin.estimate_delivery_cost` : 3 requêtes par produit (dont un parent d'acheteur invariant) → **2 requêtes au
total**. **Non modifiés volontairement** : `create_preorder_draft` (un `SELECT Product` par ligne : un pré-chargement a cassé 15 tests qui simulent ce contrat, gain mineur, revert) et les boucles `SELECT … FOR UPDATE` de `finalize_multi_order`, `confirm_preorder_draft`,
`cancel_pending_order`, `cancel_confirmed_order` — ce sont des acquisitions de verrous ordonnées (anti-deadlock) où un
batch changerait la sémantique de concurrence. Reste dans `producer.py` : pas de N+1 de lecture identifié.

## 12. Résultats de tests et reconstruction

- Backend `tests/schema` : 73 tests, tous verts (1 skip : synchro avec le dépôt frontend, vert quand `LADINI_FRONTEND_DIR` est défini).
- Backend suite complète : seuls échecs = 5 tests **déjà en échec sur `main`** (`test_webhook_role_hint`,
  `test_process_agent_task_message_dedup` ×2, `test_interpreter_langfuse_metadata` ×2). Un pré-chargement que j'avais
  introduit dans `create_preorder_draft` avait cassé 15 tests : revert, puis les 3 fichiers concernés sont verts.
  La suite complète n'a pas été relancée intégralement après ce revert (les 3 fichiers touchés + `tests/schema` l'ont été).
- Frontend : `vitest` 13 fichiers verts (1 skip), `tsc` 0 erreur, `drizzle-kit check` OK, `generate` = « nothing to migrate ».
- Base vide → migrations → seed : 3 cycles OK, rejeu des migrations sans effet, reconstruction reproductible (test).

## 13. Risques et points à surveiller

1. **Recréer la base EU** avec le nouveau baseline (`npm run db:migrate` puis `npm run db:seed`) — ne pas la « migrer ».
2. **FK nouvellement imposées** : tout code qui écrivait un id incohérent échoue désormais au lieu de corrompre en
   silence. J'ai audité les écrivains des colonnes acteur/utilisateur ; seul `order_status_history.actor_id` posait
   problème (laissé non contraint). Les autres flux (sollicitations, outbox, modération, audit) écrivent des ids
   cohérents. Un test d'intégration complet des flux WhatsApp sur base réelle reste recommandé avant la prod.
3. **Statuts en `text` libre** (orders, payments, auctions, bids…) : aucune contrainte CHECK. Recommandation : inventorier
   les valeurs réellement écrites puis ajouter des CHECK (non fait : un oubli de valeur casserait un flux).
4. **Colonnes candidates au retrait** (jamais référencées côté Python ni frontend, non supprimées sans décision produit) :
   `auctions.{quality_grading, required_certifications, preferred_packaging}`, `bids.valid_until`,
   `deliveries.{shipping_condition, proof_of_delivery_url}`, `products.min_order_quality`,
   `conversations.{user_intent, needs_follow_up}`, `solicitations.responded_at`, `clients.prefered_payement_method` (faute
   de frappe), `producers/buyer_profiles.company_registration_number`, `order_disputes.*` (feature non branchée).
   Colonnes `auth.*` : pilotées par NextAuth, à conserver.
5. **Pas de stock négatif interdit en base** : aucun CHECK sur `stocks.quantity` / `products.quantity_for_sale`
   (protégé par `SELECT … FOR UPDATE` applicatif). À envisager après audit des ajustements.
6. **Tests pré-existants instables** (échouent à l'identique sur `main`) : `test_webhook_role_hint`,
   `test_process_agent_task_message_dedup` (clés `msg:wamid.*` laissées dans Redis), `test_interpreter_langfuse_metadata`.
7. **Déploiement** : appliquer la migration Drizzle **avant** le nouveau code backend ; retirer côté prod le DDL au
   démarrage évite le risque de deadlock du 2026-09-19.
