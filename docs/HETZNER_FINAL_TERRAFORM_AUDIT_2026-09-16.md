# Audit final Terraform Hetzner — sécurité réseau & bootstrap

**Date** : 2026-09-17
**Scope** : `infra/providers/hetzner/` (Terraform), `infra/firewall/ufw.sh`, cloud-init.
**Contexte** : premier `terraform apply` réel visé sur le projet Hetzner Cloud
du domaine `ladini.tech` (registrar Namecheap, DNS/proxy Cloudflare actif).
Cible : `Internet → Cloudflare → Hetzner LB (public) → réseau privé Hetzner →
node(s) app`, jamais `Internet → node directement`.

**Ce document NE couvre PAS** : PostgreSQL/migrations/schémas (hors scope,
DB externe gérée par Drizzle), les intents métier A-G/Legacy Retirement.
**Aucun `terraform apply` n'a été exécuté pendant cet audit.**

---

## 1. Firewall — bypass Cloudflare/LB par IP publique du node

**Constat initial** : `hcloud_firewall.app` ouvrait TCP 80, TCP 443 et UDP 443
à `0.0.0.0/0` / `::/0`. Combiné au fait que chaque node app a une IP IPv4
publique (`public_net { ipv4_enabled = true }`, `servers.tf`), n'importe qui
pouvait appeler `http(s)://<IP_PUBLIQUE_DU_NODE>` directement, contournant
totalement Cloudflare (WAF, cache, masquage d'IP) et le Load Balancer.

**Fait Hetzner déterminant** (documentation officielle) : un Hetzner Cloud
Firewall filtre **uniquement l'interface réseau publique** d'un server. Le
trafic sur le réseau privé (`network.tf`, `10.20.0.0/16`) n'est **jamais**
filtré par ce firewall, quelles que soient ses règles. Le Load Balancer route
vers chaque node via sa **seule IP privée** (`use_private_ip = true`,
`load_balancer.tf:50`) — un fait indépendant des règles de `hcloud_firewall`.

**Conséquence exploitée** : retirer les règles publiques 80/443/443-udp du
firewall cloud **n'affecte en rien** le chemin `LB → node`, puisque ce chemin
ne passe jamais par ce firewall. Ça ferme en revanche complètement le bypass
par IP publique.

**Correctif appliqué** ([firewall.tf](../infra/providers/hetzner/firewall.tf)) :
- Règles TCP 80, TCP 443, UDP 443 **supprimées** de `hcloud_firewall.app`.
- Conservées : SSH (22/tcp, restreint à `var.admin_cidrs`) et ICMP (public,
  diagnostic, surface négligeable).
- Défense en profondeur alignée côté hôte : [infra/firewall/ufw.sh](../infra/firewall/ufw.sh)
  accepte désormais un `PRIVATE_NET_CIDR` optionnel (passé par le cloud-init,
  voir §4) qui restreint 80/443 au CIDR privé au lieu de `0.0.0.0/0` quand ce
  projet est déployé derrière un LB — sans rien casser pour un déploiement
  mono-node sans LB (comportement historique conservé si la variable est
  vide).

**ACME/Let's Encrypt** : aucun impact. Le challenge HTTP-01 de Caddy valide
via le **vrai enregistrement DNS public** (`api.ladini.tech` → Cloudflare
proxied → IP du LB), jamais en visant l'IP du node — il emprunte donc
exactement le même chemin que le trafic normal (Cloudflare → LB → node en
privé), jamais l'IP publique du node.

**Vérifié / non cassé** :
- Caddy : sert toujours sur le réseau Docker interne, atteint via le LB en
  privé — inchangé.
- LB healthchecks : le LB parle aux nodes en IP privée, jamais filtrée par ce
  firewall — inchangé, confirmé ci-dessus.
- TLS : aucune règle TLS ne dépendait du firewall cloud.
- SSH : règle intacte, non touchée (voir §7).

---

## 2. Cloudflare-only LB ou LB public — MVP

**Question** : faut-il restreindre le Load Balancer Hetzner aux seules IP
sortantes Cloudflare ?

**Constat technique** : `terraform providers schema -json` confirme que la
ressource `hcloud_firewall_attachment` du provider `hcloud` **n'expose pas**
de champ `load_balancer_ids` — un Hetzner Cloud Firewall ne peut structurellement
pas s'attacher à un Load Balancer (uniquement à des servers/label-selectors).
Restreindre le LB aux IP Cloudflare n'est donc pas atteignable proprement au
niveau Terraform/infra pour ce provider.

**Décision (conforme à l'instruction "ne sur-ingénierie pas")** : garder le LB
public pour ce MVP. C'est un choix standard et raisonnable à ce stade — le LB
ne fait que passer du TCP, il n'expose aucune logique métier, et Cloudflare
reste la couche WAF/cache en amont pour tout trafic légitime. Un LB public
n'est pas en soi une faute : le vrai risque (bypass complet de Cloudflare)
était le node en IP publique ouvert au monde, déjà corrigé au §1.

**Piste de durcissement future, hors scope MVP** : Cloudflare *Authenticated
Origin Pulls* (mTLS client-cert entre Cloudflare et l'origine) au niveau
applicatif (Caddy), si le trafic direct au LB devient une préoccupation
concrète. Non implémenté ici — noté comme amélioration future, pas un
blocage.

---

## 3. Flux HTTP/HTTPS/TLS — cohérence avec Caddy

**Documenté** dans [README.md](../infra/providers/hetzner/README.md) (§"Notes
de conception importantes", nouvelle entrée en tête) :

```
Client
  → TLS #1 : Cloudflare edge (certificat géré par Cloudflare)
  → Cloudflare (WAF/cache/proxy — record DNS "proxied", nuage orange)
  → TLS #2 (nouvelle connexion) : Cloudflare → Hetzner LB (IP publique du LB)
  → Load Balancer Hetzner : TCP passthrough pur, AUCUNE terminaison TLS
  → réseau privé Hetzner (10.20.0.0/16) → node (IP PRIVÉE)
  → Caddy sur le node : termine réellement TLS #2 (certificat Let's Encrypt,
    ACME HTTP-01) → reverse_proxy en clair vers api:8000 (réseau Docker interne)
```

**Terminaison TLS** : jamais au LB (TCP passthrough pur, `protocol = "tcp"`
sur les deux services, `load_balancer.tf:58` et `:87`) — c'est **Caddy**, sur
chaque node, qui termine réellement le TLS de bout Cloudflare→origine.
Intentionnel et documenté de longue date dans le fichier
(`load_balancer.tf:4-13`) : terminer au LB casserait le renouvellement ACME
(qui a besoin du vrai trafic HTTP par node) et forcerait à dupliquer un
certificat entre nodes.

**Mode Cloudflare SSL/TLS requis** : `Full` ou `Full (strict)`, **jamais
`Flexible`** — documenté explicitement dans le README. En mode `Flexible`,
Cloudflare enverrait du HTTP en clair à une origine qui n'écoute qu'en HTTPS
via son propre TLS (Caddy) → échec de connexion. `Full`/`Full (strict)`
fonctionnent car le certificat de Caddy est réel (Let's Encrypt, signé par une
CA publique).

**Cohérence confirmée** : healthcheck HTTPS du service 443 (`domain =
var.public_domain`, `path = "/health/ready"`, `tls = true`) parle
correctement à Caddy en HTTPS sur chaque node — voir §4 pour la séquence de
bootstrap qui permet à ce healthcheck de devenir vert.

---

## 4. Séquence de bootstrap — risque de deadlock DNS/ACME/healthcheck

**Risque analysé** : après `terraform apply`, le DNS de `api.ladini.tech`
n'existe pas encore (l'opérateur doit l'ajouter manuellement depuis l'output
`load_balancer_ipv4`), alors que le healthcheck HTTPS du LB attend déjà
`api.ladini.tech` + `/health/ready` + `tls=true`. Risque théorique de
deadlock : LB attend HTTPS valide, Caddy attend DNS pour ACME, DNS attend
l'IP du LB.

**Conclusion de l'analyse** : **pas de deadlock réel**, pour deux raisons
structurelles :
1. Le runbook existant dans `README.md` a **déjà** le bon ordre : DNS ajouté
   **avant** que Caddy ne soit démarré (le déploiement applicatif, via
   `scripts/deploy.sh`, est une étape postérieure et distincte de
   `terraform apply`). Le cloud-init lui-même ne lance jamais l'application
   (voir §12) — donc Caddy ne tente aucun challenge ACME avant que
   l'opérateur ait eu l'occasion de configurer le DNS.
2. Le healthcheck du LB, même s'il reste rouge (HTTP 000/timeout) pendant la
   fenêtre où Caddy n'est pas encore up, **ne bloque jamais `terraform
   apply`** — Terraform crée la ressource `hcloud_load_balancer_service`
   indépendamment de l'état de santé courant du target ; un service/target
   unhealthy est un état runtime normal et transitoire, jamais une erreur de
   plan/apply.

**Correctif appliqué** : pas de changement du type de healthcheck (HTTP au
lieu de HTTPS, ou TCP temporaire) — jugé **non nécessaire** puisque le vrai
risque n'existe pas, et un changement de ce type ajouterait de la complexité
opérationnelle (deux configurations à gérer/faire converger) pour un problème
qui n'en est pas un. À la place : un bloc d'avertissement a été ajouté dans
[README.md](../infra/providers/hetzner/README.md) juste après l'étape "Attendez
~1-2 minutes..." expliquant explicitement pourquoi l'ordre DNS→Caddy ne doit
jamais être inversé, ce qui se passe si on l'inverse (fenêtre unhealthy
auto-résolutive, ACME retente avec backoff, jamais de blocage d'`apply`), et
que le healthcheck du LB ne dépend jamais du DNS (il vise l'IP privée
directement — `domain` ne sert que de SNI/Host header HTTP).

---

## 5. UDP 443 — utilité réelle

**Constat** : le firewall ouvrait UDP 443 (HTTP/3/QUIC) alors que le service
LB Hetzner est **strictement TCP** (`protocol = "tcp"` sur les deux services
`http`/`https`, `load_balancer.tf:58,87`) — le LB ne forward jamais de trafic
UDP vers les nodes.

**Analyse** : Cloudflare négocie HTTP/3 uniquement entre lui-même et le
**client** ; sa propre connexion vers l'origine (ici le LB Hetzner) reste
HTTP(S) standard (TCP). Aucun paquet QUIC n'atteint donc jamais un node dans
cette topologie, avec ou sans la règle firewall.

**Correctif appliqué** : règle UDP 443 **retirée** de `hcloud_firewall.app`
([firewall.tf](../infra/providers/hetzner/firewall.tf)), et **non ajoutée**
côté `ufw.sh` quand `PRIVATE_NET_CIDR` est défini (documenté explicitement
dans le script — voir commentaire dédié). Elle reste disponible dans
`ufw.sh` **uniquement** pour le cas d'usage historique d'un node exposé
directement sans LB devant (topologie toujours valide dans ce repo pour un
mono-node hors Hetzner-LB).

---

## 6. IPv4 publique du node — nécessité

**Analyse** :
- **SSH admin** : nécessaire — l'opérateur/admin n'est **pas** sur le réseau
  privé Hetzner (`10.20.0.0/16`), donc l'accès SSH doit transiter par l'IP
  publique du node (`admin_cidrs`, restreint, voir §7).
- **Egress sortant** (apt/Docker pulls depuis GHCR, appels LLM Groq, WhatsApp
  Cloud API, Sentry, Langfuse, Grafana Cloud) : un Hetzner Cloud Server
  **sans** IP publique n'a par défaut **aucune passerelle NAT gérée** pour
  atteindre l'internet public — retirer l'IPv4 publique casserait donc tout
  trafic sortant, sauf à provisionner une passerelle NAT dédiée (server
  supplémentaire, complexité et coût en plus).

**Décision (conforme à "ne complexifie pas inutilement")** : conserver l'IPv4
publique. Le firewall (§1) est le bon niveau pour fermer ce qui doit l'être
(80/443/443-udp), pas l'absence d'IP publique — cette dernière casserait
l'admin et l'egress pour un gain de sécurité nul (le firewall ferme déjà
totalement le bypass HTTP).

**Documenté** dans [README.md](../infra/providers/hetzner/README.md).

---

## 7. SSH — politique inchangée

**Vérifié** : règle SSH de `hcloud_firewall.app` (`firewall.tf:51-57`)
restreinte à `var.admin_cidrs` (jamais `0.0.0.0/0`), avec **double
validation Terraform** dans `variables.tf:96-105` :
- refuse un `apply` si `admin_cidrs` est vide ;
- refuse explicitement `0.0.0.0/0` ou `::/0` dans la liste.

`environments/production.tfvars` définit `admin_cidrs = ["196.118.44.22/32"]`
— un seul `/32`, conforme. **Aucune modification apportée à cette politique**,
conformément à l'instruction explicite ("ne change pas cette politique sans
raison") — rien dans cet audit ne justifiait un changement ici.

---

## 8. Labels — régression sur sélecteurs/scripts/inventaire

**Correctif déjà en place avant cet audit** (session précédente) :
`role = join("-", local.app_node_roles[count.index])` →
`"app-scheduler-admin"` (au lieu de `"app,scheduler,admin"`, invalide pour
Hetzner — labels suivent la syntaxe Kubernetes, pas de virgule). Idem pour le
node scheduler dédié : `role = "scheduler-admin"`.

**Vérification (cet audit)** : recherche exhaustive dans le repo de tout
parsing programmatique du label Hetzner `role` — **aucun script, test ou
outil d'inventaire ne lit ce label**. `scripts/validate_inventory.py` et le
runbook lisent les **rôles** depuis `infra/inventory.yml` (rempli
manuellement par l'opérateur depuis l'output Terraform `app_nodes[].roles`,
qui reste une **liste** Terraform non affectée par ce changement de
séparateur, voir `outputs.tf:27`) — jamais depuis un label Hetzner. **Aucune
régression.**

Notez que `node_roles` passé au cloud-init (`servers.tf:77`, `:120`) garde
délibérément la virgule (`"app,scheduler,admin"`) — ce n'est PAS un label
Hetzner, juste une variable de template consommée par
`cloud-init/app-node.yaml.tpl` pour son `final_message` humain ; aucune
contrainte de syntaxe Hetzner ne s'y applique, changer son séparateur n'était
ni nécessaire ni fait.

---

## 9. Cibles du Load Balancer — dépendances/race Terraform

**Vérifié** (`load_balancer.tf:38-53`) :
- `hcloud_load_balancer_network.app` a un `depends_on` explicite sur
  `hcloud_network_subnet.app` (le subnet doit exister avant l'attachement
  réseau du LB).
- `hcloud_load_balancer_target.app` a un `depends_on` explicite sur
  `hcloud_load_balancer_network.app` (le LB doit être attaché au réseau privé
  avant qu'on puisse y cibler un server via `use_private_ip = true`).
- `hcloud_load_balancer_target.app` référence `hcloud_server.app[count.index].id`
  et `hcloud_load_balancer.app.id` directement dans ses arguments — dépendance
  **implicite** correcte du graphe Terraform (pas besoin de `depends_on`
  redondant pour ces deux-là).
- `hcloud_server.app` a lui-même un `depends_on` explicite sur
  `hcloud_network_subnet.app`.

**Conclusion** : la chaîne de dépendances est complète et correcte : subnet →
{server privé, LB-network attachment} → LB target. Pas de race possible, pas
de dépendance implicite manquante. **Aucun `depends_on` supplémentaire
nécessaire** — le graphe actuel est déjà minimal et suffisant.

---

## 10. Type de server / localisation — validation

**Valeurs effectives** (`environments/production.tfvars` + défauts
`variables.tf`) : `app_server_type = "cx33"`, `location = "nbg1"` (défaut),
`network_zone = "eu-central"` (défaut), `app_image = "ubuntu-24.04"` (défaut).

**Cohérence vérifiée par lecture du code** :
- `variables.tf:33` documente explicitement que `nbg1` appartient à la zone
  `eu-central` — cohérent avec `network_zone = "eu-central"` (utilisé par
  `hcloud_network_subnet.app`, `network.tf:22`).
- `cx33` est une valeur `string` libre pour `hcloud_server.server_type` (pas
  de validation Terraform dessus, résolue par l'API Hetzner à l'apply) — type
  de la même famille `cx` que le défaut `cx22`, cohérent avec le reste de la
  configuration (pas de mélange de familles ARM/dédié).
- `ubuntu-24.04` est cohérent avec `cloud-init/app-node.yaml.tpl` qui utilise
  des primitives Debian/Ubuntu (`apt-get`, `systemd`, `ufw`) — pas changé.

**Décision** : **aucune modification** apportée à ces valeurs, conformément à
l'instruction explicite ("ne change pas le type si ce n'est pas
nécessaire"). Leur validation finale et définitive (existence réelle du type
`cx33` dans le catalogue Hetzner à cette date, quotas du projet) ne peut être
confirmée qu'au moment du `terraform plan`/`apply` réel contre l'API Hetzner
(hors de portée de cet audit statique) — mais le `terraform plan` déjà
exécuté par l'opérateur (voir contexte de mission) a déjà list ce server sans
erreur de type, ce qui constitue une confirmation empirique suffisante.

---

## 11. État Terraform / secrets

**Vérifié par grep exhaustif** sur `infra/providers/hetzner/**/*.tf`,
`*.tfvars*`, et le cloud-init :
- `hcloud_token` : `sensitive = true` (`variables.tf:16`), **aucune valeur
  par défaut**, jamais présent dans un `.tfvars` du repo — sourcé uniquement
  via `TF_VAR_hcloud_token` (variable d'environnement), conforme à
  l'exigence.
- Aucune trace de mot de passe DB, mot de passe Redis, token Grafana, secret
  WhatsApp, secret Groq, ou secret Sentry/Langfuse dans aucun fichier
  Terraform ou template cloud-init.
- `user_data` (cloud-init) ne contient que : clé SSH **publique**, URL de
  dépôt Git (public), nom de branche/ref, nom d'utilisateur de déploiement,
  CIDR réseau privé — tous non sensibles. Le fichier
  `cloud-init/app-node.yaml.tpl` documente lui-même explicitement (commentaire
  ligne 21-25) que le `.env` applicatif n'est **jamais** écrit ici,
  précisément parce que `user_data` est lisible via l'API metadata Hetzner par
  tout process root sur le node.

**Conclusion** : **aucune modification nécessaire**, l'état actuel est déjà
conforme à l'exigence.

---

## 12. Cloud-init — cohérence avec un déploiement immutable

**Vérifié** (`cloud-init/app-node.yaml.tpl`) :
- Installe Docker Engine + plugin Compose depuis le dépôt officiel Docker
  (pas de version `latest` implicite dangereuse — `docker-ce`,
  `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`,
  `docker-compose-plugin`, versions résolues par `apt` au moment du boot,
  cohérent avec la pratique standard).
- Crée l'utilisateur `deploy_user` (`deploy` par défaut), membre du groupe
  `docker`, clé SSH injectée — permissions correctes, pas de secret en clair
  à part la clé publique (non sensible par nature).
- Clone le dépôt Git — **uniquement** pour bootstrap (checkout requis par
  `scripts/deploy.sh` exécuté **après**, et par `ufw.sh` immédiatement après
  le clone). Le cloud-init lui-même **ne lance jamais** `docker compose up`
  (documenté explicitement dans son en-tête, lignes 21-22) — le runtime
  applicatif reste entièrement piloté par `scripts/deploy.sh` avec des images
  Docker immuables (build-once/run-everywhere, GHCR, jamais de tag
  `latest` — vérifié dans une session d'audit précédente, non re-vérifié en
  détail ici car hors du scope Terraform de cette mission, mais rien dans le
  cloud-init ne contredit cette pratique).
- Applique le firewall hôte via `ufw.sh`, référencé (pas dupliqué) — voir §1
  et §5 pour le contenu.
- Durcit SSH (`PasswordAuthentication no`, `PermitRootLogin no`,
  `X11Forwarding no`) via un fichier `sshd_config.d` dédié.
- Alloy : dossier créé, service **non démarré** (placeholder explicite,
  config réelle hors scope de ce cloud-init).

**Conclusion** : conforme à l'exigence d'un runtime immutable — le cloud-init
reste strictement un bootstrap d'infrastructure, jamais un déploiement
applicatif. Aucune modification nécessaire ici au-delà de l'ajout du
paramètre `private_net_cidr` (§1).

---

## 13. Validation Terraform finale

Exécuté dans `infra/providers/hetzner/` après l'ensemble des correctifs de
cet audit :

```
$ terraform fmt -recursive -diff
(aucune sortie — déjà formaté)

$ terraform validate
Success! The configuration is valid.
```

**`terraform plan -var-file="environments/production.tfvars"`** : **non
ré-exécuté par cet agent** dans cette dernière passe — l'environnement de cet
agent ne dispose pas de `HCLOUD_TOKEN` (uniquement présent dans le terminal
PowerShell propre à l'opérateur). Deux `terraform plan` réels ont déjà été
exécutés par l'opérateur plus tôt dans cette même mission, révélant et
validant la correction de deux bugs réels (format de label, puis indexation
de set au plan) — voir historique de la mission. **Aucun changement de cette
dernière passe (firewall, ufw.sh, `private_net_cidr`, documentation README)
n'affecte la liste des ressources créées ni leurs arguments de fond** : ce
sont soit des retraits de règles `rule {}` à l'intérieur d'une ressource déjà
planifiée (`hcloud_firewall.app`), soit un nouvel argument de `templatefile()`
consommé uniquement par le rendu du `user_data` (qui ne change pas le
**nombre** de ressources), soit de la documentation pure (README). Le compte
de ressources attendu reste donc **11 to add, 0 to change, 0 to destroy**,
identique au dernier plan réel confirmé par l'opérateur — mais ceci est une
**déduction par lecture du code, pas une ré-exécution empirique**.

**Action requise avant l'`apply` réel** : relancer, dans le terminal de
l'opérateur (où `HCLOUD_TOKEN`/`TF_VAR_hcloud_token` est configuré) :

```bash
cd infra/providers/hetzner
terraform plan -var-file="environments/production.tfvars"
```

et confirmer visuellement `Plan: 11 to add, 0 to change, 0 to destroy` avant
de lancer `terraform apply`.

---

## 14. Risques restants (non bloquants)

- Le LB reste public (§2) — accepté comme choix MVP raisonnable, pas
  sur-ingénierié. Amélioration future possible : Cloudflare Authenticated
  Origin Pulls.
- `terraform plan` post-correctifs non ré-exécuté par cet agent (§13) — à
  confirmer par l'opérateur avant l'`apply` (dernière étape avant
  déploiement réel).
- La validité réelle du type `cx33` / quotas du compte Hetzner ne peut être
  garantie à 100% que par l'API Hetzner elle-même au moment du plan/apply —
  déjà indirectement confirmée par le dernier plan réel de l'opérateur (§10).
- Alloy reste en placeholder sur ce cloud-init (non démarré) — hors scope de
  cette mission Terraform, déjà noté comme chantier séparé.

---

## Verdict

**SAFE TO APPLY**, sous réserve de la seule action listée au §13 (confirmer
un `terraform plan` propre — `11 to add, 0 to change, 0 to destroy` — dans le
terminal de l'opérateur avant de lancer `terraform apply`).

Justification : le vecteur de risque réel identifié en début d'audit (bypass
complet de Cloudflare + LB par appel direct à l'IP publique d'un node) est
structurellement fermé (§1), sans effet de bord sur Caddy/ACME/TLS/healthcheck/SSH
(vérifié explicitement pour chacun). Aucune régression introduite sur les
labels/sélecteurs (§8), aucun secret exposé (§11), le graphe de dépendances
Terraform est correct (§9), et `fmt`/`validate` passent proprement (§13).

**Prochaine commande exacte** (à exécuter par l'opérateur, dans son propre
terminal, PAS par cet agent) :

```bash
cd infra/providers/hetzner
terraform plan -var-file="environments/production.tfvars"
```

Si le plan confirme `11 to add, 0 to change, 0 to destroy` :

```bash
terraform apply -var-file="environments/production.tfvars"
```

**Cet agent n'a exécuté aucun `terraform apply` et ne doit pas en exécuter,
conformément à l'instruction de la mission.**
