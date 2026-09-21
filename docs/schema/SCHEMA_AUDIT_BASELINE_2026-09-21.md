# Audit de schéma — état initial (avant nettoyage)

Sources comparées (outil : `backend/tests/schema/schema_model.py`, matrice complète dans
`SCHEMA_DIVERGENCE_MATRIX_BASELINE_2026-09-21.md`) :

| Source | Contenu |
|---|---|
| **Drizzle** | snapshot `drizzle/meta/0007_snapshot.json` (frontend) — 52 tables |
| **SQLAlchemy** | `Base.metadata` — 51 tables (49 domaine + 2 modèles « mémoire ») |
| **PostgreSQL** | base **reconstruite depuis les migrations** (PG local) — 52 tables |

> La base EU réelle (`eu-west-1`) n'a **pas** été utilisée comme référence : elle est vide et ne provient pas de
> `drizzle migrate` (0 clé étrangère, pas de table `__drizzle_migrations`, index de la migration 0006 absents,
> extension `pg_stat_statements` seulement). Elle a probablement été créée par `push` ou d'une autre façon. À recréer
> depuis les migrations du fix.

## Résultat chiffré

| Comparaison | Divergences |
|---|---|
| Drizzle ↔ PostgreSQL (migrations rejouées) | **0** — la chaîne de migrations reproduit fidèlement le snapshot |
| SQLAlchemy ↔ Drizzle | **272** (196 colonnes, 59 FK, 11 index, 1 unique, 5 tables) ; 539 éléments identiques |

Détail des 196 divergences de colonnes : 120 **types** (`String`→`varchar` côté Python vs `text` côté Drizzle/PG),
1 défaut de valeur différent (`'other'` vs `'OTHER'`), 98 **server_default** absents côté SQLAlchemy (défauts applicatifs
`default=` seulement) ; **aucune divergence de nullabilité**.

## Tables

| Table | Drizzle | SQLAlchemy | PG | Constat |
|---|---|---|---|---|
| `public.episodic_memories`, `public.user_farm_profiles` | ✗ | ✓ | ✗ | Modèles morts d'une direction abandonnée, jamais créés → à supprimer |
| `marketplace.seed_*` (3) | ✓ | ✗ | ✓ | Spécifiques au site, non utilisées par Python → OK sans miroir |
| `marketplace.preorder_drafts`, `procurement_drafts`, `sales_publish_drafts`, `mcp_idempotency_records`, `public.agri_workspaces` | ✗ | ✗ | (créées au démarrage) | **Tables fantômes** : créées par du DDL Python au démarrage du worker, absentes de Drizzle |

## Mécanismes qui modifient le schéma hors Drizzle (à supprimer)

- `services/database/d.py::ensure_performance_indexes` (appelé par `api/tasks.py::_ensure_schema` au démarrage du
  worker) exécute : `PERFORMANCE_INDEX_DDL` (extension `pg_trgm` + 4 index GIN), `SCHEMA_COLUMN_DDL` (ALTER TABLE ADD
  COLUMN + index partiels), et le DDL de 4 tables (drafts, idempotence).
- `workspace/store.py` : `CREATE TABLE IF NOT EXISTS agri_workspaces` + `ALTER TABLE` à l'exécution.
- `core/database.py::ensure_extensions` : `CREATE EXTENSION pg_trgm, vector, pgcrypto, uuid-ossp`.
- `Base.metadata.create_all` : **aucune occurrence** (bon).

## Clés étrangères (59 déclarées en Python, absentes de Drizzle/PG)

Les 19 FK « cœur » + 9 FK `seed_*` de Drizzle sont identiques à Python. Les 59 autres n'existent que dans le modèle
ORM : la base n'impose donc aucune intégrité référentielle sur `auth`, `governance`, sur les enchères/offres, le stock,
les livraisons, `intelligence`, etc. La classification A/B/C est faite dans le document du fix.

## Usage réel (tables/colonnes)

Tables sans **aucune** référence hors registre de modèles, côté Python **et** frontend : `governance.overlay_layers`,
`governance.zone_metrics`, `governance.zone_settings`, `intelligence.ai_rating_reasonings`,
`intelligence.agent_context_memory`, `marketplace.marketplace_ratings`, `marketplace.batches`.
Colonnes jamais référencées (aucun des deux côtés) — candidats, **non supprimés sans décision produit** : voir le
document du fix. Les colonnes `auth.accounts/sessions/users` sont pilotées par l'adaptateur NextAuth : à conserver.

## Contraintes / enums

Aucun enum PostgreSQL, aucune contrainte CHECK dans les trois sources : les statuts sont des `text`/`varchar` libres.
Aucune divergence d'enum possible aujourd'hui ; c'est aussi un risque (valeurs invalides acceptées).
