#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/compute_valkey_maxmemory.sh — calcule une valeur `maxmemory`
# RAISONNABLE pour Valkey à partir de la RAM RÉELLE de la machine
# (2026-09-20, cutover Upstash → Valkey) — remplace la valeur hardcodée
# `1gb` du premier jet de ce chantier, qui supposait une taille d'instance
# EC2 sans jamais la vérifier.
#
#   bash scripts/compute_valkey_maxmemory.sh                # à exécuter SUR l'EC2 Valkey
#   bash scripts/compute_valkey_maxmemory.sh --apply         # calcule ET écrit valkey.conf (voir garde ci-dessous)
#
# Lit /proc/meminfo (MemTotal) — plus fiable/portable que de parser la
# sortie de `free` (dont le format varie selon les locales/versions).
#
# ── Règle de headroom ────────────────────────────────────────────────
# Réserve pour : le noyau Linux + WireGuard (léger, kernel-space) +
# redis_exporter (quelques Mo) + FRAGMENTATION mémoire Valkey (`INFO
# memory` → `mem_fragmentation_ratio`, souvent 1.0-1.5x sur un dataset
# actif) + le `fork()` d'AOF rewrite (§ POINT CRITIQUE : un `fork()`
# Linux partage les pages mémoire en copy-on-write, mais toute page
# modifiée PENDANT la réécriture est dupliquée — sur un dataset à forte
# écriture, l'utilisation résidente peut transitoirement approcher 2x
# `maxmemory` le temps de la réécriture). Une instance EC2 dédiée à
# Valkey ne fait tourner presque rien d'autre, mais CE headroom-là est le
# plus dangereux à sous-estimer : un OOM killer Linux qui tue Valkey
# PENDANT un rewrite AOF est le pire scénario (processus tué, AOF corrompu
# potentiellement en cours d'écriture).
#
# Fraction retenue (conservatrice par palier — headroom relatif plus
# généreux sur une petite instance, où la marge absolue est de toute façon
# faible) :
#   RAM ≤ 2 Go   → 40% de la RAM totale
#   2-8 Go       → 50% de la RAM totale
#   RAM > 8 Go   → 60% de la RAM totale
# Toujours `maxmemory-policy noeviction` (voir docs/REDIS_VALKEY_
# PRODUCTION_CUTOVER_2026-09-20.md §E) — jamais une éviction silencieuse
# de tâches Celery/verrous d'idempotence sous pression mémoire.
#
# Surchargeable : VALKEY_MAXMEMORY_FRACTION=0.5 bash scripts/compute_valkey_maxmemory.sh
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

VALKEY_CONF="${VALKEY_CONF:-/etc/valkey/valkey.conf}"
# Overridable uniquement pour les tests de régression
# (scripts/test/test-compute-valkey-maxmemory.sh) : pointe la lecture RAM
# sur un fixture au lieu du vrai /proc/meminfo.
MEMINFO_PATH="${MEMINFO_PATH:-/proc/meminfo}"

if [ ! -r "$MEMINFO_PATH" ]; then
  echo "FATAL: ${MEMINFO_PATH} illisible — ce script cible un hôte Linux (l'EC2 Valkey)." >&2
  exit 1
fi

# `|| true` : sous `set -e`, si `MemTotal:` est absente de $MEMINFO_PATH
# (fichier malformé/inattendu), `grep` sort en erreur et tuerait ce script
# silencieusement ICI, AVANT même d'atteindre le message FATAL explicite
# juste en dessous — même classe de bug que celle documentée dans
# scripts/preflight.sh (§4sexies, incident réel 2026-09-20). `MEM_TOTAL_KB`
# reste vide dans ce cas, et le check suivant le rapporte proprement.
MEM_TOTAL_KB="$(grep -E '^MemTotal:' "$MEMINFO_PATH" | awk '{print $2}' || true)"
[ -n "$MEM_TOTAL_KB" ] || { echo "FATAL: impossible de lire MemTotal depuis ${MEMINFO_PATH}" >&2; exit 1; }
MEM_TOTAL_BYTES=$((MEM_TOTAL_KB * 1024))
MEM_TOTAL_MB=$((MEM_TOTAL_KB / 1024))
MEM_TOTAL_GB_X10=$((MEM_TOTAL_MB * 10 / 1024))   # 1 décimale, en entier (évite bc/dépendance externe)

