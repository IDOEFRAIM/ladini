#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-ufw-firewall.sh — régression sur infra/firewall/ufw.sh.
#
# Couvre (2026-09-17, incident réel "ufw status: inactive" post-apply
# Hetzner malgré cloud-init `done`) :
#   Cas A : PRIVATE_NET_CIDR défini    -> 80/443 restreints à ce CIDR,
#           JAMAIS ouverts au monde, jamais de règle 443/udp.
#   Cas B : PRIVATE_NET_CIDR absent    -> comportement public historique
#           (80/443/443-udp ouverts à tous), provider-neutral.
#   Cas C : --dry-run                  -> aucune commande `ufw` n'est
#           réellement exécutée (seulement affichée), quel que soit le cas.
#   Cas D : sshd_config SANS directive "Port" active (le cas NORMAL sur
#           Ubuntu) -> le script ne doit PAS mourir silencieusement (c'est
#           exactement le bug root-cause de l'incident réel, voir ufw.sh
#           en-tête) ; SSH_PORT doit retomber sur 22 par défaut.
#
# Exécute infra/firewall/ufw.sh en --dry-run UNIQUEMENT (jamais de vraie
# commande ufw) : ne nécessite ni root ni le paquet `ufw` installé — voir
# le garde conditionnel ajouté dans ufw.sh (root/ufw requis seulement si
# DRY=0). Portable (bash pur, pas de mock binaire nécessaire).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UFW_SH="${ROOT}/infra/firewall/ufw.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

assert_contains() {
  local haystack="$1" needle="$2" label="$3"
  if grep -qF -- "$needle" <<<"$haystack"; then ok "$label"; else bad "$label (attendu: \"$needle\")"; fi
}
assert_not_contains() {
  local haystack="$1" needle="$2" label="$3"
  if grep -qF -- "$needle" <<<"$haystack"; then bad "$label (NE devait PAS contenir: \"$needle\")"; else ok "$label"; fi
}

# sshd_config fixture SANS directive "Port" active — reproduit exactement
# la config par défaut d'Ubuntu (celle qui a fait planter la version
# buguée du script, voir Cas D).
SSHD_FIXTURE="${SANDBOX}/sshd_config"
cat > "$SSHD_FIXTURE" <<'EOF'
# Config sshd minimale, sans directive Port active (cas par défaut Ubuntu)
Include /etc/ssh/sshd_config.d/*.conf
PermitRootLogin no
EOF
export SSHD_CONFIG_PATH="$SSHD_FIXTURE"

echo "═══ Cas A : PRIVATE_NET_CIDR=10.20.0.0/16 (topologie LB Hetzner) ═══"
OUT_A="$(PRIVATE_NET_CIDR=10.20.0.0/16 bash "$UFW_SH" --dry-run 2>&1)"
RC_A=$?
if [ "$RC_A" -ne 0 ]; then
  bad "le script doit sortir en 0 (sortie: ${RC_A})"
  echo "$OUT_A"
else
  assert_not_contains "$OUT_A" "+ ufw allow 80/tcp" "pas de 'ufw allow 80/tcp' global"
  assert_not_contains "$OUT_A" "+ ufw allow 443/tcp" "pas de 'ufw allow 443/tcp' global"
  assert_not_contains "$OUT_A" "+ ufw allow 443/udp" "pas de 'ufw allow 443/udp'"
  assert_contains "$OUT_A" "+ ufw allow from 10.20.0.0/16 to any port 80 proto tcp" "80/tcp restreint au CIDR privé"
  assert_contains "$OUT_A" "+ ufw allow from 10.20.0.0/16 to any port 443 proto tcp" "443/tcp restreint au CIDR privé"
  assert_contains "$OUT_A" "+ ufw limit 22/tcp" "SSH (port 22, fallback) toujours en limit"
fi
echo

echo "═══ Cas B : PRIVATE_NET_CIDR absent (exposition directe) ═══"
OUT_B="$(unset PRIVATE_NET_CIDR; bash "$UFW_SH" --dry-run 2>&1)"
RC_B=$?
if [ "$RC_B" -ne 0 ]; then
  bad "le script doit sortir en 0 (sortie: ${RC_B})"
  echo "$OUT_B"
else
  assert_contains "$OUT_B" "+ ufw allow 80/tcp" "80/tcp ouvert publiquement"
  assert_contains "$OUT_B" "+ ufw allow 443/tcp" "443/tcp ouvert publiquement"
  assert_contains "$OUT_B" "+ ufw allow 443/udp" "443/udp ouvert publiquement (HTTP/3, exposition directe)"
  assert_not_contains "$OUT_B" "ufw allow from" "aucune règle restreinte à un CIDR (pas de LB devant)"
fi
echo

echo "═══ Cas C : --dry-run n'exécute RIEN réellement ═══"
# Espionne : remplace ufw par un binaire qui, s'il est réellement appelé,
# écrit un marqueur détectable. Le script doit sortir 0 SANS jamais
# invoquer ce binaire (DRY=1 -> run() ne fait qu'un echo).
BIN="${SANDBOX}/bin"; mkdir -p "$BIN"
MARKER="${SANDBOX}/ufw_was_called"
cat > "${BIN}/ufw" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "${MARKER}"
exit 0
EOF
chmod +x "${BIN}/ufw"
OUT_C="$(PATH="${BIN}:${PATH}" PRIVATE_NET_CIDR=10.20.0.0/16 bash "$UFW_SH" --dry-run 2>&1)"
RC_C=$?
if [ "$RC_C" -ne 0 ]; then
  bad "le --dry-run doit sortir en 0 (sortie: ${RC_C})"
  echo "$OUT_C"
elif [ -f "$MARKER" ]; then
  bad "--dry-run a réellement invoqué 'ufw' (marqueur trouvé) : $(cat "$MARKER")"
else
  ok "--dry-run n'a exécuté aucune commande ufw réelle"
fi
assert_contains "$OUT_C" "+ ufw --force reset" "--dry-run affiche quand même les commandes qui SERAIENT exécutées"
echo

echo "═══ Cas D : sshd_config SANS directive Port active (régression incident réel) ═══"
# C'est le fixture par défaut (SSHD_CONFIG_PATH ci-dessus). Avant le fix,
# ce cas précis faisait mourir le script silencieusement (set -e + pipefail
# sur un grep sans résultat), AVANT même le premier 'ufw reset' — exactement
# le bug qui a laissé ufw 'inactive' après un vrai terraform apply.
OUT_D="$(bash "$UFW_SH" --dry-run 2>&1)"
RC_D=$?
if [ "$RC_D" -ne 0 ]; then
  bad "le script ne doit PAS mourir quand sshd_config n'a pas de directive Port (sortie: ${RC_D}) — RÉGRESSION du bug incident réel"
  echo "$OUT_D"
else
  ok "le script ne meurt pas silencieusement sans directive Port active"
  assert_contains "$OUT_D" "Port SSH détecté : 22" "fallback correct sur le port 22 par défaut"
  assert_contains "$OUT_D" "+ ufw --force reset" "le script continue bien jusqu'à 'ufw reset' (preuve qu'il n'est pas mort avant)"
  assert_contains "$OUT_D" "+ ufw status verbose" "le script va jusqu'au bout (status verbose final)"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
