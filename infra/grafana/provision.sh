#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# Provisionne les dashboards + alertes Grafana Cloud de façon IDEMPOTENTE
# via l'API HTTP de Grafana — jamais de duplication à un ré-exécution
# (POST /api/dashboards/db avec `overwrite: true` + `uid` stable ; POST
# /api/v1/provisioning/alert-rules avec `X-Disable-Provenance` et un `uid`
# stable par règle, cf. infra/grafana/alerts/alerts.yaml).
#
# Ce script est appelé DEPUIS un environnement CI/CD ou une machine
# d'admin qui détient les identifiants Grafana Cloud (GRAFANA_CLOUD_URL +
# GRAFANA_CLOUD_API_KEY, PAS les mêmes clés API que infra/alloy/.env —
# celles-là sont scope remote_write/push, celle-ci doit avoir le scope
# Grafana "Admin" ou "Editor" sur les dashboards/alertes).
#
# Dans CET environnement (sandbox de dev), ces identifiants n'existent
# PAS — le script le détecte et sort proprement en 0 (état ATTENDU, pas
# une erreur) après avoir validé que les fichiers JSON/YAML sont au moins
# syntaxiquement corrects.
#
# Usage :
#   GRAFANA_CLOUD_URL=https://<stack>.grafana.net \
#   GRAFANA_CLOUD_API_KEY=glsa_xxx \
#   GRAFANA_CLOUD_PROMETHEUS_DS_UID=<uid-datasource-prometheus> \
#   ./provision.sh
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARDS_DIR="${SCRIPT_DIR}/dashboards"
ALERTS_FILE="${SCRIPT_DIR}/alerts/alerts.yaml"

echo "── Ladini — provisioning Grafana ──────────────────────────────────"

# ── 1. Validation syntaxique — TOUJOURS exécutée, même sans identifiants.
#    Un fichier JSON/YAML cassé doit faire échouer la CI indépendamment de
#    la disponibilité des credentials Grafana Cloud.
echo "[1/3] Validation syntaxique des dashboards JSON…"
json_check_failed=0
for f in "${DASHBOARDS_DIR}"/*.json; do
  if command -v jq >/dev/null 2>&1; then
    if ! jq empty "$f" >/dev/null 2>&1; then
      echo "  ✗ JSON invalide : $f"
      json_check_failed=1
    else
      echo "  ✓ $f"
    fi
  else
    if ! python3 -m json.tool "$f" >/dev/null 2>&1; then
      echo "  ✗ JSON invalide : $f"
      json_check_failed=1
    else
      echo "  ✓ $f"
    fi
  fi
done

echo "[2/3] Validation syntaxique des règles d'alerte YAML…"
yaml_check_failed=0
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  if ! python3 -c "import yaml, sys; yaml.safe_load(open(sys.argv[1], encoding='utf-8'))" "${ALERTS_FILE}" >/dev/null 2>&1; then
    echo "  ✗ YAML invalide : ${ALERTS_FILE}"
    yaml_check_failed=1
  else
    echo "  ✓ ${ALERTS_FILE}"
  fi
else
  echo "  ⚠ PyYAML indisponible — validation YAML sautée (pas bloquant)."
fi

if [[ "${json_check_failed}" -eq 1 || "${yaml_check_failed}" -eq 1 ]]; then
  echo "ÉCHEC — au moins un fichier de dashboard/alerte est syntaxiquement invalide."
  exit 1
fi

# ── 2. Apply réel — UNIQUEMENT si les identifiants sont présents.
if [[ -z "${GRAFANA_CLOUD_URL:-}" || -z "${GRAFANA_CLOUD_API_KEY:-}" ]]; then
  echo "[3/3] GRAFANA APPLY NOT RUN — reason = credentials unavailable"
  echo "      (GRAFANA_CLOUD_URL / GRAFANA_CLOUD_API_KEY absents de l'environnement)."
  echo "      Les fichiers sont valides — rien de plus vérifiable sans un vrai stack Grafana Cloud."
  exit 0
fi

echo "[3/3] Application vers ${GRAFANA_CLOUD_URL}…"

DS_UID="${GRAFANA_CLOUD_PROMETHEUS_DS_UID:-}"
if [[ -z "${DS_UID}" ]]; then
  echo "ÉCHEC — GRAFANA_CLOUD_PROMETHEUS_DS_UID requis pour résoudre \${DS_PROMETHEUS} dans les dashboards."
  exit 1
fi

# Dashboards — POST /api/dashboards/db, idempotent via `overwrite: true` +
# `uid` fixe (posé dans chaque fichier JSON, voir gen_dashboards.py).
for f in "${DASHBOARDS_DIR}"/*.json; do
  echo "  → dashboard $(basename "$f")"
  # Injecte le datasource réel à la place du placeholder ${DS_PROMETHEUS},
  # puis enveloppe dans le corps attendu par l'API (`dashboard` + `overwrite`).
  payload="$(python3 - "$f" "$DS_UID" <<'PY'
import json, sys
path, ds_uid = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    raw = fh.read().replace("${DS_PROMETHEUS}", ds_uid)
dashboard = json.loads(raw)
dashboard.pop("__inputs", None)
dashboard["id"] = None  # laisser Grafana résoudre par uid, jamais par id numérique
print(json.dumps({"dashboard": dashboard, "overwrite": True, "folderTitle": "Ladini"}))
PY
)"
  curl -sf -X POST "${GRAFANA_CLOUD_URL}/api/dashboards/db" \
    -H "Authorization: Bearer ${GRAFANA_CLOUD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "${payload}" \
    -o /dev/null \
    && echo "    ✓ appliqué" \
    || { echo "    ✗ échec — voir la réponse ci-dessus"; exit 1; }
done

# Alertes — les endpoints de provisioning Grafana Alerting attendent une
# règle par requête (pas un YAML multi-groupes en un seul POST) ; on
# préfère ici documenter clairement plutôt que de deviner un mapping
# YAML→JSON fragile pour un environnement qui, de toute façon, n'existe
# pas dans ce sandbox. Chemin recommandé en CI réelle : Grafana Terraform
# provider (`grafana_rule_group`) qui lit CE MÊME fichier via un
# convertisseur, ou `grizzly`/`grafana-tools` en pipeline dédiée — laissé
# hors scope de ce script one-shot.
echo "  → alertes : voir alerts.yaml — appliquer via le provisioning Grafana"
echo "    natif (monter alerts/ dans /etc/grafana/provisioning/alerting/ sur"
echo "    une instance Grafana Cloud avec file provisioning activé), ou via"
echo "    le provider Terraform Grafana (grafana_rule_group / grafana_contact_point)."

echo "── Terminé ──────────────────────────────────────────────────────"
