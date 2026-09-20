#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/detect_valkey_systemd_unit.sh — détecte le VRAI nom d'unit
# systemd pour Valkey (2026-09-20, cutover Upstash → Valkey) — ne suppose
# JAMAIS `valkey.service` : certaines installations Debian/Ubuntu
# installent `valkey-server.service` (paquet `valkey` upstream vs.
# `valkey-server` du dépôt Debian, noms différents selon la provenance du
# paquet). Toute commande `systemctl` de ce chantier doit utiliser le nom
# détecté ICI, jamais un nom en dur.
#
#   UNIT="$(bash scripts/detect_valkey_systemd_unit.sh)" && sudo systemctl restart "$UNIT"
#
# Peut aussi être SOURCÉ par un autre script (voir
# scripts/configure_valkey_bind_wireguard.sh) : expose la fonction
# `detect_valkey_systemd_unit` sans rien exécuter au chargement si
# $0 != ce fichier (garde `[[ "${BASH_SOURCE[0]}" == "$0" ]]`).
#
# Sortie stdout : le nom exact de l'unit (ex: "valkey-server.service"),
# RIEN d'autre — pensé pour être capturé dans une variable
# ($(...)). Sortie non-zéro si aucune unit Valkey trouvée, ou si
# PLUSIEURS unites Valkey distinctes sont installées (ambigu — un
# opérateur doit trancher manuellement plutôt que ce script ne devine).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

detect_valkey_systemd_unit() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "FATAL: systemctl indisponible sur cet hôte." >&2
    return 1
  fi

  # `list-unit-files` (pas `list-units`) : trouve l'unit MÊME si le
  # service n'est pas encore démarré (cas du tout premier provisioning,
  # avant le tout premier `systemctl start`) — `list-units` ne montrerait
  # rien tant qu'aucune instance n'a jamais tourné.
  local matches
  matches="$(systemctl list-unit-files 2>/dev/null | grep -iE '^valkey[a-z0-9_-]*\.service' | awk '{print $1}')"

  local count
  count="$(printf '%s\n' "$matches" | grep -c . || true)"

  if [ "$count" -eq 0 ]; then
    echo "FATAL: aucune unit systemd Valkey trouvée (recherché : valkey*.service). Valkey est-il installé ?" >&2
    return 1
  elif [ "$count" -gt 1 ]; then
    echo "FATAL: plusieurs unites Valkey trouvées, ambigu — trancher manuellement :" >&2
    printf '%s\n' "$matches" | sed 's/^/  /' >&2
    return 1
  fi

  printf '%s\n' "$matches"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  detect_valkey_systemd_unit
fi
