# Runbook — Restauration de la base de données

> La base Postgres de Ladini est **externe / managée** (pas un conteneur de
> cette stack). Ce document sert à : (1) **vérifier réellement** ce que le
> fournisseur sauvegarde, (2) savoir **restaurer**, pas seulement cocher une
> case « backup enabled ».
>
> ⚠️ Le rollback applicatif (`scripts/rollback.sh`) **ne restaure jamais** la
> DB. Ce runbook est le seul chemin quand une release a appliqué une migration
> **destructive** (voir `docs/runbooks/incident.md` → *migration failed*, cas B).

---

## 0. Identifier le fournisseur EXACT

```bash
grep -E '^(DB_HOST|DO_DB_HOST)=' .env
```

| Hôte contient…                | Fournisseur         | Console                                   |
|-------------------------------|---------------------|-------------------------------------------|
| `.db.ondigitalocean.com`      | DigitalOcean Managed PG | cloud.digitalocean.com → Databases     |
| `.amazonaws.com` (RDS)        | AWS RDS / Aurora    | console.aws.amazon.com/rds                 |
| `compute-*.amazonaws.com` + `heroku` dans le nom du user | Heroku Postgres | dashboard.heroku.com → Resources → Postgres |
| autre                         | **À documenter ici** | —                                        |

> **TODO opérateur** : dès que le fournisseur réel est confirmé, remplir la
> section « Ce fournisseur » ci-dessous avec les valeurs vérifiées (pas
> supposées). Tant que ce n'est pas fait, considérer qu'**il n'y a pas de
> stratégie de restauration validée**.

---

## 1. Vérifier ce qui existe RÉELLEMENT (à faire maintenant, pas en incident)

Pour **chaque** ligne, noter la réponse observée dans la console du fournisseur :

| Question                         | Où vérifier | Réponse (à remplir) |
|----------------------------------|-------------|---------------------|
| Backups automatiques activés ?   | Console → Backups / Settings | ☐ oui ☐ non |
| Fréquence                        | idem        | ex. quotidien 03:00 UTC |
| Rétention                        | idem        | ex. 7 jours |
| PITR (point-in-time recovery) ?  | idem        | ☐ oui (fenêtre : __ ) ☐ non |
| Dernier backup réussi (date)     | idem        | |
| Restauration testée une fois ?   | ce runbook  | ☐ oui (date) ☐ **jamais** |

Si « PITR : non » **et** « backups : non » → **risque P0** : mettre en place
au minimum un dump `pg_dump` quotidien (section 4) **avant** tout déploiement
comportant une migration.

---

## 2. Dump manuel AVANT une migration risquée (filet systématique)

À lancer juste avant `deploy.sh` si la release contient une migration classée
`MIGRATION_REQUIRES_MANUAL_RECOVERY` (ou par prudence, toujours) :

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker run --rm --network host -e PGPASSWORD="$DB_PASSWORD" postgres:16-alpine \
  pg_dump -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --no-privileges -Fc \
  > "backups/ladini-${STAMP}.dump"
ls -lh "backups/ladini-${STAMP}.dump"        # vérifier une taille plausible (> quelques Ko)
```

> `--network host` pour joindre la DB managée depuis le conteneur jetable.
> `-Fc` = format custom (compressé, restaurable partiellement).
> `backups/` est **git-ignoré** ; le copier hors du VPS (objet storage) si possible.

---

## 3. Restaurer

### 3.a — depuis un dump `pg_dump -Fc`

```bash
# Option prudente : restaurer dans une NOUVELLE base, valider, puis basculer.
NEWDB="ladini_restore_$(date -u +%Y%m%d)"
docker run --rm --network host -e PGPASSWORD="$DB_PASSWORD" postgres:16-alpine \
  psql -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d postgres \
  -c "CREATE DATABASE \"$NEWDB\";"
docker run --rm --network host -e PGPASSWORD="$DB_PASSWORD" -v "$PWD/backups:/b" postgres:16-alpine \
  pg_restore -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d "$NEWDB" \
  --no-owner --no-privileges --clean --if-exists /b/ladini-<STAMP>.dump
```

Puis pointer la stack sur `$NEWDB` : dans `.env`, `DB_NAME=$NEWDB` →
`./scripts/deploy.sh <release-compatible-avec-ce-schema>`.

### 3.b — restauration managée (backup fournisseur / PITR)

Toujours via la **console du fournisseur** (pas de commande générique) :

- **DigitalOcean** : Databases → cluster → *Backups* → *Restore* (crée un
  **nouveau** cluster). Récupérer le nouveau host, mettre à jour `DB_HOST` dans
  `.env`, redéployer.
- **AWS RDS** : *Snapshots* → *Restore snapshot* (nouvelle instance) **ou**
  *Restore to point in time*. Nouveau endpoint → `DB_HOST`.
- **Heroku** : `heroku pg:backups:restore <backup-id> DATABASE_URL --app <app>`
  (⚠️ écrase la base courante) **ou** `heroku pg:copy` vers un nouvel add-on.

> Une restauration managée crée quasi toujours une **nouvelle instance/endpoint**.
> Le seul changement côté Ladini est `DB_HOST` (+ éventuellement `DB_PORT` /
> identifiants) dans `.env`, puis un `deploy.sh`. Rien d'autre.

---

## 4. Mettre en place un dump quotidien (si le managé ne suffit pas)

`crontab -e` sur le VPS :

```cron
17 2 * * *  cd /opt/ladini && bash scripts/db_backup.sh >> /var/log/ladini-backup.log 2>&1
```

`scripts/db_backup.sh` (à créer si ce besoin est confirmé) : le bloc de la
section 2 + rotation (garder 14 fichiers) + upload vers un bucket (S3 /
Spaces / n'importe quel objet-storage — provider-neutral via `aws s3 cp` ou
`rclone`).

---

## 5. Après restauration — vérifier

```bash
curl -s http://127.0.0.1:8000/health/ready | jq .          # database: ok
docker compose -f docker-compose.prod.yml exec pgbouncer \
  psql "host=127.0.0.1 port=6432 user=$DB_USER dbname=$DB_NAME" \
  -c "select count(*) from marketplace.orders;"            # ordre de grandeur attendu
./scripts/smoke.sh
```

Contrôler que la restauration est bien **antérieure** à la migration
destructive : sinon on restaure un schéma déjà cassé.

---

## Ce fournisseur (à compléter après vérification)

```
Fournisseur       : ________________________
Backups auto      : ______  fréquence : ______  rétention : ______
PITR              : ______  fenêtre : ______
Procédure restore : (lien console) ____________________________________
Dernier test de restauration : ______ (date)  par : ______
```
