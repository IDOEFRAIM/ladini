#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/preflight.sh — vérifications AVANT toute modification de la prod.
#
#   ./scripts/preflight.sh <release>
#
# Échoue AVANT de toucher quoi que ce soit (§42 failure atomicity). Ne
# `pull` pas, ne `up` pas, ne stoppe rien. Sortie ≠ 0 → deploy.sh s'arrête,
# la prod actuelle continue.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

TARGET_RELEASE="${1:-}"
MIN_FREE_MB="${MIN_FREE_MB:-3000}"       # 3 Go libres minimum avant un pull
FAIL=0
check() { if "$@"; then log "  ✓ $CHK"; else err "  ✗ $CHK"; FAIL=1; fi; }

log "Préflight — release cible : ${TARGET_RELEASE:-<non fournie>}"

# 1. release fournie et NON mutable
CHK="release fournie"
[ -n "$TARGET_RELEASE" ] && log "  ✓ $CHK" || { err "  ✗ $CHK — usage: preflight.sh <release>"; FAIL=1; }
CHK="release non mutable (pas 'latest'/'stable'/'main')"
case "$TARGET_RELEASE" in
  latest|stable|main|master|"") err "  ✗ $CHK — '$TARGET_RELEASE' interdit en prod (§29)"; FAIL=1 ;;
  *) log "  ✓ $CHK" ;;
esac

# 2. Docker + Compose v2
CHK="docker installé"; check command -v docker >/dev/null
CHK="démon docker joignable"; check docker info >/dev/null 2>&1
CHK="docker compose v2"
if docker compose version >/dev/null 2>&1; then log "  ✓ $CHK"; else err "  ✗ $CHK"; FAIL=1; fi

# 3. Fichiers indispensables
CHK=".env présent";           check test -f "$ENV_FILE"
CHK="compose présent";        check test -f "$COMPOSE_FILE"

# 4. Variables obligatoires présentes dans .env (les `:?` du compose)
CHK="variables obligatoires (.env)"
# (2026-09-16, chantier Hetzner scale-out §5/§20) : REDIS_URL remplace
# REDIS_PASSWORD dans cette liste — la production exige désormais un Redis
# EXTERNE partagé (REDIS_URL complet, mot de passe déjà inclus dans l'URL),
# plus de conteneur Redis local dans docker-compose.prod.yml. REDIS_PASSWORD
# reste utile en DEV (docker-compose.dev.yml), jamais requis en prod.
REQUIRED_VARS=(REDIS_URL MCP_HTTP_AUTH_TOKEN FLOWER_USER FLOWER_PASSWORD GROQ_API_KEY)
# DB : accepte la convention neutre OU le legacy DO_DB_*
missing=()
for v in "${REQUIRED_VARS[@]}"; do
  grep -qE "^${v}=.+" "$ENV_FILE" || missing+=("$v")
done
if ! grep -qE '^(DB_HOST|DO_DB_HOST)=.+' "$ENV_FILE"; then missing+=("DB_HOST|DO_DB_HOST"); fi
if ! grep -qE '^(DB_NAME|DO_DB_NAME)=.+' "$ENV_FILE"; then missing+=("DB_NAME|DO_DB_NAME"); fi
if [ "${#missing[@]}" -eq 0 ]; then log "  ✓ $CHK"; else err "  ✗ $CHK — manquantes/vides : ${missing[*]}"; FAIL=1; fi

# 4bis. REDIS_URL ne doit JAMAIS pointer vers un conteneur Docker local en
# production (§5/§20) — un `redis://...@redis:6379/...` ou `@localhost:...`
# signifierait un Redis par node, silencieusement incohérent dès 2 nodes
# (idempotence/verrous/broker Celery divergents entre nodes). Ne bloque pas
# le dev (docker-compose.dev.yml n'est jamais dans COMPOSE_FILE ici), donc
# ce garde est sans risque de faux positif sur le chemin de déploiement réel.
CHK="REDIS_URL pointe vers un Redis externe (pas un hôte local/conteneur)"
redis_url_line="$(grep -E '^REDIS_URL=' "$ENV_FILE" | tail -n1)"
if printf '%s' "$redis_url_line" | grep -qiE '@(redis|localhost|127\.0\.0\.1):'; then
  err "  ✗ $CHK — REDIS_URL ressemble à un Redis local/conteneur : redis externe partagé requis en prod"
  FAIL=1
