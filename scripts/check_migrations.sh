#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/check_migrations.sh — garde EXPAND / MIGRATE / CONTRACT (§11).
#
#   ./scripts/check_migrations.sh <base_ref> <head_ref>            # mode CI (bloquant)
#   ./scripts/check_migrations.sh <base_ref> <head_ref> --classify # echo ROLLBACK_SAFE|MIGRATION_REQUIRES_MANUAL_RECOVERY
#
# Ce qui est inspecté dans le diff <base>..<head> :
#   - backend/schema_contract/migrations/*.sql (copie des migrations Drizzle — LE mécanisme de
#     schéma : Drizzle définit et migre ; le backend n'exécute plus aucun DDL)
#   - backend/alembic/versions/*.py (si/quand Alembic sera configuré)
#   - tout autre .py sous backend/migrations/
#
# Motifs DESTRUCTIFS refusés dans une release (car ils cassent le rollback
# applicatif : l'ancien code attend l'ancien schéma) :
#   DROP TABLE / DROP COLUMN / DROP CONSTRAINT / DROP INDEX
#   RENAME COLUMN / RENAME TO / ALTER COLUMN ... TYPE
#   ADD COLUMN ... NOT NULL  sans  DEFAULT   (échoue sur table non vide OU
#                                             invalide l'ancien code qui n'écrit pas la colonne)
#
# EXPAND autorisé sans réserve :
#   ADD COLUMN ... (nullable, ou NOT NULL DEFAULT ...), CREATE TABLE IF NOT EXISTS,
#   CREATE INDEX [CONCURRENTLY] IF NOT EXISTS, ADD CONSTRAINT ... NOT VALID
#
# Échappatoire (contraction ASSUMÉE, dans une release ULTÉRIEURE au retrait
# du code qui l'utilisait) : mettre dans le message de commit OU dans le
# fichier de migration la ligne exacte :
#       migration-contract-approved: <raison courte>
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

BASE="${1:-}"; HEAD="${2:-HEAD}"; MODE="${3:-ci}"
[ -n "$BASE" ] || { echo "usage: $0 <base_ref> <head_ref> [--classify]" >&2; exit 2; }
[ "$MODE" = "--classify" ] && MODE="classify"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Fichiers "migration" modifiés/ajoutés dans le diff
mapfile -t CHANGED < <(git diff --name-only "${BASE}..${HEAD}" -- \
  'backend/schema_contract/migrations/*.sql' \
  'backend/alembic/versions/*.py' \
  'backend/migrations/*.py' 2>/dev/null || true)
emit_safe()   { [ "$MODE" = classify ] && { echo "ROLLBACK_SAFE"; exit 0; }; echo "✓ migrations: ROLLBACK_SAFE (aucune contraction destructive)"; exit 0; }
emit_unsafe() {
  if [ "$MODE" = classify ]; then echo "MIGRATION_REQUIRES_MANUAL_RECOVERY"; exit 0; fi
  echo "::error::Migration destructive détectée dans la même release que le code."
  printf '%s\n' "$@" >&2
  cat >&2 <<'EOF'

  → Découpez en EXPAND / CONTRACT :
      release N   : ajoute la nouvelle structure, le code lit ancien+nouveau
      release N+1 : backfill
      release N+2 : retire l'ancienne structure  (+ 'migration-contract-approved:' dans le commit)

  Si cette contraction est VOLONTAIRE et que le code qui utilisait l'ancienne
  structure a DÉJÀ été retiré dans une release précédente, ajoutez au message
  de commit :   migration-contract-approved: <raison>
EOF
  exit 1
}

[ "${#CHANGED[@]}" -eq 0 ] && emit_safe

# Échappatoire : contraction explicitement approuvée
if git log --format='%B' "${BASE}..${HEAD}" | grep -qiE '^\s*migration-contract-approved:'; then
  [ "$MODE" = classify ] && { echo "MIGRATION_REQUIRES_MANUAL_RECOVERY"; exit 0; }
  echo "⚠ migrations: contraction APPROUVÉE explicitement (migration-contract-approved dans le commit) — laissez passer, mais le rollback applicatif seul ne suffit pas."
  exit 0
fi

DESTRUCTIVE_RE='(DROP[[:space:]]+(TABLE|COLUMN|CONSTRAINT|INDEX))|(RENAME[[:space:]]+(COLUMN|TO))|(ALTER[[:space:]]+COLUMN[[:space:]]+[A-Za-z0-9_"]+[[:space:]]+TYPE)|(ALTER[[:space:]]+COLUMN[[:space:]]+[A-Za-z0-9_"]+[[:space:]]+SET[[:space:]]+NOT[[:space:]]+NULL)'
findings=()

for f in "${CHANGED[@]}"; do
  # uniquement les LIGNES AJOUTÉES dans ce diff
  added="$(git diff "${BASE}..${HEAD}" -- "$f" | sed -n 's/^+//p' | grep -v '^+' || true)"
  [ -z "$added" ] && continue

  while IFS= read -r line; do
    up="$(printf '%s' "$line" | tr '[:lower:]' '[:upper:]')"
    if printf '%s' "$up" | grep -qE "$DESTRUCTIVE_RE"; then
      findings+=("$f: $line")
    fi
    # ADD COLUMN ... NOT NULL sans DEFAULT
    if printf '%s' "$up" | grep -qE 'ADD[[:space:]]+COLUMN.*NOT[[:space:]]+NULL' \
       && ! printf '%s' "$up" | grep -qE 'DEFAULT'; then
      findings+=("$f (NOT NULL sans DEFAULT): $line")
    fi
  done <<< "$added"
done

[ "${#findings[@]}" -eq 0 ] && emit_safe
emit_unsafe "${findings[@]}"
