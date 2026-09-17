#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/predeploy_check.sh — dernière barrière avant un déploiement
# Hetzner réel. Chaîne les vérifications STATIQUES (rien ici ne parle au
# réseau de production, rien n'est destructif) :
#
#   1. syntaxe bash des scripts de déploiement (bash -n)
#   2. régression pare-feu hôte (infra/firewall/ufw.sh --dry-run, voir
#      scripts/test/test-ufw-firewall.sh) — PRIVATE_NET_CIDR, dry-run
#      inoffensif, non-régression de l'incident "ufw resté inactive"
#   3. régression verrous de déploiement (scripts/test/
#      test-deploy-lock-architecture.sh + test-node-preflight-lock.sh) —
#      deadlock cluster/node ET deadlock node/preflight (voir §BUG plus bas)
#   4. docker compose config (docker-compose.prod.yml, aucun `build:`)
#   5. validation de infra/inventory.yml (si présent) — via
#      scripts/validate_inventory.py, script Python STDLIB PUR (re/sys/
#      pathlib, aucune dépendance tierce), exécutable par n'importe quel
#      `python3` système
#   6. terraform fmt -check + validate (si terraform est installé et
#      infra/providers/hetzner/*.tf présent) — jamais `plan`/`apply` ici
#   7. smoke.sh / smoke_observability.sh — UNIQUEMENT si SMOKE_API_URL est
#      déjà exporté par l'appelant (un stack tourne réellement quelque
#      part) ; sinon sautés avec un avertissement, jamais un échec
#
# CE QUE CE SCRIPT NE FAIT PLUS (2026-09-18, incident réel premier déploiement
# Hetzner) : il n'exécute PLUS `pytest backend/tests/...`. Root cause : ce
# script tourne SUR LE NODE DE PROD (Ubuntu 24.04 minimal, images Docker
# immuables GHCR) — qui n'a NI Poetry NI les dépendances backend (SQLAlchemy,
# pydantic, langgraph…) installées, et ne doit JAMAIS les installer juste
# pour ce script (l'image de prod ne contient même pas `backend/tests`,
# volontairement). `pytest` y tombait sur `/usr/bin/python3` sans ces
# dépendances → échec de COLLECTION (pas un vrai échec de test) dès qu'un
# seul fichier importait `ladini.*`, faisant échouer predeploy_check pour une
# raison n'ayant RIEN à voir avec la sécurité du déploiement lui-même. Ces
# tests applicatifs (architecture métier, PII redaction, idempotency,
# métriques worker, invariants compose nécessitant PyYAML) restent un GATE
# CI obligatoire — voir `.github/workflows/cicd.yml` (`pytest tests`, tout le
# dossier) et sa dépendance dure avec `release.yml` (`workflow_run` +
# `conclusion == 'success'`) : une image ne peut être poussée sur GHCR que si
# CI est passée. Non-régression : backend/tests/architecture/
# test_ci_release_gating.py (CI) + scripts/test/test-predeploy-check-minimal-host.sh.
#
# Deux incidents de VERROUILLAGE réels corrigés le même jour (2026-09-18),
# couverts par l'étape 3 ci-dessus :
#   (1) cluster_deploy.sh (orchestrateur, self-hosted runner co-localisé
#       avec le node qu'il déploie) dégradait son verrou cluster-wide sur LA
#       MÊME ressource que le verrou local du node → auto-deadlock dès que
#       l'orchestrateur SSHait vers lui-même. Fix : deux verrous, deux
#       ressources toujours distinctes (voir scripts/lib.sh).
#   (2) node_deploy.sh appelait acquire_lock() AVANT preflight.sh — dont le
#       check 9 (verrou actif) détectait alors le verrou que node_deploy.sh
#       venait LUI-MÊME de poser, et refusait son propre déploiement. Fix :
#       preflight.sh (non-mutant) tourne désormais TOUJOURS avant
#       acquire_lock() — voir le commentaire "§BUG CORRIGÉ" dans
#       node_deploy.sh.
#
# Sortie 0 = tout est passé. Sortie != 0 = NE PAS déployer, lire le
# dernier bloc affiché.
#
# Usage :
#   ./scripts/predeploy_check.sh
#   SMOKE_API_URL=http://127.0.0.1:18000 ./scripts/predeploy_check.sh   # + smoke sur un E2E déjà démarré
#   TERRAFORM_BIN=/path/to/terraform.exe ./scripts/predeploy_check.sh  # si `terraform` n'est pas sur PATH
#   PYTHON_BIN=/usr/bin/python3 ./scripts/predeploy_check.sh            # forcer l'interpréteur (défaut : détection auto)
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "${ROOT}"

TERRAFORM_BIN="${TERRAFORM_BIN:-terraform}"
FAILED=0
STEP=0

_step() {
  STEP=$((STEP + 1))
  echo ""
  echo "── [${STEP}] $* ──────────────────────────────────────"
}

_fail() {
  echo "✗ $*"
  FAILED=1
}

_ok() {
  echo "✓ $*"
}

