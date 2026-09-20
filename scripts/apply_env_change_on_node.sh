#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/apply_env_change_on_node.sh — applique un changement de
# variable D'ENV RÉEL sur le node (2026-09-20, cutover Upstash → Valkey) :
# corrige un trou de la procédure précédente, qui régénérait
# LADINI_APP_ENV_B64 (secret GitHub) SANS jamais garantir que le fichier
# `.env` RÉELLEMENT lu par `docker compose` sur le node avait la nouvelle
# valeur — deux sources de vérité qui pouvaient diverger silencieusement.
#
#   sudo bash scripts/apply_env_change_on_node.sh \
#     REDIS_URL='redis://:NEWSECRET@10.200.0.2:6379/0' \
#     WIREGUARD_REQUIRED=1
#
# Ordre GARANTI par ce script : (1) backup horodaté de .env AVANT toute
# modification, (2) upsert des variables demandées (jamais un doublon —
# une variable déjà présente est REMPLACÉE, pas dupliquée), (3)
# permissions 600, (4) vérification REDACTÉE (jamais le secret en clair,
# ni dans la sortie de ce script ni dans un log). Ce script NE RECRÉE
# AUCUN conteneur — c'est délibéré (voir §Q du runbook de cutover) :
# recréer worker/api/beat/flower est une étape SÉPARÉE, dans un ordre
# précis, après que CE script ait confirmé le fichier correctement écrit.
#
# Usage : VAR=valeur [VAR2=valeur2 ...] en arguments positionnels
# "CLE=valeur". Le fichier ciblé : $ENV_FILE (défaut :
# /opt/ladini/app/.env, cohérent avec NODE_DEPLOY_DIR utilisé par
# scripts/cluster_deploy.sh).
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_DEPLOY_DIR="${NODE_DEPLOY_DIR:-/opt/ladini/app}"
ENV_FILE="${ENV_FILE:-${NODE_DEPLOY_DIR}/.env}"

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 CLE=valeur [CLE2=valeur2 ...]" >&2
  exit 1
fi

[ -f "$ENV_FILE" ] || { echo "FATAL: ${ENV_FILE} introuvable." >&2; exit 1; }

# ── 1. Backup horodaté AVANT toute modification ────────────────────
BACKUP_PATH="${ENV_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_FILE" "$BACKUP_PATH"
chmod 600 "$BACKUP_PATH"
echo "✓ Backup : ${BACKUP_PATH}"

# ── 2. Upsert de chaque variable demandée ───────────────────────────
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
cp -p "$ENV_FILE" "$TMP_ENV"

for kv in "$@"; do
  case "$kv" in
    *=*) : ;;
    *) echo "FATAL: argument invalide (attendu CLE=valeur) : ${kv}" >&2; exit 1 ;;
  esac
  key="${kv%%=*}"
  value="${kv#*=}"
  if grep -qE "^${key}=" "$TMP_ENV"; then
    # Remplace la ligne existante — jamais un doublon. `sed` avec un
    # délimiteur `#` (pas `/`) : une URL contient des `/`, qui casseraient
    # une substitution `s/.../.../ ` classique.
    sed -i "s#^${key}=.*#${key}=${value}#" "$TMP_ENV"
    echo "✓ ${key} : mis à jour (valeur précédente écrasée, backup ci-dessus si besoin de comparer)"
  else
    printf '%s=%s\n' "$key" "$value" >> "$TMP_ENV"
    echo "✓ ${key} : ajouté"
  fi
done

install -m 600 "$TMP_ENV" "$ENV_FILE"
echo "✓ ${ENV_FILE} écrit, permissions 600"

# ── 3. Vérification REDACTÉE — jamais le secret en clair ────────────
echo
echo "═══ Vérification (redacted) ═══"
for kv in "$@"; do
  key="${kv%%=*}"
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1)"
  if [[ "$key" == "REDIS_URL" || "$key" == *_URL || "$key" == *PASSWORD* || "$key" == *SECRET* || "$key" == *TOKEN* ]]; then
    # Redaction : scheme://host:port uniquement pour une URL avec
    # credentials, "<défini>"/"<absent>" pour un secret nu.
    redacted="$(printf '%s' "$line" | sed -E "s#^${key}=(rediss?://)([^@]*@)?([^/]*).*#${key}=\\1***@\\3#")"
    if [ "$redacted" = "$line" ]; then
      # Pas une URL rediss?:// — probablement un secret nu (password/token).
      if [ -n "${line#${key}=}" ]; then
        redacted="${key}=<défini, non affiché>"
      else
        redacted="${key}=<absent>"
      fi
    fi
    echo "  ${redacted}"
  else
    echo "  ${line}"
  fi
done
echo
echo "✓ Fichier appliqué. Étape suivante : mettre EXACTEMENT le même contenu"
echo "  dans LADINI_APP_ENV_B64 (voir docs/REDIS_VALKEY_MIGRATION_2026-09-20.md §11)"
echo "  AVANT de recréer worker/api/beat/flower — sinon le PROCHAIN déploiement"
echo "  GitHub Actions écraserait ce .env avec l'ancienne valeur."
echo "  Recréation des conteneurs : voir docs/REDIS_VALKEY_PRODUCTION_CUTOVER_2026-09-20.md §Q."
