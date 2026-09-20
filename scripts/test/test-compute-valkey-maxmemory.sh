#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-compute-valkey-maxmemory.sh — régression sur
# scripts/compute_valkey_maxmemory.sh (2026-09-20, blocker #3 — ne jamais
# hardcoder `maxmemory 1gb`, détecter la RAM réelle et appliquer une
# fraction prudente par palier, headroom pour fork()/AOF rewrite).
#
# Couvre :
#   Cas A : RAM ≤ 2 Go -> fraction 40%.
#   Cas B : 2-8 Go -> fraction 50%.
#   Cas C : RAM > 8 Go -> fraction 60%.
#   Cas D : maxmemory-policy noeviction toujours recommandé (jamais une
#           autre politique, quelle que soit la taille de RAM).
#   Cas E : VALKEY_MAXMEMORY_FRACTION surcharge le palier automatique.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/compute_valkey_maxmemory.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

_meminfo() {
  local kb="$1" path="${SANDBOX}/meminfo_$$_${RANDOM}"
  printf 'MemTotal:       %d kB\nMemFree:        1000 kB\n' "$kb" > "$path"
  printf '%s' "$path"
}

# Recalcule la valeur ATTENDUE avec la même arithmétique que le script
# (division entière Mo, puis awk*fraction) — évite tout risque de calcul
# à la main erroné dans le test lui-même.
_expected_mb() {
  local mem_total_kb="$1" fraction="$2"
  local mem_total_mb=$((mem_total_kb / 1024))
  awk -v mb="$mem_total_mb" -v frac="$fraction" 'BEGIN { printf "%d", (mb * 1024 * 1024 * frac) / 1024 / 1024 }'
}

echo "═══ Cas A : RAM ≤ 2 Go -> fraction 40% ═══"
MEM_KB=2048000
MEMINFO="$(_meminfo "$MEM_KB")"
EXPECTED_MB="$(_expected_mb "$MEM_KB" 0.40)"
OUT_A="$(MEMINFO_PATH="$MEMINFO" bash "$SCRIPT" 2>&1)"
if grep -qE "fraction 40%" <<<"$OUT_A" && grep -qE "maxmemory recommandé : ${EXPECTED_MB}mb" <<<"$OUT_A"; then
  ok "$((MEM_KB / 1024)) Mo -> 40%, maxmemory=${EXPECTED_MB}mb"
else
  bad "attendu 40%/${EXPECTED_MB}mb, obtenu : $(grep -E 'fraction|recommandé' <<<"$OUT_A")"
fi
echo

echo "═══ Cas B : 2-8 Go -> fraction 50% ═══"
MEM_KB=4096000
MEMINFO="$(_meminfo "$MEM_KB")"
EXPECTED_MB="$(_expected_mb "$MEM_KB" 0.50)"
OUT_B="$(MEMINFO_PATH="$MEMINFO" bash "$SCRIPT" 2>&1)"
if grep -qE "fraction 50%" <<<"$OUT_B" && grep -qE "maxmemory recommandé : ${EXPECTED_MB}mb" <<<"$OUT_B"; then
  ok "$((MEM_KB / 1024)) Mo -> 50%, maxmemory=${EXPECTED_MB}mb"
else
  bad "attendu 50%/${EXPECTED_MB}mb, obtenu : $(grep -E 'fraction|recommandé' <<<"$OUT_B")"
fi
echo

echo "═══ Cas C : RAM > 8 Go -> fraction 60% ═══"
MEM_KB=16384000
MEMINFO="$(_meminfo "$MEM_KB")"
EXPECTED_MB="$(_expected_mb "$MEM_KB" 0.60)"
OUT_C="$(MEMINFO_PATH="$MEMINFO" bash "$SCRIPT" 2>&1)"
if grep -qE "fraction 60%" <<<"$OUT_C" && grep -qE "maxmemory recommandé : ${EXPECTED_MB}mb" <<<"$OUT_C"; then
  ok "$((MEM_KB / 1024)) Mo -> 60%, maxmemory=${EXPECTED_MB}mb"
else
  bad "attendu 60%/${EXPECTED_MB}mb, obtenu : $(grep -E 'fraction|recommandé' <<<"$OUT_C")"
fi
echo

echo "═══ Cas D : maxmemory-policy noeviction toujours présent ═══"
if grep -q "maxmemory-policy noeviction" <<<"$OUT_A" \
   && grep -q "maxmemory-policy noeviction" <<<"$OUT_B" \
   && grep -q "maxmemory-policy noeviction" <<<"$OUT_C"; then
  ok "noeviction recommandé sur les 3 paliers testés"
else
  bad "noeviction manquant sur au moins un palier"
fi
echo

echo "═══ Cas E : VALKEY_MAXMEMORY_FRACTION surcharge le palier ═══"
MEM_KB=2048000
MEMINFO="$(_meminfo "$MEM_KB")"
EXPECTED_MB="$(_expected_mb "$MEM_KB" 0.25)"
OUT_E="$(MEMINFO_PATH="$MEMINFO" VALKEY_MAXMEMORY_FRACTION=0.25 bash "$SCRIPT" 2>&1)"
if grep -q "Fraction forcée via VALKEY_MAXMEMORY_FRACTION=0.25" <<<"$OUT_E" \
   && grep -qE "maxmemory recommandé : ${EXPECTED_MB}mb" <<<"$OUT_E"; then
  ok "surcharge manuelle respectée (0.25 x $((MEM_KB / 1024)) = ${EXPECTED_MB}mb)"
else
  bad "surcharge non respectée : $(grep -E 'Fraction|recommandé' <<<"$OUT_E")"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
