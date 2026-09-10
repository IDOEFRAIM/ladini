#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# deploy-prod.sh — déploiement production Ladini (build + migrate + rolling)
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
# (audit 2026-09-10) La liste référençait encore clickhouse/minio/langfuse-*,
# supprimés du compose lors du passage de Langfuse en Cloud ("Version
# Allégée"). Seules les images TIERCES réellement définies sont pull-ables.
${COMPOSE} pull redis pgbouncer autoheal || warn "pull partiel."

# ── 2. Build ─────────────────────────────────────────────────────────
log "2/4 · build des images api + worker…"
${COMPOSE} build api worker

# ── 3. Migrations Alembic (conteneur éphémère, AVANT la bascule) ──────
log "3/4 · migrations base de données…"
# PgBouncer local → DB managée DigitalOcean (voir docker-compose.prod.yml,
# service `pgbouncer` : plus de Postgres local dans cette stack). `--wait`
# bloque tant que le healthcheck (nc -z 127.0.0.1 6432) n'est pas OK.
${COMPOSE} up -d --wait --wait-timeout 60 pgbouncer \
  || die "PgBouncer non healthy après 60s — vérifiez DO_DB_HOST/PORT/USER/PASSWORD/NAME dans .env."

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
#
# (audit 2026-09-10) Deux bugs corrigés ici — c'était LE point de blocage du
# déploiement :
#   1. La liste citait clickhouse/minio/langfuse-* (supprimés du compose au
#      passage de Langfuse en Cloud). `docker compose up` sort en erreur sur
#      un service inconnu ET cette ligne n'a pas de garde `|| warn` — combiné
#      au `set -Eeuo pipefail` du haut, TOUT le script avortait ici, avant la
#      bascule worker/api. Le déploiement ne pouvait pas aboutir.
#   2. `beat` et `autoheal` n'étaient démarrés NULLE PART. `beat` porte tous
#      les crons Celery (sollicitations d'enchères/proximité + les 3 services
#      de réconciliation PROCUREMENT/PREORDER/SALES) : sans lui, les drafts
#      bloqués en EXECUTING ne sont jamais réconciliés. `autoheal` est le
#      seul mécanisme qui redémarre un conteneur `unhealthy` hors Swarm (voir
#      le bandeau du compose) : sans lui, un conteneur en deadlock y reste.
#      Ni l'un ni l'autre n'a de dépendant qui les tirerait implicitement.
${COMPOSE} up -d redis pgbouncer mcp flower beat autoheal

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
