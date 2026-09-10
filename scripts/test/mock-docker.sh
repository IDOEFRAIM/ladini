#!/usr/bin/env bash
# Faux `docker` pour tester deploy.sh / rollback.sh / preflight.sh SANS démon.
# Comportement piloté par des variables d'env :
#   MOCK_MISSING_IMAGE=<release>   → `docker manifest inspect` échoue pour cette release
#   MOCK_UNHEALTHY=<service>       → ce service ne passe jamais 'healthy'
#   MOCK_READY_HTTP=<code>         → code renvoyé pour /health/ready (via le faux curl)
#   MOCK_PULL_FAIL=<release>       → `docker compose pull` échoue pour cette release
#   MOCK_LOG=<path>               → journalise chaque appel
set -euo pipefail
log() { [ -n "${MOCK_LOG:-}" ] && echo "docker $*" >>"$MOCK_LOG" || true; }
log "$@"

cmd="${1:-}"; shift || true
case "$cmd" in
  --version) echo "Docker version 27.0.0-mock, build mock" ;;
  info) exit 0 ;;
  login) exit 0 ;;
  manifest)
    # manifest inspect <ref>
    ref="${2:-}"
    if [ -n "${MOCK_MISSING_IMAGE:-}" ] && [[ "$ref" == *":${MOCK_MISSING_IMAGE}" ]]; then
      echo "no such manifest: $ref" >&2; exit 1
    fi
    echo '{"schemaVersion":2}'; exit 0 ;;
  image)
    sub="${1:-}"; shift || true
    case "$sub" in
      inspect)
        # renvoie un label plausible
        case "$*" in
          *org.opencontainers.image.revision*) echo "deadbeefcafe1234deadbeefcafe1234deadbeef" ;;
          *org.opencontainers.image.created*)  echo "2026-09-10T00:00:00Z" ;;
          *) echo "" ;;
        esac ;;
      prune) exit 0 ;;
    esac ;;
  inspect)
    # docker inspect -f '{{ .State.Health.Status }}...' <cid>
    fmt=""; cid=""
    while [ $# -gt 0 ]; do case "$1" in -f) fmt="$2"; shift 2;; *) cid="$1"; shift;; esac; done
    svc="${cid#mockcid-}"
    if [ -n "${MOCK_UNHEALTHY:-}" ] && [ "$svc" = "${MOCK_UNHEALTHY}" ]; then
      echo "unhealthy"
    else
      echo "healthy"
    fi ;;
  compose)
    # parse jusqu'au sous-verbe
    verb=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --env-file|-f) shift 2;;
        pull|up|ps|config|run|exec|version) verb="$1"; shift; break;;
        *) shift;;
      esac
    done
    case "$verb" in
      version) echo "Docker Compose version v2.29.0-mock" ;;
      config) exit 0 ;;
      pull)
        if [ -n "${MOCK_PULL_FAIL:-}" ] && [ "${RELEASE_VERSION:-}" = "${MOCK_PULL_FAIL}" ]; then
          echo "manifest unknown" >&2; exit 1
        fi
        exit 0 ;;
      up) exit 0 ;;
      run) exit 0 ;;
      ps)
        # -q <svc> → un faux cid ; sinon un tableau
        if [ "${1:-}" = "-q" ]; then echo "mockcid-${2:-api}"; else echo "NAME  STATUS"; fi ;;
      exec) exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