echo "→ RAM totale détectée : ${MEM_TOTAL_MB} Mo ($(( MEM_TOTAL_GB_X10 / 10 )).$(( MEM_TOTAL_GB_X10 % 10 )) Go)"

if [ -n "${VALKEY_MAXMEMORY_FRACTION:-}" ]; then
  FRACTION="$VALKEY_MAXMEMORY_FRACTION"
  echo "→ Fraction forcée via VALKEY_MAXMEMORY_FRACTION=${FRACTION}"
elif [ "$MEM_TOTAL_MB" -le 2048 ]; then
  FRACTION="0.40"
  echo "→ RAM ≤ 2 Go : fraction 40% (headroom large — marge absolue faible sur une petite instance)"
elif [ "$MEM_TOTAL_MB" -le 8192 ]; then
  FRACTION="0.50"
  echo "→ 2-8 Go : fraction 50%"
else
  FRACTION="0.60"
  echo "→ RAM > 8 Go : fraction 60%"
fi

# Calcul entier (bytes), en évitant toute dépendance à `bc` :
# maxmemory_bytes = MEM_TOTAL_BYTES * FRACTION, via awk (POSIX, toujours présent).
MAXMEMORY_BYTES="$(awk -v total="$MEM_TOTAL_BYTES" -v frac="$FRACTION" 'BEGIN { printf "%d", total * frac }')"
MAXMEMORY_MB=$((MAXMEMORY_BYTES / 1024 / 1024))

echo "→ maxmemory recommandé : ${MAXMEMORY_MB}mb (${FRACTION} × ${MEM_TOTAL_MB} Mo)"
echo
echo "  maxmemory ${MAXMEMORY_MB}mb"
echo "  maxmemory-policy noeviction"
echo
echo "── Monitoring recommandé AVANT saturation (voir docs/REDIS_VALKEY_PRODUCTION_CUTOVER_2026-09-20.md §J) ──"
echo "  ALERTE WARNING  : redis_memory_used_bytes / redis_memory_max_bytes > 0.75-0.80"
echo "  ALERTE CRITICAL : redis_memory_used_bytes / redis_memory_max_bytes > 0.90"
echo "  (redis_exporter, déjà déployé — infra/exporters/docker-compose.exporters.yml)"

if [ "$APPLY" -eq 1 ]; then
  [ -f "$VALKEY_CONF" ] || { echo "FATAL: ${VALKEY_CONF} introuvable — --apply nécessite un valkey.conf existant." >&2; exit 1; }
  BACKUP="${VALKEY_CONF}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$VALKEY_CONF" "$BACKUP"
  echo
  echo "✓ Backup : ${BACKUP}"
  if grep -qE '^\s*maxmemory\s+' "$VALKEY_CONF"; then
    sed -i "s/^\s*maxmemory\s\+.*/maxmemory ${MAXMEMORY_MB}mb/" "$VALKEY_CONF"
  else
    printf 'maxmemory %smb\n' "$MAXMEMORY_MB" >> "$VALKEY_CONF"
  fi
  if grep -qE '^\s*maxmemory-policy\s+' "$VALKEY_CONF"; then
    sed -i 's/^\s*maxmemory-policy\s\+.*/maxmemory-policy noeviction/' "$VALKEY_CONF"
  else
    echo 'maxmemory-policy noeviction' >> "$VALKEY_CONF"
  fi
  echo "✓ ${VALKEY_CONF} mis à jour — redémarrer Valkey pour appliquer (voir scripts/restart_valkey_safe.sh)."
else
  echo
  echo "(dry-run — relancer avec --apply pour écrire ${VALKEY_CONF})"
fi
