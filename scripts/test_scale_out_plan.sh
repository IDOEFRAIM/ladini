#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test_scale_out_plan.sh — verrou de non-régression scale-out
# (chantier Hetzner, audit 2026-09-19).
#
# Prouve la propriété demandée : « un scale-out N → N+1 ne détruit ni ne
# remplace AUCUN node/ressource déjà existant(e) ». Compare l'ensemble des
# adresses de ressources RÉELLEMENT en state AVANT le test à un
# `terraform plan` généré pour `app_node_count = N+1` — échoue bruyamment
# si l'une de ces adresses PRÉEXISTANTES apparaît en `delete` (seule ou
# dans un `replace`, càd `actions` contenant `"delete"`).
#
#   ./scripts/test_scale_out_plan.sh [terraform_dir] [var_file]
#   (défauts : infra/providers/hetzner, environments/production.tfvars)
#
# Mode par défaut : `terraform plan -refresh=false` — analyse STRUCTURELLE
# du diff de configuration à partir du DERNIER state connu, sans appel
# réseau à l'API Hetzner (donc exécutable sans connectivité/`hcloud_token`
# réel — un jeton syntaxiquement valide mais FAUX est utilisé si aucun
# n'est fourni). Ce n'est PAS un substitut à un `terraform plan` normal
# avant un vrai `apply` (qui, lui, rafraîchit l'état réel et peut détecter
# un drift externe) — voir l'option `--refresh` ci-dessous pour ce cas.
#
# Options :
#   --refresh   force un VRAI refresh (nécessite TF_VAR_hcloud_token/
#               HCLOUD_TOKEN réel + accès réseau à l'API Hetzner) — à
#               utiliser juste avant un `apply` réel pour la confirmation
#               finale la plus fidèle possible. Toujours sans état modifié
#               ni apply : ce script ne fait jamais qu'un `plan`.
#
# Ne fait JAMAIS `terraform apply`. N'écrit jamais dans le répertoire du
# module (plans/état temporaires dans un répertoire scratch, jamais commit).
# Exit 0 si la propriété tient, 1 sinon (message explicite).
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REFRESH_MODE=0
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --refresh) REFRESH_MODE=1 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

TF_DIR="${POSITIONAL[0]:-${HERE}/../infra/providers/hetzner}"
VAR_FILE="${POSITIONAL[1]:-environments/production.tfvars}"

TF_DIR="$(cd "$TF_DIR" && pwd)"
[ -f "${TF_DIR}/${VAR_FILE}" ] || {
  echo "[test_scale_out_plan] fichier var introuvable : ${TF_DIR}/${VAR_FILE}" >&2
  exit 1
}

command -v terraform >/dev/null 2>&1 || {
  echo "[test_scale_out_plan] \`terraform\` introuvable dans le PATH." >&2
  exit 1
}

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# ── Jeton : réel si fourni (obligatoire en mode --refresh), sinon un
# jeton syntaxiquement valide (64 car.) qui ne quitte jamais cette machine
# et ne sert qu'à satisfaire la validation CÔTÉ CLIENT du provider — voir
# `terraform providers schema`, aucun appel réseau n'est fait en dessous
# tant que `-refresh=false` est actif et qu'aucune donnée `data "hcloud_*"`
# n'existe dans ce module (vérifié — aucune à ce jour).
if [ "$REFRESH_MODE" = 1 ]; then
  : "${TF_VAR_hcloud_token:?--refresh nécessite TF_VAR_hcloud_token (jeton Hetzner réel)}"
else
  export TF_VAR_hcloud_token="${TF_VAR_hcloud_token:-$(printf 'a%.0s' $(seq 1 64))}"
fi

cd "$TF_DIR"

echo "[test_scale_out_plan] terraform init (backend local, pas de réseau requis pour un state déjà initialisé)…"
terraform init -input=false -backend=false >/dev/null 2>&1 || true
# `-backend=false` ci-dessus est volontairement TOLÉRÉ en échec : sur un
# répertoire déjà `init` (le cas normal, `.terraform/` présent — voir
# .terraform.lock.hcl commité), cette commande est un no-op utile mais pas
# strictement nécessaire ; on ne bloque jamais le test dessus.

echo "[test_scale_out_plan] adresses de ressources actuellement en state…"
BEFORE_ADDRESSES="$(terraform state list)"
if [ -z "$BEFORE_ADDRESSES" ]; then
  echo "[test_scale_out_plan] state vide — rien à protéger, ce test n'a pas de sens ici (aucune infrastructure réelle existante)." >&2
  exit 1
fi
echo "$BEFORE_ADDRESSES" | sed 's/^/    /'

