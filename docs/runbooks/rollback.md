# Runbook — Rollback Ladini

> Une release casse. On revient à la **dernière release connue bonne** en
> quelques commandes, de manière **déterministe**. Pas de bricolage SSH.

---

## TL;DR

```bash
cd "$DEPLOY_DIR"
./scripts/rollback.sh                 # → deploy/releases/previous
```

ou release explicite :

```bash
./scripts/rollback.sh sha-b921ea7
```

Sortie attendue :

```
╔══════════════════════════════════════════════════════════════════╗
  ROLLBACK SUCCESS
  Now running : sha-b921ea7
  Was         : sha-a83f6c1
  Health      : OK   (/health/ready = 200)
  Smoke       : OK
  Note        : APP rollback only — la base de données n'a pas été modifiée.
╚══════════════════════════════════════════════════════════════════╝
```

`rollback.sh` : prend le **verrou** de déploiement, `pull` l'ancienne image,
`up -d` (recréation), attend `/health/ready`, lance `smoke.sh`, puis réécrit
`deploy/releases/current` / `previous`.

---

## APP ROLLBACK ≠ DATABASE ROLLBACK — le point critique

`rollback.sh` revient au **code** précédent. Il **ne touche jamais** à la base
de données (§43 du cahier des charges). Deux cas :

| Situation | Le rollback applicatif suffit ? |
|---|---|
| La release cassée n'a fait **aucune** migration, ou seulement des migrations **EXPAND** (ajout de colonne nullable / `NOT NULL DEFAULT`, `CREATE … IF NOT EXISTS`) | **Oui.** L'ancien code fonctionne sur le schéma élargi (il ignore les nouvelles colonnes). |
| La release cassée a fait une migration **destructive** : `DROP COLUMN`, `DROP TABLE`, `RENAME`, `ALTER … TYPE`, `SET NOT NULL` sur une colonne que l'ancien code n'écrit pas | **NON.** L'ancien code attend l'ancien schéma. Revenir au code seul = 500 en boucle. → `docs/runbooks/database-restore.md`. |

Le guard CI `scripts/check_migrations.sh` **bloque** une migration destructive
dans la même release que le code qui en dépend (modèle EXPAND / MIGRATE /
CONTRACT). `rollback.sh` **détecte** en plus, au moment du rollback, si la
release qu'on quitte contenait une migration classée
`MIGRATION_REQUIRES_MANUAL_RECOVERY` et **s'interrompt** avec un avertissement
(passer `ROLLBACK_FORCE=1` pour outrepasser si on sait que la DB est compatible).

---

## Rollback automatique pendant un `deploy.sh`

Si `deploy.sh` échoue **après** `docker compose up` (conteneurs démarrés mais
`/health/ready` KO ou `smoke.sh` KO) **et** qu'une release précédente est
enregistrée **et** `AUTO_ROLLBACK=1` (défaut) : `deploy.sh` re-`pull` +
re-`up` la release précédente et re-vérifie `/health/ready`. Le résultat est
tracé dans `deploy/releases/history.log` :

- `deploy-failed-autorollback` : la prod tourne de nouveau sur l'ancienne release.
- `deploy-failed-rollback-uncertain` : le rollback auto lui-même n'a pas
  confirmé la santé → **INCIDENT**, suivre `docs/runbooks/incident.md`.

Pour désactiver le rollback auto (diagnostiquer l'état cassé) :

```bash
AUTO_ROLLBACK=0 ./scripts/deploy.sh sha-XXXX
```

---

## Et si `rollback.sh` lui-même échoue ?

Presque toujours : l'image de la release cible n'est plus au registry
(politique de rétention = `current` + `previous` seulement).

```bash
# lister ce qui est encore tirable
docker image ls | grep ladini-
# choisir une release encore présente et forcer
./scripts/rollback.sh <release-encore-presente>
```

Si **aucune** ancienne image n'est disponible localement ni au registry :
relancer un **build** de l'ancien commit via *release.yml* (`workflow_dispatch`
avec `ref: <ancien-sha>`), puis redéployer.

---

## Après un rollback

1. Confirmer : `curl -s http://127.0.0.1:8000/version` == la release cible.
2. Ouvrir un post-mortem : quelle release, quel `Stage`, quelle `Reason`
   (dans `history.log` et les logs `docker compose logs`).
3. Corriger sur une branche, PR, CI verte, nouvelle release, redéploiement.
   **Ne jamais** re-déployer le même tag « corrigé à la main ».
