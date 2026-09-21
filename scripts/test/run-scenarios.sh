#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/run-scenarios.sh — exécute les scénarios §45 du cahier des
# charges contre un FAUX docker/curl (aucun démon requis). Valide la LOGIQUE
# de deploy.sh / rollback.sh / preflight.sh / check_migrations.sh.
#
# Un vrai test E2E (vraies images, vrai VPS) reste nécessaire — voir
# docs/runbooks/deployment.md. Ici on prouve le flux de décision.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
BIN="${SANDBOX}/bin"; mkdir -p "$BIN"
PASS=0; FAIL=0
ok()   { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

# ── Faux binaires ────────────────────────────────────────────────
cp "${ROOT}/scripts/test/mock-docker.sh" "${BIN}/docker"; chmod +x "${BIN}/docker"

cat > "${BIN}/curl" <<'EOF'
#!/usr/bin/env bash
# Faux curl — couvre exactement les formes utilisées par smoke.sh / lib.sh :
#   -s -o /dev/null -w '%{http_code}' URL   → imprime le code, exit 0
#   -s -w '\n%{http_code}' URL              → imprime  body<LF>code, exit 0
#   -fsS URL / -f -s -S URL                 → imprime body ; exit 22 si non-2xx
#   -s URL                                  → imprime body, exit 0
args=("$@"); url=""; has_w=0; w_is_codeonly=0; has_o_null=0; has_f=0
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    http://*|https://*) url="${args[$i]}" ;;
    -w) has_w=1; [ "${args[$((i+1))]}" = '%{http_code}' ] && w_is_codeonly=1 ;;
    -o) [ "${args[$((i+1))]}" = /dev/null ] && has_o_null=1 ;;
    -f|-fsS|-fS|-sf) has_f=1 ;;
  esac
done
case "$url" in
  *"/health/live")   code=200; body='{"status": "alive", "service": "ladini-api"}' ;;
  *"/health/ready")
     code="${MOCK_READY_HTTP:-200}"
     if [ "$code" = 200 ]; then body='{"status": "ready", "components": {"database": "ok", "redis": "ok"}}'
     else body='{"status": "degraded", "components": {"database": "error: OperationalError", "redis": "ok"}}'; fi ;;
  *"/version")            code=200; body="{\"release\": \"${MOCK_VERSION_RELEASE:-sha-new}\", \"git_sha\": \"x\", \"built_at\": \"y\"}" ;;
  *"/api/webhook/twilio") code=405; body='Method Not Allowed' ;;
  *":8003/mcp")           code=401; body='Unauthorized' ;;
  *"/health")             code=200; body='{"status": "ok"}' ;;
  *)                      code=200; body='ok' ;;
esac
if [ "$has_w" = 1 ] && [ "$w_is_codeonly" = 1 ]; then
  printf '%s' "$code"
elif [ "$has_w" = 1 ]; then
  printf '%s\n%s' "$body" "$code"           # -w '\n%{http_code}'
elif [ "$has_o_null" = 1 ]; then
  :
else
  printf '%s' "$body"
fi
if [ "$has_f" = 1 ] && { [ "$code" -lt 200 ] || [ "$code" -ge 300 ]; }; then exit 22; fi
exit 0
EOF
chmod +x "${BIN}/curl"

for t in sha1sum awk sed grep df; do
  command -v "$t" >/dev/null 2>&1 || echo "  (info) outil absent sur cette machine, ignoré: $t"
done

