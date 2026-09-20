#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-apply-env-change-on-node.sh — régression sur
# scripts/apply_env_change_on_node.sh (2026-09-20, blocker #2 — la
# procédure précédente régénérait LADINI_APP_ENV_B64 sans jamais garantir
# que .env RÉEL sur le node avait la nouvelle REDIS_URL).
#
# Couvre :
#   Cas A : backup horodaté créé AVANT toute modification, contenu
#           identique à l'original.
#   Cas B : upsert d'une variable EXISTANTE — remplacée, jamais dupliquée.
#   Cas C : upsert d'une variable ABSENTE — ajoutée.
#   Cas D : le mot de passe REDIS_URL n'apparaît JAMAIS dans la sortie du
#           script (seul host:port visible).
#   Cas E : le fichier final contient bien la VRAIE valeur complète
#           (le script redacte sa PROPRE sortie affichée, pas ce qu'il
#           écrit sur disque — le fichier doit rester fonctionnellement
#           correct pour docker compose).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/apply_env_change_on_node.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

mkdir -p "${SANDBOX}/app"
cat > "${SANDBOX}/app/.env" <<'EOF'
REDIS_URL=rediss://default:oldsecret@old-upstash-host.upstash.io:6379/0
DB_HOST=x
EOF
ORIGINAL_CONTENT="$(cat "${SANDBOX}/app/.env")"

SECRET="s3cr3t-VALKEY-do-not-leak"
OUT="$(NODE_DEPLOY_DIR="${SANDBOX}/app" bash "$SCRIPT" \
  "REDIS_URL=redis://:${SECRET}@10.200.0.2:6379/0" "WIREGUARD_REQUIRED=1" 2>&1)"
RC=$?

echo "═══ Cas A : backup créé, contenu identique à l'original ═══"
BACKUP_FILE="$(ls "${SANDBOX}/app/"/.env.bak.* 2>/dev/null | head -n1)"
if [ -n "$BACKUP_FILE" ] && [ "$(cat "$BACKUP_FILE")" = "$ORIGINAL_CONTENT" ]; then
  ok "backup présent et identique à l'original avant modification"
else
  bad "backup absent ou différent : ${BACKUP_FILE:-<aucun>}"
fi
echo

echo "═══ Cas B : REDIS_URL (déjà présent) remplacé, jamais dupliqué ═══"
COUNT_REDIS_URL="$(grep -c '^REDIS_URL=' "${SANDBOX}/app/.env")"
if [ "$COUNT_REDIS_URL" -eq 1 ]; then
  ok "exactement une ligne REDIS_URL= dans le fichier final"
else
  bad "attendu 1 ligne REDIS_URL=, trouvé ${COUNT_REDIS_URL}"
fi
echo

echo "═══ Cas C : WIREGUARD_REQUIRED (absent) ajouté ═══"
if grep -q '^WIREGUARD_REQUIRED=1$' "${SANDBOX}/app/.env"; then
  ok "WIREGUARD_REQUIRED=1 ajouté au fichier"
else
  bad "WIREGUARD_REQUIRED=1 absent du fichier final"
fi
echo

echo "═══ Cas D : le secret n'apparaît JAMAIS dans la sortie du script ═══"
if [ "$RC" -eq 0 ] && ! grep -q "$SECRET" <<<"$OUT"; then
  ok "secret absent de toute la sortie du script (rc=${RC})"
else
  bad "le secret est apparu dans la sortie du script, ou le script a échoué (rc=${RC})"
fi
echo

echo "═══ Cas E : le fichier .env final contient la VRAIE valeur complète ═══"
if grep -q "REDIS_URL=redis://:${SECRET}@10.200.0.2:6379/0" "${SANDBOX}/app/.env"; then
  ok "la vraie URL complète (avec secret) est bien écrite sur disque — le fichier reste fonctionnel pour docker compose"
else
  bad "la vraie valeur n'est pas présente dans le fichier final — cassé pour docker compose"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
