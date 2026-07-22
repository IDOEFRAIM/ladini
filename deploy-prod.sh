#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# deploy-prod.sh — déploiement production AgriConnect (build + migrate + rolling)
#
# Étapes :
#   1. pull des dernières images de base + git pull (optionnel)
#   2. build des images api + worker
#   3. migrations Alembic via un conteneur ÉPHÉMÈRE (offline, avant bascule)
#   4. rolling update : worker d'abord (tâches re-queue), puis api (health-gated)
#
# ⚠️  ZERO-DOWNTIME RÉEL sur l'API : docker compose seul recrée le conteneur api
#     (micro-coupure). Pour une vraie bascule sans coupure des webhooks, placez
#     un reverse-proxy (nginx/traefik) devant 2+ répliques d'`api` et mettez à
#     jour une réplique à la fois. Ce script fait le "near-zero" mono-réplique +
#     dépendances health-gated ; voir la section API ci-dessous.
#
# Prérequis : Docker + Compose v2, un fichier .env à la racine (voir .env.example).
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
COMPOSE="docker compose -f ${COMPOSE_FILE}"
ENV_FILE="${ENV_FILE:-.env}"

log() { printf "\033[1;32m[deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[deploy][warn]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[deploy][error]\033[0m %s\n" "$*" >&2; exit 1; }

[ -f "${ENV_FILE}" ] || die "Fichier ${ENV_FILE} introuvable — copiez .env.example et remplissez les secrets."
[ -f "${COMPOSE_FILE}" ] || die "${COMPOSE_FILE} introuvable (lancez depuis la racine du repo)."

# ── 1. Pull ──────────────────────────────────────────────────────────
log "1/4 · git pull + pull des images de base…"
if [ "${SKIP_GIT_PULL:-0}" != "1" ] && command -v git >/dev/null 2>&1; then
  git pull --ff-only || warn "git pull ignoré (repo non-git ou divergence)."
fi
${COMPOSE} pull redis postgres pgbouncer clickhouse minio langfuse-web langfuse-worker langfuse-postgres || warn "pull partiel."

# ── 2. Build ─────────────────────────────────────────────────────────
log "2/4 · build des images api + worker…"
${COMPOSE} build api worker

# ── 3. Migrations Alembic (conteneur éphémère, AVANT la bascule) ──────
log "3/4 · migrations base de données…"
# On démarre d'abord la DB + pgbouncer et on attend leur santé.
${COMPOSE} up -d postgres pgbouncer
log "   attente santé DB…"
for i in $(seq 1 30); do
  if ${COMPOSE} exec -T postgres pg_isready -U "${POSTGRES_USER:-postgres}" >/dev/null 2>&1; then break; fi
  sleep 2
  [ "$i" = "30" ] && die "Postgres non prêt après 60s."
done

if [ -f "backend/alembic.ini" ]; then
  # Conteneur JETABLE (--rm) qui applique les migrations puis disparaît. Offline :
  # aucune requête utilisateur ne passe encore par le nouveau code.
  ${COMPOSE} run --rm --no-deps \
    -w /app/backend \
    api alembic upgrade head \
    || die "Migration Alembic échouée — bascule annulée (la prod tourne toujours sur l'ancien code)."
  log "   migrations appliquées."
else
  warn "backend/alembic.ini absent → Alembic n'est PAS encore configuré dans ce repo."
  warn "   Migrations SAUTÉES. Bootstrap requis : \`cd backend && alembic init alembic\`,"
  warn "   pointez sqlalchemy.url sur DATABASE_URL, générez une révision initiale,"
  warn "   puis relancez ce script. (Le schéma actuel est créé hors-Alembic.)"
fi

# ── 4. Rolling update ────────────────────────────────────────────────
log "4/4 · bascule des services applicatifs…"

# 4a. Dépendances (idempotent, ne coupe rien si déjà up).
${COMPOSE} up -d redis pgbouncer flower \
  clickhouse minio langfuse-postgres langfuse-web langfuse-worker

# 4b. Worker : recréation sûre — les tâches en vol sont re-queue (acks_late=True
#     dans celery_app.py), aucune perte. On attend qu'il redevienne healthy.
log "   worker…"
${COMPOSE} up -d --no-deps --build worker
${COMPOSE} up -d --wait --wait-timeout 120 worker || warn "worker healthcheck non confirmé (vérifiez flower)."

# 4c. API : recréation health-gated. `--wait` bloque tant que /health n'est pas OK,
#     donc on ne "termine" pas le déploiement sur une API cassée.
log "   api (health-gated)…"
${COMPOSE} up -d --no-deps --build api
${COMPOSE} up -d --wait --wait-timeout 120 api || die "API non healthy après bascule — INVESTIGUEZ (logs ci-dessous)."

log "✅ Déploiement terminé."
${COMPOSE} ps
