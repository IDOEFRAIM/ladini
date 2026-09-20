#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-valkey-bind-wireguard-order.sh — régression sur
# scripts/configure_valkey_bind_wireguard.sh (2026-09-20, blocker #5 —
# ne JAMAIS configurer `bind <IP WireGuard>` avant que cette IP existe
# réellement sur l'interface wg0) et
# scripts/detect_valkey_systemd_unit.sh (blocker #4 — ne jamais supposer
# `valkey.service`, détecter le vrai nom d'unit installé).
#
# Couvre :
#   Cas A : wg0 absent — le script REFUSE (exit != 0), aucune trace de
#           modification tentée au-delà du diagnostic.
#   Cas B : wg0 présent mais SANS l'IP attendue — REFUS également, avec un
#           message qui montre la sortie réelle de `ip addr show` pour
#           diagnostic.
#   Cas C : wg0 présent AVEC l'IP attendue — le script PROGRESSE au-delà
#           du garde (atteint l'étape d'écriture de valkey.conf).
#   Cas D : détection d'unit — un seul match (`valkey-server.service`) ->
#           retourné tel quel.
#   Cas E : détection d'unit — aucun match -> échec explicite, jamais un
#           nom par défaut deviné.
#   Cas F : détection d'unit — plusieurs matches -> échec explicite
#           (ambigu, décision manuelle requise), jamais un choix arbitraire.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "═══ Cas A : wg0 absent — REFUS ═══"
cat > "${SANDBOX}/ip" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "addr" ] && [ "$2" = "show" ]; then exit 1; fi
EOF
chmod +x "${SANDBOX}/ip"
OUT_A="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/configure_valkey_bind_wireguard.sh" --dry-run 2>&1)"
RC_A=$?
if [ "$RC_A" -ne 0 ] && grep -q "interface wg0 absente" <<<"$OUT_A"; then
  ok "wg0 absent -> refus explicite, rc=${RC_A}"
else
  bad "aurait dû refuser (rc=${RC_A}) : ${OUT_A}"
fi
echo

echo "═══ Cas B : wg0 présent, mauvaise IP — REFUS ═══"
cat > "${SANDBOX}/ip" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "addr" ] && [ "$2" = "show" ]; then
  printf 'inet 10.200.0.99/24 scope global wg0\n'
fi
EOF
chmod +x "${SANDBOX}/ip"
OUT_B="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/configure_valkey_bind_wireguard.sh" --dry-run 2>&1)"
RC_B=$?
if [ "$RC_B" -ne 0 ] && grep -q "n'apparaît PAS sur wg0" <<<"$OUT_B"; then
  ok "mauvaise IP -> refus explicite, rc=${RC_B}"
else
  bad "aurait dû refuser (rc=${RC_B}) : ${OUT_B}"
fi
echo

echo "═══ Cas C : wg0 présent AVEC la bonne IP — progresse ═══"
cat > "${SANDBOX}/ip" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "addr" ] && [ "$2" = "show" ]; then
  printf 'inet 10.200.0.2/24 scope global wg0\n'
fi
EOF
chmod +x "${SANDBOX}/ip"
OUT_C="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/configure_valkey_bind_wireguard.sh" --dry-run 2>&1)"
if grep -q "IP 10.200.0.2 présente sur wg0" <<<"$OUT_C" && grep -q "4/8 : écriture" <<<"$OUT_C"; then
  ok "bonne IP présente -> le script dépasse le garde et atteint l'étape d'écriture"
else
  bad "aurait dû progresser jusqu'à l'étape d'écriture : ${OUT_C}"
fi
echo

echo "═══ Cas D : détection unit — un seul match ═══"
cat > "${SANDBOX}/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list-unit-files" ]; then
  printf 'UNIT FILE\tSTATE\nvalkey-server.service\tenabled\nssh.service\tenabled\n'
fi
EOF
chmod +x "${SANDBOX}/systemctl"
OUT_D="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/detect_valkey_systemd_unit.sh")"
RC_D=$?
if [ "$RC_D" -eq 0 ] && [ "$OUT_D" = "valkey-server.service" ]; then
  ok "unit détectée correctement : ${OUT_D}"
else
  bad "attendu 'valkey-server.service', obtenu (rc=${RC_D}) : ${OUT_D}"
fi
echo

echo "═══ Cas E : détection unit — aucun match ═══"
cat > "${SANDBOX}/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list-unit-files" ]; then
  printf 'UNIT FILE\tSTATE\nssh.service\tenabled\n'
fi
EOF
chmod +x "${SANDBOX}/systemctl"
OUT_E="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/detect_valkey_systemd_unit.sh" 2>&1)"
RC_E=$?
if [ "$RC_E" -ne 0 ] && grep -q "aucune unit systemd Valkey" <<<"$OUT_E"; then
  ok "aucun match -> échec explicite, jamais un nom deviné"
else
  bad "aurait dû échouer explicitement (rc=${RC_E}) : ${OUT_E}"
fi
echo

echo "═══ Cas F : détection unit — plusieurs matches (ambigu) ═══"
cat > "${SANDBOX}/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list-unit-files" ]; then
  printf 'UNIT FILE\tSTATE\nvalkey.service\tdisabled\nvalkey-server.service\tenabled\n'
fi
EOF
chmod +x "${SANDBOX}/systemctl"
OUT_F="$(PATH="${SANDBOX}:$PATH" bash "${ROOT}/scripts/detect_valkey_systemd_unit.sh" 2>&1)"
RC_F=$?
if [ "$RC_F" -ne 0 ] && grep -q "plusieurs unites Valkey" <<<"$OUT_F"; then
  ok "ambiguïté détectée -> échec explicite, jamais un choix arbitraire"
else
  bad "aurait dû échouer sur l'ambiguïté (rc=${RC_F}) : ${OUT_F}"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
