#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-predeploy-check-minimal-host.sh — régression sur
# scripts/predeploy_check.sh (2026-09-18, incident réel premier déploiement
# Hetzner) : ce script tourne SUR LE NODE DE PROD, qui n'a NI Poetry NI les
# dépendances backend (SQLAlchemy, pydantic, langgraph…) installées, et ne
# doit JAMAIS les installer juste pour predeploy_check.sh. Avant le fix,
# l'étape [4] lançait `pytest backend/tests/...` — dont plusieurs fichiers
# importent `ladini.*` — et échouait à la COLLECTION dès que ce module
# n'était pas installable, faisant échouer predeploy_check pour une raison
# sans rapport avec la sécurité réelle du déploiement.
#
# Couvre :
#   Cas A : predeploy_check.sh ne contient plus AUCUNE invocation `pytest`
#           ciblant backend/tests/ (le pattern qui a causé l'incident ne
#           peut plus silencieusement revenir).
#   Cas B : le SEUL script Python que predeploy_check.sh exécute encore
#           (scripts/validate_inventory.py) tourne avec un interpréteur qui
#           n'a PAS le paquet `ladini` importable (le cas réel sur un host
#           de prod frais) — preuve directe que la chaîne restante est
#           stdlib-safe.
#   Cas C : predeploy_check.sh lui-même, avec PYTHON_BIN forcé sur cet
#           interpréteur sans `ladini`, ne produit AUCUNE trace de
#           `ModuleNotFoundError` mentionnant ladini/sqlalchemy/pydantic
#           dans sa sortie (non-régression bout en bout — les autres étapes
#           peuvent échouer pour des raisons qui leur sont propres, ex.
#           docker/terraform absents de CET environnement de test, ce
#           n'est PAS ce que ce cas vérifie).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/predeploy_check.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "═══ Cas A : plus aucune invocation pytest dans predeploy_check.sh ═══"
# Seules les lignes de CODE comptent — l'en-tête documente délibérément
# l'incident et mentionne "pytest"/"backend/tests" en PROSE (explique
# pourquoi ce n'est plus exécuté ici). On exclut les lignes de commentaire
# (`#`, espaces de tête compris) pour ne juger que ce qui s'exécute
# réellement.
CODE_ONLY="$(grep -vE '^\s*#' "$SCRIPT")"
if grep -qE '\bpytest\b' <<<"$CODE_ONLY"; then
  bad "predeploy_check.sh exécute encore 'pytest' — régression possible de l'incident 2026-09-18"
  grep -nE '\bpytest\b' "$SCRIPT" | grep -vE '^\s*[0-9]+:\s*#'
else
  ok "aucune invocation pytest — les tests applicatifs restent un gate CI, jamais un gate host"
fi
if grep -qE 'backend/tests/' <<<"$CODE_ONLY"; then
  bad "predeploy_check.sh référence encore un chemin backend/tests/ exécuté (test applicatif sur le host)"
else
  ok "aucun chemin backend/tests/ exécuté sur le host"
fi
echo

echo "═══ Cas B : scripts/validate_inventory.py tourne sans le paquet 'ladini' ═══"
# Un interpréteur qui n'a PAS 'ladini' d'installé est exactement la
# condition réelle sur un host de prod frais (l'image runtime elle-même ne
# contient pas backend/tests, et le HOST n'a ni Poetry ni le package
# installé en dehors de l'image Docker). On le prouve en cherchant un
# interpréteur système où `import ladini` échoue déjà — pas besoin de le
# fabriquer, c'est la norme hors d'un venv projet.
PYTHON_CANDIDATE=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if ! "$candidate" -c "import ladini" >/dev/null 2>&1; then
      PYTHON_CANDIDATE="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_CANDIDATE" ]; then
  echo "  (aucun interpréteur système sans 'ladini' trouvé pour prouver le Cas B dans CET environnement — sauté, pas un échec)"
else
  FIXTURE="${SANDBOX}/inventory.yml"
  cat > "$FIXTURE" <<'EOF'
nodes:
  - name: node-a
    host: 10.20.1.10
    roles: [app, scheduler, admin]
EOF
  if "$PYTHON_CANDIDATE" -c "import ladini" >/dev/null 2>&1; then
    bad "sanity check : ${PYTHON_CANDIDATE} a bien 'ladini' importable — le Cas B ne prouve rien ici"
  else
    ok "sanity check : '${PYTHON_CANDIDATE} -c \"import ladini\"' échoue bien (condition réelle d'un host de prod)"
  fi
  OUT_B_LOG="${SANDBOX}/validate_inventory.log"
  if "$PYTHON_CANDIDATE" "${ROOT}/scripts/validate_inventory.py" "$FIXTURE" >"$OUT_B_LOG" 2>&1; then
    ok "validate_inventory.py s'exécute avec succès SANS le paquet 'ladini' installé"
  else
    bad "validate_inventory.py a échoué sans 'ladini' — n'est plus stdlib-pur ? $(cat "$OUT_B_LOG" 2>/dev/null)"
  fi
fi
echo

echo "═══ Cas C : predeploy_check.sh (PYTHON_BIN forcé) ne lève aucun ModuleNotFoundError applicatif ═══"
if [ -z "$PYTHON_CANDIDATE" ]; then
  echo "  (sauté — dépend du même interpréteur que le Cas B, indisponible ici)"
else
  OUT_C="$(PYTHON_BIN="$PYTHON_CANDIDATE" bash "$SCRIPT" 2>&1 || true)"
  if grep -qE "ModuleNotFoundError.*(ladini|sqlalchemy|pydantic|langgraph)" <<<"$OUT_C"; then
    bad "predeploy_check.sh tente encore d'importer une dépendance backend applicative"
    grep -E "ModuleNotFoundError" <<<"$OUT_C"
  else
    ok "aucune trace de ModuleNotFoundError applicatif dans la sortie de predeploy_check.sh"
  fi
  if grep -qE "PREDEPLOY CHECK: (PASS|FAIL)" <<<"$OUT_C"; then
    ok "predeploy_check.sh atteint bien son verdict final (ne meurt pas prématurément)"
  else
    bad "predeploy_check.sh n'a pas atteint son bloc de verdict final"
  fi
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
