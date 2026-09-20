#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/check_redis_target_consistency.sh — confirme que TOUS les
# services Redis-dépendants (api, worker, beat, flower par défaut)
# ciblent EXACTEMENT le même host:port Redis/Valkey (2026-09-20, cutover
# Upstash → Valkey) — ferme le trou identifié : régénérer
# LADINI_APP_ENV_B64 et écrire .env sur le node ne GARANTIT PAS, à lui
# seul, que chaque conteneur a effectivement redémarré avec la nouvelle
# valeur (un conteneur pas encore recréé garde SON environnement figé au
# moment de son propre démarrage).
#
#   bash scripts/check_redis_target_consistency.sh
#   bash scripts/check_redis_target_consistency.sh --service api --service worker
#
# N'affiche JAMAIS le mot de passe ni l'URL complète : chaque conteneur
# calcule LUI-MÊME `scheme://host:port` depuis SA PROPRE variable
# REDIS_URL (jamais lue par ce script hôte) via un court script Python
# (urlsplit, ne conserve que scheme/hostname/port) — seul ce résultat
# redacté traverse `docker compose exec` vers ce script.
#
# Sortie : 0 si tous les services interrogés ciblent le MÊME host:port ;
# non-zéro sinon (divergence détectée, ou un service injoignable).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

SERVICES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICES+=("${2:?}"); shift 2 ;;
    *) echo "Option inconnue : $1" >&2; exit 1 ;;
  esac
done
if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api worker beat flower)
fi

PY_SNIPPET='
import os
from urllib.parse import urlsplit
url = os.environ.get("REDIS_URL", "")
parts = urlsplit(url)
print(f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.hostname else "ABSENT")
'

declare -A TARGETS
FAIL=0

echo "═══ Cible Redis résolue PAR CHAQUE conteneur (redacté, host:port uniquement) ═══"
for svc in "${SERVICES[@]}"; do
  if ! dc ps -q "$svc" 2>/dev/null | grep -q .; then
    echo "  ${svc}: (conteneur non démarré — sauté)"
    continue
  fi
  target="$(dc exec -T "$svc" python -c "$PY_SNIPPET" 2>/dev/null | tr -d '\r')"
  if [ -z "$target" ]; then
    echo "  ${svc}: ERREUR (impossible d'interroger ce conteneur — python absent de l'image, ou service down)"
    FAIL=1
    continue
  fi
  echo "  ${svc}: ${target}"
  TARGETS["$svc"]="$target"
done
echo

echo "═══ Cohérence ═══"
REFERENCE_SVC=""
REFERENCE_TARGET=""
for svc in "${!TARGETS[@]}"; do
  if [ -z "$REFERENCE_SVC" ]; then
    REFERENCE_SVC="$svc"
    REFERENCE_TARGET="${TARGETS[$svc]}"
    continue
  fi
  if [ "${TARGETS[$svc]}" != "$REFERENCE_TARGET" ]; then
    echo "  ✗ DIVERGENCE : '${svc}' cible '${TARGETS[$svc]}', mais '${REFERENCE_SVC}' cible '${REFERENCE_TARGET}'"
    echo "    → un conteneur n'a probablement pas encore été recréé avec la nouvelle REDIS_URL."
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ] && [ -n "$REFERENCE_TARGET" ]; then
  echo "  ✓ Tous les services interrogés ciblent la même destination : ${REFERENCE_TARGET}"
elif [ -z "$REFERENCE_TARGET" ]; then
  echo "  ? Aucun service interrogeable (tous down ou non démarrés) — rien à comparer"
  FAIL=1
fi

exit "$FAIL"
