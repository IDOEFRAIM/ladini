#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/dev-up.sh — lancer la stack en LOCAL sur un poste de dev, sans
# passer par le registry ni par `scripts/deploy.sh` (réservé à un VPS avec
# une release déjà publiée sur GHCR).
#
#   ./scripts/dev-up.sh            # build local + up
#   ./scripts/dev-up.sh --no-build # up seul (images déjà construites/pull)
#   ./scripts/dev-up.sh --down     # arrête la stack
#
# Résout deux frictions réelles rencontrées en dev (2026-09-12) :
#   1. `docker-compose.prod.yml` exige `RELEASE_VERSION` (`:?…`, jamais
#      `latest` — voir §BUILD ONCE) : lancé SANS ce script, `docker compose
#      up` échoue immédiatement avec un message qui ressemble à "il ne
#      trouve pas mes variables". Ce script fixe `RELEASE_VERSION=dev` par
#      défaut (écrasable en l'exportant avant d'appeler le script).
#   2. Diagnostiquer un `.env` mal chargé (mauvais dossier, mauvais nom,
#      encodage) AVANT de lancer quoi que ce soit — `docker compose config`
#      est la SEULE source de vérité sur ce que Compose a réellement résolu
#      (pas ce que PyYAML ou un éditeur de texte en pense).
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROD_FILE="docker-compose.prod.yml"
BUILD_FILE="docker-compose.build.yml"
ENV_FILE="${ENV_FILE:-.env}"

log()  { printf '\033[1;32m[dev-up]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev-up][warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[dev-up][error]\033[0m %s\n' "$*" >&2; exit 1; }

MODE="up"
DO_BUILD=1
for arg in "$@"; do
  case "$arg" in
    --down) MODE="down" ;;
    --no-build) DO_BUILD=0 ;;
    --build) DO_BUILD=1 ;;
    *) die "argument inconnu: $arg (usage: --no-build | --build | --down)" ;;
  esac
done

# ── 0. Docker présent et démon accessible ─────────────────────────
command -v docker >/dev/null 2>&1 || die "docker introuvable dans le PATH."
docker info >/dev/null 2>&1 || die "le démon Docker n'est pas joignable (Docker Desktop lancé ?)."
docker compose version >/dev/null 2>&1 || die "docker compose (v2) introuvable — mettez à jour Docker Desktop/le plugin compose."

if [ "$MODE" = "down" ]; then
  log "Arrêt de la stack…"
  docker compose -f "$PROD_FILE" -f "$BUILD_FILE" down
  exit 0
fi

# ── 1. .env présent, DANS CE dossier précisément ──────────────────
# Docker Compose charge `.env` depuis le RÉPERTOIRE COURANT au moment de
# l'appel (pas depuis où est le fichier -f). En lançant ce script depuis
# n'importe où (`cd "$ROOT"` ci-dessus), on élimine la cause n°1 d'un
# ".env qui semble ignoré" : lancer `docker compose` depuis un autre dossier
# que la racine du repo.
if [ ! -f "$ENV_FILE" ]; then
  if [ -f ".env.example" ]; then
    warn "${ENV_FILE} absent — copie de .env.example (à REMPLIR ensuite : REDIS_PASSWORD, MCP_HTTP_AUTH_TOKEN, GROQ_API_KEY, DB_*, FLOWER_USER/PASSWORD)."
    cp .env.example "$ENV_FILE"
    die "${ENV_FILE} créé depuis .env.example — remplissez les valeurs [REQUIS] puis relancez ce script."
  else
    die "${ENV_FILE} introuvable et .env.example absent. Lancez ce script depuis la racine du dépôt."
  fi
fi

# Détection d'un piège Windows fréquent : fichier créé avec une extension
# cachée (.env.txt) ou un encodage UTF-16 (Bloc-notes) — Compose ignore le
# premier silencieusement (mauvais nom de fichier) et échoue bruyamment sur
# le second (mais avec un message peu clair). On avertit sans deviner à sa
# place ce que l'utilisateur voulait.
if [ -f ".env.txt" ]; then
  warn "Un fichier .env.txt existe à côté de ${ENV_FILE} — si vos variables n'apparaissent pas, "
  warn "c'est peut-être LUI que vous avez édité par erreur (renommez-le en .env, sans extension)."