# ⚠️ Le nombre de nodes RÉELLEMENT existants vient du STATE (adresses
# `hcloud_server.app[...]` déjà listées ci-dessus), JAMAIS de `$VAR_FILE` —
# un `.tfvars` peut légitimement être en avance sur le state (ex: un
# opérateur a déjà changé `app_node_count` localement pour un test, SANS
# avoir fait `apply` — exactement le scénario qui a déclenché cet audit).
# Se fier au fichier testerait un mauvais delta (ex: state à 1, fichier
# déjà à 2 → ce script testerait 2→3, pas 1→2, et masquerait un vrai
# problème sur la transition réelle qui reste à faire).
CURRENT_COUNT="$(printf '%s\n' "$BEFORE_ADDRESSES" | grep -cE '^hcloud_server\.app\[')"
[ "$CURRENT_COUNT" -gt 0 ] || {
  echo "[test_scale_out_plan] aucun hcloud_server.app[...] trouvé dans le state — rien à scale-out." >&2
  exit 1
}
NEXT_COUNT=$((CURRENT_COUNT + 1))
echo "[test_scale_out_plan] scale-out testé (depuis le STATE réel, pas ${VAR_FILE}) : app_node_count ${CURRENT_COUNT} → ${NEXT_COUNT}"

SCALED_VARS="${SCRATCH}/scaled.tfvars"
grep -vE '^\s*app_node_count\s*=' "$VAR_FILE" > "$SCALED_VARS"
printf 'app_node_count = %s\n' "$NEXT_COUNT" >> "$SCALED_VARS"

PLAN_ARGS=(-input=false -var-file="$SCALED_VARS" -out="${SCRATCH}/scale.tfplan")
[ "$REFRESH_MODE" = 1 ] || PLAN_ARGS=(-refresh=false "${PLAN_ARGS[@]}")

if [ "$REFRESH_MODE" = 1 ]; then
  PLAN_MODE_LABEL="refresh réel"
else
  PLAN_MODE_LABEL="structurel, -refresh=false"
fi
echo "[test_scale_out_plan] terraform plan (${PLAN_MODE_LABEL})…"
if ! terraform plan "${PLAN_ARGS[@]}" >"${SCRATCH}/plan.log" 2>&1; then
  echo "[test_scale_out_plan] ÉCHEC : terraform plan a échoué :" >&2
  cat "${SCRATCH}/plan.log" >&2
  exit 1
fi

terraform show -json "${SCRATCH}/scale.tfplan" > "${SCRATCH}/plan.json"

echo "$BEFORE_ADDRESSES" > "${SCRATCH}/before_addresses.txt"

"$PYTHON_BIN" - "${SCRATCH}/plan.json" "${SCRATCH}/before_addresses.txt" <<'PYEOF'
import json
import sys

# Windows console (cp1252 par défaut) ne sait pas encoder ✗/— — force
# l'UTF-8, même convention que scripts/validate_inventory.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

plan_path, before_path = sys.argv[1], sys.argv[2]

with open(before_path, encoding="utf-8") as f:
    before_addresses = {line.strip() for line in f if line.strip()}

with open(plan_path, encoding="utf-8") as f:
    plan = json.load(f)

changes = plan.get("resource_changes", [])
violations = []
created = 0

for change in changes:
    address = change.get("address")
    actions = set(change.get("change", {}).get("actions", []))
    # Une ressource `moved` (adresse renommée, ex. count -> for_each) porte
    # `previous_address` — l'ancienne adresse ÉTAIT dans `before_addresses`,
    # la nouvelle ne l'est pas encore : ce n'est ni une création ni une
    # destruction réelle, on ne la compte dans aucun des deux compteurs.
    previous_address = change.get("previous_address")
    is_pure_move = previous_address and previous_address != address and actions == {"no-op"}

    if "create" in actions and "delete" not in actions and not previous_address:
        created += 1

    was_existing = address in before_addresses or (previous_address in before_addresses if previous_address else False)
    if was_existing and "delete" in actions and not is_pure_move:
        violations.append((address, sorted(actions)))

if violations:
    print("[test_scale_out_plan] VIOLATION — ressource(s) EXISTANTE(S) détruite(s)/remplacée(s) :", file=sys.stderr)
    for addr, actions in violations:
        print(f"    ✗ {addr}  actions={actions}", file=sys.stderr)
    sys.exit(1)

if created == 0:
    print("[test_scale_out_plan] ATTENTION — aucune ressource créée : le plan de scale-out ne fait rien (app_node_count a-t-il bien changé ?).", file=sys.stderr)
    sys.exit(1)

print(f"[test_scale_out_plan] OK — 0 destruction/remplacement de ressource existante, {created} ressource(s) créée(s).")
PYEOF

echo "[test_scale_out_plan] ✓ propriété vérifiée : le scale-out ${CURRENT_COUNT} → ${NEXT_COUNT} n'affecte aucune ressource existante."
