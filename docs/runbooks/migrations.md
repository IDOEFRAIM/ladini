# Runbook — Migrations DB : EXPAND / MIGRATE / CONTRACT

> On doit pouvoir revenir de la release **N** à **N-1** sans découvrir que N a
> détruit la structure attendue par N-1. Règle : **une migration et le code
> qui la consomme ne voyagent jamais ensemble quand la migration est
> destructive.**

---

## Le cycle

```
EXPAND    (release N)    ajouter la structure — COMPATIBLE avec l'ancien code
                         · ADD COLUMN ... NULL   ou   NOT NULL DEFAULT ...
                         · CREATE TABLE IF NOT EXISTS
                         · CREATE INDEX [CONCURRENTLY] IF NOT EXISTS
                         · ADD CONSTRAINT ... NOT VALID   (puis VALIDATE plus tard)
   ↓
DEPLOY    (release N)    ancien code ET nouveau code fonctionnent sur ce schéma
   ↓
MIGRATE   (release N..)  backfill des données (idempotent, par lots)
   ↓
CONTRACT  (release N+k)  SEULEMENT une fois qu'AUCUN code déployé n'utilise
                         plus l'ancienne structure :
                         · DROP COLUMN / DROP TABLE / RENAME / ALTER TYPE
                         + message de commit :  migration-contract-approved: <raison>
```

Interdit **dans une même release** :

- `DROP COLUMN` d'une colonne encore lue/écrite par la version précédente
- `RENAME COLUMN` / `RENAME TO` (destructif : l'ancien code ne trouve plus la colonne)
- `ALTER COLUMN ... TYPE` incompatible
- `ADD COLUMN ... NOT NULL` **sans** `DEFAULT` (échoue sur table non vide + casse l'ancien code)
- `SET NOT NULL` sur une colonne que l'ancien code n'écrit pas

---

## Garde CI

`scripts/check_migrations.sh` tourne sur chaque PR (job `migration-safety` de
`.github/workflows/cicd.yml`). Il scanne le diff de :

- `backend/alembic/versions/*.py` (si/quand Alembic est configuré)
- `backend/migrations/*.py`
- `backend/src/ladini/services/database/common.py` — **le mécanisme de schéma
  ACTUEL** : `SCHEMA_COLUMN_DDL` (tuple d'`ALTER TABLE ... IF NOT EXISTS`)
  appliqué au démarrage du worker via `ensure_performance_indexes()`.

Il **bloque** le merge si une ligne AJOUTÉE contient un motif destructif, sauf
si le commit porte `migration-contract-approved: <raison>` (contraction
assumée, code retiré dans une release antérieure).

Il **classe** aussi (`--classify`) : `ROLLBACK_SAFE` vs
`MIGRATION_REQUIRES_MANUAL_RECOVERY`. `scripts/deploy.sh` et
`scripts/rollback.sh` consomment cette classification.

---

## Marquage dans `/version` et les logs

`scripts/deploy.sh` enregistre la classe de la migration de la release dans
`deploy/releases/history.log` (`mig=ROLLBACK_SAFE` /
`mig=MIGRATION_REQUIRES_MANUAL_RECOVERY`) et l'affiche dans le bloc
`DEPLOYMENT SUCCESS`.

---

## Rollback selon la classe

| Classe | `rollback.sh` | Ce qu'il faut faire |
|---|---|---|
| `ROLLBACK_SAFE` | fonctionne seul | rien de plus |
| `MIGRATION_REQUIRES_MANUAL_RECOVERY` | **s'interrompt** avec avertissement | `docs/runbooks/database-restore.md` (restaurer un dump antérieur), puis redéployer l'ancienne release. `ROLLBACK_FORCE=1` pour outrepasser si on a la certitude que la DB est compatible. |

**Ne jamais promettre un rollback automatique quand il est faux.** C'est
pourquoi App-rollback et DB-rollback sont traités séparément (§43).

---

## Quand Alembic sera branché

Aujourd'hui il n'y a **pas** d'Alembic : le schéma initial est créé hors
migration et les colonnes ajoutées le sont via `SCHEMA_COLUMN_DDL`
(idempotent). `scripts/deploy.sh` détecte `backend/alembic.ini` : s'il existe,
il lance `alembic upgrade head` dans un conteneur éphémère **avant** la
bascule ; sinon il saute avec un avertissement.

Bootstrap (hors périmètre de ce chantier, à planifier) :

```bash
cd backend && alembic init alembic
# alembic/env.py : pointer sur settings.DATABASE_URL ; générer une révision
# initiale qui reflète le schéma courant (autogenerate + revue manuelle).
```

Une fois en place, chaque migration suit EXPAND/CONTRACT ci-dessus et le guard
CI scanne aussi `backend/alembic/versions/*.py` (déjà prévu dans le script).