# ── Faux dépôt de déploiement ───────────────────────────────────
REPO="${SANDBOX}/repo"; mkdir -p "$REPO/scripts" "$REPO/deploy/releases" "$REPO/backend"
cp "${ROOT}"/scripts/*.sh "$REPO/scripts/"
cp "${ROOT}/docker-compose.prod.yml" "$REPO/"
# .env minimal qui satisfait preflight
#
# (2026-09-16, follow-up pre-Hetzner) — REDIS_URL AJOUTÉ : ce fixture datait
# d'avant le chantier Hetzner scale-out qui a rendu REDIS_URL obligatoire
# (preflight.sh + docker-compose.prod.yml, Redis externe partagé). Sans lui,
# TOUS les cas 1-9 échouaient dès preflight — un faux négatif qui masquait
# ce fichier de tests depuis que REDIS_URL est devenu requis (jamais
# re-exécuté depuis, confirmé : aucun test ici ne passait avant ce correctif).
cat > "$REPO/.env" <<EOF
REDIS_URL=rediss://default:x@redis.example.com:6379/0
REDIS_PASSWORD=$(printf 'a%.0s' {1..32})
MCP_HTTP_AUTH_TOKEN=$(printf 'b%.0s' {1..32})
FLOWER_USER=admin
FLOWER_PASSWORD=$(printf 'c%.0s' {1..20})
GROQ_API_KEY=gsk_realish
DB_HOST=db.example.com
DB_NAME=ladini
DB_USER=u
DB_PASSWORD=p
EOF

run() {  # run <expected_exit> <label> -- <cmd...>
  local exp="$1" label="$2"; shift 2; [ "$1" = "--" ] && shift
  local out rc
  out="$(timeout 45 "$@" 2>&1)"; rc=$?
  [ "$rc" -eq 124 ] && { bad "$label (TIMEOUT 45s — hang)"; echo "$out" | sed 's/^/      /' | tail -n 15; return; }
  if [ "$rc" -eq "$exp" ]; then ok "$label (exit $rc)"; else
    bad "$label (attendu $exp, obtenu $rc)"; echo "$out" | sed 's/^/      /' | tail -n 15
  fi
}
export PATH="${BIN}:${PATH}"
export MIN_FREE_MB=0                       # le sandbox tmpfs peut être petit
cd "$REPO"

echo "── Cas 6 — variables manquantes → preflight refuse AVANT prod ──"
mv .env .env.bak
run 1 "preflight sans .env" -- bash scripts/preflight.sh sha-new
mv .env.bak .env

echo "── Cas 2/6 — release mutable 'latest' refusée ──"
run 1 "preflight latest" -- bash scripts/preflight.sh latest

echo "── Cas 7 — disque insuffisant → preflight refuse ──"
run 1 "preflight disque plein" -- env MIN_FREE_MB=999999999 bash scripts/preflight.sh sha-new

echo "── preflight nominal OK ──"
run 0 "preflight sha-new" -- bash scripts/preflight.sh sha-new

echo "── Cas 1 — déploiement normal A→B→SUCCESS ──"
: > deploy/releases/history.log
run 0 "deploy sha-A (1er)" -- env AUTO_ROLLBACK=1 HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-A bash scripts/deploy.sh sha-A
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)" = "sha-A" ] && ok "current == sha-A" || bad "current != sha-A"
run 0 "deploy sha-B" -- env AUTO_ROLLBACK=1 HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-B bash scripts/deploy.sh sha-B
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)"  = "sha-B" ] && ok "current == sha-B"  || bad "current != sha-B"
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/previous)" = "sha-A" ] && ok "previous == sha-A" || bad "previous != sha-A"

echo "── Cas 2 — image inexistante → deploy refuse, A reste active ──"
run 1 "deploy sha-GHOST (image absente)" -- env MOCK_MISSING_IMAGE=sha-GHOST HEALTH_TIMEOUT=9 bash scripts/deploy.sh sha-GHOST
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)" = "sha-B" ] && ok "current toujours sha-B (intact)" || bad "current a bougé !"

echo "── Cas 3 — container jamais healthy → échec + rollback auto vers B ──"
run 1 "deploy sha-C (api unhealthy)" -- env MOCK_UNHEALTHY=api AUTO_ROLLBACK=1 HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-C bash scripts/deploy.sh sha-C
grep -q "deploy-failed-autorollback	sha-C" deploy/releases/history.log && ok "history: rollback auto enregistré" || bad "history: pas de rollback auto"
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)" = "sha-B" ] && ok "current toujours sha-B" || bad "current != sha-B après rollback auto"

echo "── Cas 4 — smoke KO → rollback auto vers B ──"
run 1 "deploy sha-D (/health/ready=503)" -- env MOCK_READY_HTTP=503 AUTO_ROLLBACK=1 HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 bash scripts/deploy.sh sha-D
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)" = "sha-B" ] && ok "current toujours sha-B" || bad "current != sha-B"

echo "── Cas 5 — rollback manuel B→A ──"
run 0 "rollback (→ previous = sha-A)" -- env HEALTH_TIMEOUT=9 MOCK_VERSION_RELEASE=sha-A bash scripts/rollback.sh
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)"  = "sha-A" ] && ok "current == sha-A" || bad "current != sha-A"
[ "$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/previous)" = "sha-B" ] && ok "previous == sha-B" || bad "previous != sha-B"

echo "── Cas 8 — deux deploys concurrents → le 2e est bloqué par le verrou ──"
# Simule un déploiement déjà en cours en posant le lockdir à la main.
mkdir -p "$REPO/deploy/$(basename "$REPO/deploy/.deploy").lockdir" 2>/dev/null || true
LKD="$REPO/deploy/.deploy.lockdir"; mkdir -p "$LKD"; echo "$$" > "$LKD/pid"; echo "pid=$$ user=test" > "$LKD/info"
run 1 "deploy concurrent refusé" -- env HEALTH_TIMEOUT=9 bash scripts/deploy.sh sha-E
rm -rf "$LKD"

# ── Cas 9 — garde de migration : dépôt git MINIMAL et 100% ISOLÉ ─────────
# On ne clone PAS le vrai dépôt (destructif + lent) : on fabrique un dépôt
# jouet de 3 fichiers, le check_migrations.sh y est copié, il ne teste que
# la LOGIQUE de détection des motifs destructifs (son vrai job en CI).
mk_migrepo() {  # mk_migrepo <dir> <ligne-ajoutée> [amend-msg]
  local d="$1" line="$2" amsg="${3:-}"
  mkdir -p "$d/backend/schema_contract/migrations" "$d/scripts"
  cp "${ROOT}/scripts/check_migrations.sh" "$d/scripts/"
  ( cd "$d"
    git init -q; git config user.email t@t; git config user.name t
    mkdir -p backend/alembic
    printf 'ALTER TABLE x ADD COLUMN IF NOT EXISTS a text;\n' \
      > backend/schema_contract/migrations/0001_change.sql
    git add -A; git commit -q -m "base"
    printf '%s\n' "$line" >> backend/schema_contract/migrations/0001_change.sql
    git add -A; git commit -q -m "migration change"
    [ -n "$amsg" ] && git commit -q --amend -m "$amsg" || true
  )
}

echo "── Cas 9 — migration destructive détectée par le guard CI ──"
MR="${SANDBOX}/migrepo"
mk_migrepo "$MR" '    "ALTER TABLE marketplace.orders DROP COLUMN delivery_otp",'
run 1 "check_migrations bloque DROP COLUMN" -- bash -c "cd '$MR' && bash scripts/check_migrations.sh HEAD~1 HEAD"
CLS="$(cd "$MR" && bash scripts/check_migrations.sh HEAD~1 HEAD --classify 2>/dev/null)"
[ "$CLS" = "MIGRATION_REQUIRES_MANUAL_RECOVERY" ] && ok "classify == MIGRATION_REQUIRES_MANUAL_RECOVERY" || bad "classify == '$CLS'"

MR3="${SANDBOX}/migrepo3"
mk_migrepo "$MR3" '    "ALTER TABLE marketplace.orders DROP COLUMN delivery_otp",' \
  "$(printf 'test: contraction assumée\n\nmigration-contract-approved: retrait planifié')"
run 0 "check_migrations laisse passer si 'migration-contract-approved'" -- bash -c "cd '$MR3' && bash scripts/check_migrations.sh HEAD~1 HEAD"

echo "── Cas 9b — EXPAND (ADD COLUMN nullable) est ROLLBACK_SAFE ──"
MR2="${SANDBOX}/migrepo2"
mk_migrepo "$MR2" '    "ALTER TABLE marketplace.orders ADD COLUMN IF NOT EXISTS tmpcol VARCHAR",'
CLS="$(cd "$MR2" && bash scripts/check_migrations.sh HEAD~1 HEAD --classify 2>/dev/null)"
[ "$CLS" = "ROLLBACK_SAFE" ] && ok "EXPAND-only == ROLLBACK_SAFE" || bad "EXPAND classify == '$CLS'"

# ── Cas 10 — node_deploy.sh (single-node) ne résout JAMAIS la migration
#            contre le littéral "HEAD" (2026-09-16, follow-up pre-Hetzner) ──
# Sandbox DÉDIÉ (pas $REPO — déjà mutée par les cas 1-9) : copie de lib.sh
# avec `migration_class_between` REDÉFINIE en fin de fichier (la dernière
# définition d'une fonction bash l'emporte) pour capturer ses 2 arguments
# au lieu de faire un vrai `git diff` — ce test prouve QUEL SHA est passé,
# pas le comportement de check_migrations.sh (déjà couvert au Cas 9).
echo "── Cas 10 — GIT_SHA (jamais HEAD) pour la classification migration ──"
R10="${SANDBOX}/repo10"
mkdir -p "$R10/scripts" "$R10/deploy/releases" "$R10/backend/alembic"
cp "${ROOT}"/scripts/*.sh "$R10/scripts/"
cp "${ROOT}/docker-compose.prod.yml" "$R10/"
: > "$R10/backend/alembic.ini"   # présence seule suffit à activer la branche
cat > "$R10/.env" <<EOF
REDIS_URL=rediss://default:x@redis.example.com:6379/0
REDIS_PASSWORD=$(printf 'a%.0s' {1..32})
MCP_HTTP_AUTH_TOKEN=$(printf 'b%.0s' {1..32})
FLOWER_USER=admin
FLOWER_PASSWORD=$(printf 'c%.0s' {1..20})
GROQ_API_KEY=gsk_realish
DB_HOST=db.example.com
DB_NAME=ladini
DB_USER=u
DB_PASSWORD=p
EOF
MIG_LOG="${SANDBOX}/mig_calls.log"; : > "$MIG_LOG"
cat >> "$R10/scripts/lib.sh" <<EOF
# ── Override de test (Cas 10, run-scenarios.sh) — capture les arguments
# au lieu de faire un vrai git diff. La dernière définition d'une fonction
# bash l'emporte : cette version remplace celle plus haut dans ce fichier.
migration_class_between() {
  printf 'FROM=%s TO=%s\n' "\$1" "\$2" >> "${MIG_LOG}"
  echo "ROLLBACK_SAFE"
}
EOF
( cd "$R10" && git init -q && git config user.email t@t && git config user.name t \
    && git commit -q --allow-empty -m base )
run 0 "deploy sha-X10a (1er, alembic présent)" -- env HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-X10a bash "$R10/scripts/deploy.sh" sha-X10a
run 0 "deploy sha-X10b (2e, exerce la classification)" -- env HEALTH_TIMEOUT=6 ROLLBACK_HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-X10b bash "$R10/scripts/deploy.sh" sha-X10b
if [ -s "$MIG_LOG" ]; then
  if grep -qE '(^|[[:space:]])(FROM|TO)=HEAD([[:space:]]|$)' "$MIG_LOG"; then
    bad "migration_class_between n'a JAMAIS reçu le littéral HEAD ($(cat "$MIG_LOG"))"
  else
    ok "migration_class_between n'a jamais reçu le littéral HEAD ($(tail -n1 "$MIG_LOG"))"
  fi
  # Le mock docker (image_label) renvoie toujours ce SHA factice pour
  # org.opencontainers.image.revision — c'est la SEULE source de vérité
  # attendue pour le "TO" de la 2e classification.
  if grep -q 'TO=deadbeefcafe1234deadbeefcafe1234deadbeef' "$MIG_LOG"; then
    ok "TO == GIT_SHA résolu depuis le label OCI (jamais HEAD ni le tag de release)"
  else
    bad "TO n'est pas le GIT_SHA résolu depuis le label OCI ($(cat "$MIG_LOG"))"
  fi
else
  bad "migration_class_between n'a jamais été appelée — la branche alembic.ini n'a pas été exercée"
fi

echo "── Cas 11 — rollback.sh : GIT_SHA manquant ⇒ jamais de repli sur HEAD ──"
R11="${SANDBOX}/repo11"
mkdir -p "$R11/scripts" "$R11/deploy/releases" "$R11/backend/alembic"
cp "${ROOT}"/scripts/*.sh "$R11/scripts/"
cp "${ROOT}/docker-compose.prod.yml" "$R11/"
: > "$R11/backend/alembic.ini"
cp "$R10/.env" "$R11/.env"
MIG_LOG11="${SANDBOX}/mig_calls11.log"; : > "$MIG_LOG11"
cat >> "$R11/scripts/lib.sh" <<EOF
migration_class_between() {
  printf 'FROM=%s TO=%s\n' "\$1" "\$2" >> "${MIG_LOG11}"
  echo "ROLLBACK_SAFE"
}
EOF
# Manifeste de release SANS champ GIT_SHA (simule un fichier ancien/corrompu
# — exactement le cas qui retombait sur `echo HEAD` avant le correctif).
: > "$R11/deploy/releases/history.log"
cat > "$R11/deploy/releases/current" <<EOF
RELEASE_VERSION=sha-old
DEPLOYED_AT=2026-01-01T00:00:00Z
EOF
run 1 "rollback sans release précédente ni GIT_SHA (cible explicite)" -- env HEALTH_TIMEOUT=6 MOCK_VERSION_RELEASE=sha-target bash "$R11/scripts/rollback.sh" sha-target
if [ -s "$MIG_LOG11" ]; then
  bad "migration_class_between n'aurait pas dû être appelée sans GIT_SHA fiable ($(cat "$MIG_LOG11"))"
else
  ok "rollback.sh : GIT_SHA absent ⇒ classification forcée MIGRATION_REQUIRES_MANUAL_RECOVERY sans jamais tenter HEAD"
fi

rm -rf "$SANDBOX"
echo
echo "════════ RÉSULTAT : ${PASS} PASS / ${FAIL} FAIL ════════"
[ "$FAIL" -eq 0 ]