# ── 1. Syntaxe bash des scripts de déploiement ────────────────────────
_step "Syntaxe bash (scripts/*.sh)"
for f in scripts/*.sh; do
  if bash -n "$f"; then
    :
  else
    _fail "bash -n a échoué sur $f"
  fi
done
[ "$FAILED" -eq 0 ] && _ok "tous les scripts sont syntaxiquement valides"

# ── 2. Régression pare-feu hôte (ufw.sh, --dry-run uniquement) ─────────
_step "Régression pare-feu hôte (infra/firewall/ufw.sh --dry-run)"
if bash scripts/test/test-ufw-firewall.sh; then
  _ok "ufw.sh : Cas A/B/C/D passent (PRIVATE_NET_CIDR, dry-run, non-régression incident ufw inactive)"
else
  _fail "ufw.sh : au moins un cas de scripts/test/test-ufw-firewall.sh a échoué"
fi

# ── 3. Régression verrous de déploiement (cluster/node + node/preflight) ──
_step "Régression verrous de déploiement (cluster/node, node/preflight)"
if bash scripts/test/test-deploy-lock-architecture.sh; then
  _ok "verrous cluster/node : Cas A-F passent (deadlock co-localisé, double orchestration, stale lock, REDIS_URL)"
else
  _fail "verrous cluster/node : au moins un cas de test-deploy-lock-architecture.sh a échoué"
fi
if bash scripts/test/test-node-preflight-lock.sh; then
  _ok "verrou node vs preflight : Cas G passent (preflight ne s'auto-bloque plus sur son propre verrou)"
else
  _fail "verrou node vs preflight : au moins un cas de test-node-preflight-lock.sh a échoué"
fi

# ── 4. docker compose config (prod, sans build:) ──────────────────────
_step "docker compose config (docker-compose.prod.yml)"
if RELEASE_VERSION=predeploy-check-dummy REDIS_URL="rediss://x:y@z:6379/0" \
   MCP_HTTP_AUTH_TOKEN=x FLOWER_USER=x FLOWER_PASSWORD=x GROQ_API_KEY=x \
   DB_HOST=x DB_USER=x DB_PASSWORD=x DB_NAME=x \
   docker compose -f docker-compose.prod.yml config --quiet 2>/tmp/predeploy_compose_err; then
  _ok "docker-compose.prod.yml est valide"
else
  _fail "docker compose config a échoué : $(cat /tmp/predeploy_compose_err 2>/dev/null)"
fi
if grep -qE '^\s*build:' docker-compose.prod.yml; then
  _fail "docker-compose.prod.yml contient une directive build: (violation BUILD ONCE)"
else
  _ok "aucune directive build: dans docker-compose.prod.yml"
fi

# ── 5. infra/inventory.yml (si présent) — Python STDLIB PUR uniquement ──
# `PYTHON_BIN` : PAS de préférence pour un venv projet ici (ce script tourne
# aussi bien sur un poste de dev QUE sur le node de prod, qui n'a ni Poetry
# ni `backend/.venv`) — n'importe quel `python3` système suffit, car
# scripts/validate_inventory.py n'importe QUE `re`/`sys`/`pathlib` (vérifié :
# aucun `import ladini`, aucune dépendance tierce). Overridable via
# `PYTHON_BIN=...` (voir en-tête) pour forcer un interpréteur précis.
_step "infra/inventory.yml"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN="python"
if [ -f infra/inventory.yml ]; then
  if "$PYTHON_BIN" scripts/validate_inventory.py infra/inventory.yml; then
    _ok "infra/inventory.yml valide"
  else
    _fail "infra/inventory.yml invalide (voir message ci-dessus)"
  fi
else
  echo "⚠ infra/inventory.yml absent (normal si pas encore provisionné) — sauté"
fi

# ── 6. Terraform fmt + validate (jamais plan/apply ici) ────────────────
_step "Terraform fmt + validate (infra/providers/hetzner)"
if command -v "$TERRAFORM_BIN" >/dev/null 2>&1 || [ -x "$TERRAFORM_BIN" ]; then
  pushd infra/providers/hetzner >/dev/null
  if "$TERRAFORM_BIN" fmt -check -recursive >/tmp/predeploy_tf_fmt 2>&1; then
    _ok "terraform fmt -check propre"
  else
    _fail "terraform fmt -check a des diffs : $(cat /tmp/predeploy_tf_fmt)"
  fi
  if "$TERRAFORM_BIN" init -backend=false -input=false >/tmp/predeploy_tf_init 2>&1 \
     && "$TERRAFORM_BIN" validate >/tmp/predeploy_tf_validate 2>&1; then
    _ok "terraform validate OK"
  else
    _fail "terraform validate a échoué : $(cat /tmp/predeploy_tf_validate 2>/dev/null)"
  fi
  popd >/dev/null
else
  echo "⚠ terraform introuvable (ni sur PATH ni à TERRAFORM_BIN=${TERRAFORM_BIN}) — sauté, NOT RUN"
fi

# ── 7. Smoke (optionnel — seulement si un stack tourne déjà) ───────────
_step "smoke.sh / smoke_observability.sh"
if [ -n "${SMOKE_API_URL:-}" ]; then
  if SMOKE_API_URL="$SMOKE_API_URL" SMOKE_CHECK_ADMIN=0 ./scripts/smoke.sh; then
    _ok "smoke.sh passé"
  else
    _fail "smoke.sh a échoué"
  fi
  if SMOKE_API_URL="$SMOKE_API_URL" ./scripts/smoke_observability.sh; then
    _ok "smoke_observability.sh passé (avertissement seul si échec, voir sortie)"
  else
    echo "⚠ smoke_observability.sh a signalé un problème (observabilité — jamais un gate bloquant)"
  fi
else
  echo "⚠ SMOKE_API_URL non défini — aucun stack à sonder, sauté (NOT RUN)"
fi

# ── Verdict ──────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
if [ "$FAILED" -eq 0 ]; then
  echo "✓ PREDEPLOY CHECK: PASS — rien n'a bloqué (voir avertissements ⚠ ci-dessus, non bloquants)."
  exit 0
else
  echo "✗ PREDEPLOY CHECK: FAIL — corriger les points ✗ ci-dessus avant de déployer."
  exit 1
fi
