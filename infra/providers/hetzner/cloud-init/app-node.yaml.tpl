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

runcmd:
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
  - bash /opt/ladini/app/infra/firewall/ufw.sh

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
