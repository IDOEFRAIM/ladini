#cloud-config
# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/cloud-init/app-node.yaml.tpl — bootstrap d'un
# node Hetzner (chantier scale-out, § IaC Hetzner).
#
# Rendu par Terraform (templatefile() dans servers.tf) et passé en
# `user_data` à hcloud_server. S'exécute UNE SEULE FOIS au premier boot.
#
# CE QUE CE FICHIER FAIT (infrastructure du node) :
#   - installe Docker Engine + le plugin compose ;
#   - crée l'utilisateur de déploiement (${deploy_user}), membre du groupe
#     docker, avec la clé SSH admin injectée ;
#   - clone le dépôt Ladini (checkout requis par scripts/deploy.sh et par
#     ce cloud-init lui-même pour invoquer infra/firewall/ufw.sh) ;
#   - applique le pare-feu hôte via infra/firewall/ufw.sh (RÉFÉRENCÉ, pas
#     dupliqué — voir ce script pour le détail des ports) ;
#   - prépare le répertoire Alloy (placeholder — la config complète Alloy
#     est un autre chantier, volontairement hors scope ici) ;
#   - durcit SSH (désactive l'auth par mot de passe et le login root).
#
# CE QUE CE FICHIER NE FAIT PAS (hors scope, réservé à scripts/deploy.sh) :
#   - ne lance PAS `docker compose up` ;
#   - n'écrit PAS le `.env` applicatif (secrets réels — jamais dans
#     user_data, qui est lisible via l'API des metadata Hetzner par tout
#     process root sur le node) ;
#   - ne configure PAS le reverse proxy (infra/reverse-proxy/), qui a son
#     propre cycle de vie (cd infra/reverse-proxy && docker compose up -d,
#     voir son README/commentaires) ;
#   - ne configure PAS Alloy (observabilité — chantier séparé).
#
# Rôles Compose (profiles) destinés à ce node, pour référence humaine
# uniquement (ce cloud-init ne les utilise pas directement) — reportez ces
# valeurs dans infra/inventory.yml (copié depuis infra/inventory.example.yml)
# une fois le node up, voir scripts/validate_inventory.py :
#   node_name  : ${node_name}
#   roles      : ${node_roles}
# ═════════════════════════════════════════════════════════════════════

package_update: true
package_upgrade: false

users:
  - name: ${deploy_user}
    groups: [docker, sudo]
    shell: /bin/bash
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - ${ssh_public_key}

write_files:
  # Placeholder Alloy — répertoire + config vide prête à être remplie par un
  # chantier ultérieur (voir docs, "chantier Alloy" pas encore écrit à cette
  # date). Volontairement PAS de config Alloy réelle ici.
  - path: /etc/alloy/config.alloy
    owner: root:root
    permissions: "0644"
    content: |
      // Placeholder — config Alloy réelle non fournie par ce cloud-init.
      // Voir un chantier séparé pour l'observabilité (metrics/logs/traces).
      // Ne pas démarrer le service alloy tant que ce fichier n'est pas
      // remplacé par une vraie configuration.

  # Durcissement SSH minimal (en plus du firewall) — pas de mot de passe,
  # pas de login root direct. Le port SSH courant reste celui détecté par
  # infra/firewall/ufw.sh (défaut 22, non modifié ici).
  - path: /etc/ssh/sshd_config.d/99-ladini-hardening.conf
    owner: root:root
    permissions: "0644"
    content: |
      PasswordAuthentication no
      PermitRootLogin no
      X11Forwarding no


  # ── Réseau privé Hetzner ────────────────────────────────────────────
  # La NIC privée est enp7s0 sur les VMs Hetzner de cette topologie.
  # Sans cette configuration, l'interface reste DOWN et le Load Balancer
  # ne peut pas joindre Caddy sur 80/443 via le réseau 10.20.0.0/16.
  - path: /etc/netplan/60-ladini-private.yaml
    owner: root:root
    permissions: "0600"
    content: |
      network:
        version: 2
        renderer: networkd
        ethernets:
          enp7s0:
            dhcp4: true