fi
first_bytes="$(head -c 2 "$ENV_FILE" | od -An -tx1 | tr -d ' \n')"
if [ "$first_bytes" = "fffe" ] || [ "$first_bytes" = "feff" ]; then
  die "${ENV_FILE} semble être en UTF-16 (BOM détecté) — Compose attend de l'UTF-8. " \
      "Ré-enregistrez le fichier en UTF-8 (pas 'Unicode'/'UTF-16' — piège classique du Bloc-notes Windows)."
fi

# ── 2. RELEASE_VERSION — jamais requis manuellement en dev ────────
export RELEASE_VERSION="${RELEASE_VERSION:-dev}"
export GIT_SHA="${GIT_SHA:-dev-local}"
export BUILD_TIMESTAMP="${BUILD_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)}"
log "RELEASE_VERSION=${RELEASE_VERSION} (dev local — jamais poussé, jamais déployé avec ce tag)"

DC=(docker compose --env-file "$ENV_FILE" -f "$PROD_FILE")
[ "$DO_BUILD" = 1 ] && DC+=(-f "$BUILD_FILE")

# ── 3. Résolution RÉELLE de la config — la seule vérité qui compte ─
# `docker compose config` interpole TOUS les `${...}` avec le moteur RÉEL
# de Compose (pas un parseur YAML tiers) — si une variable `:?requise`
# manque, l'erreur sort ICI, avant tout `up`/`build`.
log "Résolution de la config (docker compose config)…"
if ! CONFIG_OUT="$("${DC[@]}" config 2>&1)"; then
  echo "$CONFIG_OUT" >&2
  die "docker compose config a échoué — voir l'erreur ci-dessus. Cause la plus fréquente : " \
      "une variable [REQUIS] vide/absente dans ${ENV_FILE} (le message nomme la variable)."
fi

# ── 4. Diagnostic REDACTED : quelles variables sensibles ont RÉELLEMENT
#      résolu, sans jamais afficher leur valeur. Répond directement à
#      « je ne vois pas passer REDIS_PASSWORD » : ce grep dit OUI/NON sans
#      exposer le secret.
log "Vérification des variables sensibles (valeurs masquées) :"
check_resolved() {  # check_resolved <étiquette> <motif-si-résolu>
  if echo "$CONFIG_OUT" | grep -qE "$2"; then
    printf '  \033[1;32m✓\033[0m %s résolu\n' "$1"
  else
    printf '  \033[1;31m✗\033[0m %s ABSENT de la config résolue\n' "$1"
  fi
}
check_resolved "REDIS_PASSWORD (via REDIS_URL / requirepass)" 'REDIS_URL: redis://:[^@]+@redis'
check_resolved "MCP_HTTP_AUTH_TOKEN"                          'MCP_HTTP_AUTH_TOKEN: .+'
check_resolved "DATABASE_URL (DB_HOST/USER/PASSWORD/NAME)"    'DATABASE_URL: postgresql\+asyncpg://[^:]+:[^@]+@pgbouncer'
check_resolved "GROQ_API_KEY"                                 'GROQ_API_KEY: .+'
check_resolved "FLOWER_USER/PASSWORD"                          'basic_auth=[^:]+:.+'

log "Aucune valeur secrète n'a été affichée ci-dessus — seulement présent/absent."

# ── 5. Build (overlay dev) + up ────────────────────────────────────
if [ "$DO_BUILD" = 1 ]; then
  log "Build local des 3 images (RELEASE_VERSION=${RELEASE_VERSION})…"
  "${DC[@]}" build
fi

log "docker compose up -d --wait…"
"${DC[@]}" up -d --wait --wait-timeout 180 \
  || die "Des services ne sont pas passés healthy — voir 'docker compose -f ${PROD_FILE} logs'."

log "✅ Stack up. curl http://127.0.0.1:8000/health/ready ; curl http://127.0.0.1:8000/version"
"${DC[@]}" ps