else
  log "  ✓ $CHK"
fi

CHK="pas de placeholder 'change_me' dans .env"
if grep -qiE '=(change_me|changeme|gsk_xxx|pk-lf-xxx|sk-lf-xxx)' "$ENV_FILE"; then
  err "  ✗ $CHK — des valeurs d'exemple traînent dans .env"; FAIL=1
else log "  ✓ $CHK"; fi

# 5. compose config valide (résolution des variables), AVANT tout changement
CHK="docker compose config valide"
if RELEASE_VERSION="$TARGET_RELEASE" dc config --quiet 2>/tmp/pf_cfg.err; then
  log "  ✓ $CHK"
else
  err "  ✗ $CHK"; sed 's/^/      /' /tmp/pf_cfg.err >&2; FAIL=1
fi
CHK="compose PROD sans directive build: (build-once)"
if RELEASE_VERSION="$TARGET_RELEASE" dc config 2>/dev/null | grep -qE '^\s+build:'; then
  err "  ✗ $CHK"; FAIL=1
else log "  ✓ $CHK"; fi

# 6. Registry accessible + les 3 images de la release existent
CHK="login registry (${REGISTRY})"
if docker login "$REGISTRY" </dev/null >/dev/null 2>&1 || [ -f "${HOME}/.docker/config.json" ]; then
  log "  ✓ $CHK (session existante)"
else
  warn "  ? $CHK — pas de session ; \`docker login ${REGISTRY}\` peut être requis"
fi
CHK="les 3 images ladini-*:${TARGET_RELEASE} existent au registry"
img_ok=1
if [ -n "$TARGET_RELEASE" ]; then
  for svc in "${APP_SERVICES[@]}"; do
    ref="$(image_ref "$svc" "$TARGET_RELEASE")"
    docker manifest inspect "$ref" >/dev/null 2>&1 || { err "      absente : $ref"; img_ok=0; }
  done
fi
[ "$img_ok" -eq 1 ] && log "  ✓ $CHK" || { err "  ✗ $CHK"; FAIL=1; }

# 7. Espace disque (anciennes + nouvelles images + layers + logs)
CHK="espace disque ≥ ${MIN_FREE_MB} Mo"
FREE_MB="$(df -Pm "$LADINI_ROOT" | awk 'NR==2 {print $4}')"
if [ "${FREE_MB:-0}" -ge "$MIN_FREE_MB" ]; then
  log "  ✓ $CHK (libre : ${FREE_MB} Mo)"
else
  err "  ✗ $CHK — libre : ${FREE_MB:-?} Mo. Nettoyez (voir scripts/deploy.sh::prune ou 'docker image prune -f')."
  FAIL=1
fi

# 8. Ports hôte requis (loopback) libres OU déjà tenus par NOS conteneurs
CHK="ports 127.0.0.1:8000 / 127.0.0.1:5555 disponibles ou à nous"
port_clash=0
for p in 8000 5555; do
  if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
    # occupé — OK seulement si c'est un conteneur de CETTE stack
    if ! dc ps --format '{{.Publishers}}' 2>/dev/null | grep -q ":$p->"; then
      err "      port $p occupé par un process externe"; port_clash=1
    fi
  fi
done
[ "$port_clash" -eq 0 ] && log "  ✓ $CHK" || { err "  ✗ $CHK"; FAIL=1; }

# 9. Pas de verrou de déploiement résiduel bloquant (info seulement — le vrai
#    verrou est pris par deploy.sh via flock).
CHK="pas de verrou de déploiement actif"
if [ -f "$LOCK_FILE" ] && fuser "$LOCK_FILE" >/dev/null 2>&1; then
  err "  ✗ $CHK — $LOCK_FILE tenu (déploiement concurrent ?)"; FAIL=1
else log "  ✓ $CHK"; fi

echo
if [ "$FAIL" -ne 0 ]; then
  err "PRÉFLIGHT ÉCHOUÉ — la production n'a PAS été touchée."
  exit 1
fi
log "PRÉFLIGHT OK — prêt à déployer ${TARGET_RELEASE}."