runcmd:
  # ── Activation du réseau privé Hetzner ─────────────────────────────
  # Fail-closed : le node ne doit pas être déclaré prêt si sa NIC privée
  # n'est pas opérationnelle, car le LB cible le serveur par cette NIC.
  - |
    echo "[ladini] configuration du réseau privé Hetzner (enp7s0)"
    if ! netplan generate; then
      echo "[ladini] FATAL: netplan generate a échoué" >&2
      exit 1
    fi

    if ! netplan apply; then
      echo "[ladini] FATAL: netplan apply a échoué" >&2
      exit 1
    fi

    PRIVATE_NET_READY=0
    for i in $(seq 1 20); do
      if ip -4 addr show dev enp7s0 2>/dev/null | grep -q "inet "; then
        PRIVATE_NET_READY=1
        break
      fi
      sleep 1
    done

    if [ "$PRIVATE_NET_READY" -ne 1 ]; then
      echo "[ladini] FATAL: enp7s0 n'a reçu aucune IPv4 privée" >&2
      ip -br addr >&2 || true
      exit 1
    fi

    echo "[ladini] réseau privé OK :"
    ip -br addr show enp7s0

  # ── Docker Engine + plugin compose (dépôt officiel Docker) ──────────
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - >
    echo "deb [arch=$(dpkg --print-architecture)
    signed-by=/etc/apt/keyrings/docker.asc]
    https://download.docker.com/linux/ubuntu
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" |
    tee /etc/apt/sources.list.d/docker.list > /dev/null
  - apt-get update -y
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git ufw
  - systemctl enable --now docker

  # ── Répertoires applicatifs ─────────────────────────────────────────
  - install -d -o ${deploy_user} -g ${deploy_user} /opt/ladini
  - install -d -o ${deploy_user} -g ${deploy_user} /opt/ladini/deploy/releases

  # ── Checkout du dépôt (requis par scripts/deploy.sh ET par ufw.sh
  #    invoqué juste après) — clone en tant que deploy_user, pas root.
  - >
    sudo -u ${deploy_user} git clone --branch ${git_ref} --depth 1
    ${git_repo_url} /opt/ladini/app || (cd /opt/ladini/app && sudo -u
    ${deploy_user} git fetch origin ${git_ref} && sudo -u ${deploy_user}
    git checkout ${git_ref})

  # ── Pare-feu hôte — RÉFÉRENCE le script du dépôt, ne le duplique pas ──
  # `PRIVATE_NET_CIDR` (2026-09-17, audit Cloudflare/LB) : cette topologie a
  # TOUJOURS un Load Balancer Hetzner devant (voir load_balancer.tf) — le
  # host n'a donc jamais besoin d'exposer 80/443 à 0.0.0.0/0, seulement au
  # réseau privé par lequel le LB route réellement (défense en profondeur,
  # même raisonnement que infra/providers/hetzner/firewall.tf).
  #
  # Bloc explicite, pas une simple ligne (2026-09-17, incident réel : `ufw
  # status` restait `inactive` après un `terraform apply`, alors que
  # cloud-init affichait `done`) — `runcmd` concatène TOUTES ses commandes
  # en UN SEUL script `/bin/sh`, SANS `set -e` : un échec au milieu (quelle
  # qu'en soit la cause) ne fait PAS échouer cloud-init, puisque sa DERNIÈRE
  # commande (`systemctl restart ssh || true`, plus bas) réussit toujours.
  # Ce bloc rend l'échec VISIBLE (log dédié + `exit 1` qui stoppe net le
  # reste du bootstrap) au lieu de le laisser disparaître en silence. La
  # cause racine du bug d'origine (ufw.sh mourait dans sa détection du port
  # SSH) est corrigée dans ufw.sh lui-même — ce bloc est une défense en
  # profondeur pour que toute régression future soit visible immédiatement.
  - |
    echo "[ladini] application du pare-feu hôte (ufw.sh, PRIVATE_NET_CIDR=${private_net_cidr})"
    if PRIVATE_NET_CIDR=${private_net_cidr} bash /opt/ladini/app/infra/firewall/ufw.sh >/var/log/ladini-ufw.log 2>&1; then
      echo "[ladini] ufw.sh OK — voir /var/log/ladini-ufw.log"
    else
      echo "[ladini] FATAL: ufw.sh a echoue - voir /var/log/ladini-ufw.log - abandon du bootstrap" >&2
      exit 1
    fi

  # ── Alloy : dossier de données créé, service PAS activé (placeholder) ──
  - install -d -o root -g root /var/lib/alloy

  - systemctl restart ssh || systemctl restart sshd || true

# Note : `final_message` ci-dessous utilise un `$$` avant l'accolade —
# échappe l'interpolation Terraform pour laisser passer la variable
# LITTÉRALEMENT dans le cloud-init généré. C'est cloud-init lui-même (module
# cc_final_message), PAS Terraform, qui substitue cette variable au boot
# (temps écoulé depuis le démarrage). Sans l'échappement, templatefile() la
# traite comme une variable Terraform manquante et échoue "terraform validate"
# (confirmé — aucune valeur correspondante n'existe côté Terraform, ni ne devrait).
# ⚠️ Ne PAS écrire le nom de cette variable dans ce commentaire sans le
# préfixer de `$$` : templatefile() interpole aussi le texte des commentaires.
final_message: "Ladini node (${node_name}, roles: ${node_roles}) prêt après $${uptime}s. Étape suivante : infra/inventory.yml + scripts/deploy.sh <release> depuis un poste avec accès SSH ${deploy_user}@<ip>."
