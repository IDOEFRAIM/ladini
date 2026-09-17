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
    # Le port est occupé. Il est acceptable uniquement si le mapping hôte
    # appartient à un conteneur de CETTE stack Docker Compose.
    owned_by_stack=0

    while IFS= read -r cid; do
      [ -n "$cid" ] || continue

      if docker inspect         --format '{{range $containerPort, $bindings := .NetworkSettings.Ports}}{{range $bindings}}{{println .HostPort}}{{end}}{{end}}'         "$cid" 2>/dev/null | grep -qx "$p"; then
        owned_by_stack=1
        break
      fi
    done < <(dc ps -q 2>/dev/null)

    if [ "$owned_by_stack" -ne 1 ]; then
      err "      port $p occupé par un process externe"
      port_clash=1
    else
      log "      ✓ port $p déjà tenu par un conteneur de cette stack"
    fi
  fi
done

[ "$port_clash" -eq 0 ] && log "  ✓ $CHK" || { err "  ✗ $CHK"; FAIL=1; }

# 9. Pas de verrou de déploiement résiduel bloquant (DIAGNOSTIC seulement —
#    ne touche jamais au verrou, voir lib.sh::deploy_lock_status). Le vrai
#    verrou est pris par acquire_lock() dans lib.sh, un `mkdir` atomique sur
#    LOCK_DIR, jamais LOCK_FILE — voir §BUG CORRIGÉ (historique) ci-dessous.
#
# §BUG CORRIGÉ #1 (2026-09-18, audit lock cluster/node) : cette vérification
# testait `[ -f "$LOCK_FILE" ]` — un chemin `deploy/.deploy.lock` (fichier)
# qu'AUCUN script de ce dépôt n'a jamais créé (le verrou réel est le
# RÉPERTOIRE `deploy/.deploy.lockdir`, créé par `mkdir` dans
# lib.sh::acquire_lock). Cette étape passait donc TOUJOURS au vert,
# silencieusement — un faux sentiment de sécurité, jamais une vraie
# vérification.
#
# §BUG CORRIGÉ #2 (2026-09-18, incident réel post-premier-fix) : une fois
# #1 corrigé pour tester LE VRAI LOCK_DIR, ce check tournait encore APRÈS
# que node_deploy.sh ait lui-même appelé acquire_lock() — preflight.sh
# détectait alors le verrou que SON PROPRE appelant venait de poser, et le
# signalait comme "déploiement concurrent" : auto-deadlock sur soi-même.
# Fix DÉFINITIF, côté ORDRE D'EXÉCUTION (pas ici) : node_deploy.sh appelle
# désormais preflight.sh AVANT acquire_lock() — voir son commentaire
# "§BUG CORRIGÉ ICI". Au moment où CE check tourne, le process appelant n'a
# donc JAMAIS encore posé de verrou lui-même ; un verrou "active" détecté
# ici ne peut être QUE celui d'un AUTRE déploiement, réellement concurrent.
# Ce check reste utile pour un OPÉRATEUR qui lance `preflight.sh` à la main
# pendant qu'un déploiement tourne ailleurs (voir scripts/test/
# test-node-preflight-lock.sh, Cas G3).
CHK="pas de verrou de déploiement actif"
case "$(deploy_lock_status "$LOCK_DIR")" in
  active)
    holder="$(_lock_holder_info "$LOCK_DIR")"
    err "  ✗ $CHK — ${LOCK_DIR} tenu (déploiement concurrent ?${holder:+ — $holder})"; FAIL=1
    ;;
  stale)
    log "  ✓ $CHK (un verrou périmé existe — sera récupéré automatiquement au prochain acquire_lock)"
    ;;
  *)
    log "  ✓ $CHK"
    ;;
esac

echo
if [ "$FAIL" -ne 0 ]; then
  err "PRÉFLIGHT ÉCHOUÉ — la production n'a PAS été touchée."
  exit 1
fi
log "PRÉFLIGHT OK — prêt à déployer ${TARGET_RELEASE}."
