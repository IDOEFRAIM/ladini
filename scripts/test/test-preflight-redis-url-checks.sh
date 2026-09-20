#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-preflight-redis-url-checks.sh — régression sur les
# nouveaux garde-fous REDIS_URL de scripts/preflight.sh (2026-09-20,
# migration Upstash → Valkey auto-hébergé) : schéma valide (§4ter) et
# placeholder embarqué (§4quater).
#
# preflight.sh en entier a des dépendances lourdes (docker, registry,
# disque, ports, verrou — voir §6-9) hors de portée d'un test unitaire de
# cette seule logique regex ; même discipline que
# scripts/test/test-node-preflight-lock.sh et
# scripts/test/test-predeploy-check-minimal-host.sh : on extrait le motif
# RÉEL depuis le fichier de prod (pas une réimplémentation indépendante qui
# pourrait dériver silencieusement) et on l'applique aux mêmes chaînes que
# preflight.sh (`REDIS_URL=...` complet, comme lu depuis $ENV_FILE).
#
# Couvre :
#   Cas A : redis:// (SANS TLS, ex. Valkey en réseau privé) — schéma ACCEPTÉ
#           — non-régression explicite du risque "rejeter redis:// juste
#           parce qu'on n'utilise plus rediss://".
#   Cas B : rediss:// (AVEC TLS) — schéma ACCEPTÉ, comme avant.
#   Cas C : schéma absent/invalide (http://, valeur vide, typo "redis:/") —
#           REJETÉ.
#   Cas D : placeholder EMBARQUÉ au milieu de l'URL (le format documenté
#           dans .env.example : redis://:CHANGE_ME@valkey-host:6379/0) —
#           DÉTECTÉ, alors que le check générique "commence par change_me"
#           le raterait.
#   Cas E : URL réelle, remplie, sans aucun token d'exemple — AUCUN
#           placeholder détecté (pas de faux positif sur une vraie URL de
#           prod).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFLIGHT="${ROOT}/scripts/preflight.sh"

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

# ── Extraction des motifs RÉELS depuis preflight.sh (pas de duplication
# aveugle) — recherche LITTÉRALE (grep -F) de la ligne, puis sed pour isoler
# le motif entre quotes simples : grep -oE appliquerait sa PROPRE lecture
# regex à un texte qui contient déjà des méta-caractères regex littéraux
# (le "?" de "rediss?"), ce qui casserait l'extraction elle-même.
SCHEME_LINE="$(grep -F "grep -qE '^REDIS_URL=rediss?://'" "$PREFLIGHT" | head -n1)"
SCHEME_PATTERN="$(printf '%s\n' "$SCHEME_LINE" | sed -n "s/.*grep -qE '\(.*\)'.*/\1/p")"

PLACEHOLDER_LINE="$(grep -F "grep -qiE 'change_?me|valkey-host|your-redis-host'" "$PREFLIGHT" | head -n1)"
PLACEHOLDER_PATTERN="$(printf '%s\n' "$PLACEHOLDER_LINE" | sed -n "s/.*grep -qiE '\(.*\)'.*/\1/p")"

if [ -z "$SCHEME_PATTERN" ]; then
  bad "impossible d'extraire le motif de schéma depuis preflight.sh — le fichier a-t-il changé de forme ?"
fi
if [ -z "$PLACEHOLDER_PATTERN" ]; then
  bad "impossible d'extraire le motif de placeholder embarqué depuis preflight.sh — a-t-il été renommé/supprimé ?"
fi
echo

_check_scheme() {
  # Applique le motif EXTRAIT de preflight.sh §4ter, tel quel.
  printf '%s' "$1" | grep -qE "$SCHEME_PATTERN"
}
_check_placeholder() {
  # Applique le motif EXTRAIT de preflight.sh §4quater, tel quel.
  printf '%s' "$1" | grep -qiE "$PLACEHOLDER_PATTERN"
}

echo "═══ Cas A : redis:// (sans TLS) — schéma ACCEPTÉ ═══"
if _check_scheme "REDIS_URL=redis://:realsecret123@10.0.5.12:6379/0"; then
  ok "redis:// est accepté (Valkey en réseau privé, pas de TLS requis)"
else
  bad "RÉGRESSION : redis:// rejeté — preflight.sh ne doit JAMAIS bloquer redis:// juste parce que rediss:// n'est plus utilisé"
fi
echo

echo "═══ Cas B : rediss:// (avec TLS) — schéma ACCEPTÉ ═══"
if _check_scheme "REDIS_URL=rediss://default:realsecret123@managed-redis.example.com:6379/0"; then
  ok "rediss:// reste accepté (Redis managé public avec TLS)"
else
  bad "rediss:// aurait dû être accepté"
fi
echo

echo "═══ Cas C : schéma absent/invalide — REJETÉ ═══"
for bad_url in "REDIS_URL=http://x:y@host:6379/0" "REDIS_URL=" "REDIS_URL=redis:/host:6379/0"; do
  if _check_scheme "$bad_url"; then
    bad "aurait dû être rejeté : '$bad_url'"
  else
    ok "rejeté comme attendu : '$bad_url'"
  fi
done
echo

echo "═══ Cas D : placeholder EMBARQUÉ au milieu de l'URL — DÉTECTÉ ═══"
EMBEDDED="REDIS_URL=redis://:CHANGE_ME@valkey-host:6379/0"
if _check_placeholder "$EMBEDDED"; then
  ok "placeholder embarqué détecté (format .env.example exact : '$EMBEDDED')"
else
  bad "RÉGRESSION : placeholder embarqué NON détecté pour '$EMBEDDED' — le check générique 'commence par change_me' le raterait déjà, §4quater existe pour ça"
fi
if _check_placeholder "REDIS_URL=rediss://default:change_me@your-redis-host:6379/0"; then
  ok "ancien format d'exemple (rediss://.../your-redis-host) toujours détecté"
else
  bad "l'ancien format d'exemple aurait dû être détecté"
fi
echo

echo "═══ Cas E : URL réelle, remplie — AUCUN faux positif ═══"
REAL="REDIS_URL=redis://:aK9$(printf 'x%.0s' $(seq 1 20))@10.0.5.12:6379/0"
if _check_placeholder "$REAL"; then
  bad "faux positif sur une URL de prod réelle sans placeholder : '$REAL'"
else
  ok "aucun faux positif sur une URL de prod réelle remplie"
fi
if _check_scheme "$REAL"; then
  ok "schéma redis:// de cette même URL réelle accepté"
else
  bad "schéma de l'URL réelle aurait dû être accepté"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
