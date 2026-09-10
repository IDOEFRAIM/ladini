# deploy/releases/ — manifeste de release (état runtime, sur le VPS)

Ces fichiers sont **écrits par `scripts/deploy.sh` / `scripts/rollback.sh`** sur
l'hôte de déploiement. Ils ne sont **pas** versionnés (voir `.gitignore`) —
seuls ce README et `.gitkeep` le sont.

| Fichier        | Rôle |
|----------------|------|
| `current`      | Release **actuellement active**. Format `key=value` (`RELEASE_VERSION`, `GIT_SHA`, `BUILD_TIMESTAMP`, `DEPLOYED_AT`, `DEPLOYED_BY`). |
| `previous`     | **Dernière release connue bonne** avant `current`. Cible par défaut de `rollback.sh` sans argument. |
| `history.log`  | Journal append-only, une ligne par déploiement/rollback : `date \t user \t event \t release \t detail`. |

## Répondre à « quelle version tourne ? » sans GitHub

```bash
cat deploy/releases/current
curl -s http://127.0.0.1:8000/version        # doit matcher RELEASE_VERSION
```

## Rétention des images (rollback toujours possible)

`deploy.sh` conserve **au minimum** les images de `current` **et** `previous`.
Le nettoyage des images plus anciennes est délégué à un `docker image prune`
manuel/cron — voir `docs/runbooks/deployment.md` → « Nettoyage disque ».
