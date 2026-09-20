# WireGuard — tunnel Hetzner ↔ AWS EC2 Valkey (2026-09-20)

## Pourquoi

`redis://:PASSWORD@<IP_PUBLIQUE_AWS>:6379` sur Internet public, même avec un
mot de passe fort, expose Valkey (et donc le broker Celery, l'idempotence,
l'état du LLM Gateway) à toute tentative de connexion/brute-force venant du
monde entier tant que le Security Group AWS reste ouvert sur 6379. Objectif :
Valkey n'est **jamais** joignable publiquement — uniquement via un tunnel
WireGuard chiffré point à point entre les nodes Hetzner et l'EC2 AWS, sur une
IP **privée** du tunnel.

```
Hetzner app node(s)  --[WireGuard UDP 51820, chiffré]-->  AWS EC2 (wg0)  -->  Valkey :6379 (bind sur l'IP wg0)
```

`REDIS_URL` finale : `redis://:PASSWORD@10.200.0.2:6379/0` — jamais l'IP
publique AWS. Pas de `rediss://` nécessaire : le tunnel WireGuard chiffre
déjà tout le trafic, TLS applicatif serait redondant (mais reste supporté
sans régression si un jour Valkey change de topologie, voir
[docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](../../docs/REDIS_VALKEY_MIGRATION_2026-09-20.md)).

## Plan d'adressage — vérifié sans conflit

| Réseau | CIDR | Usage |
|---|---|---|
| Hetzner privé existant | `10.20.0.0/16` (nodes sur `10.20.1.0/24`) | Réseau privé Hetzner Cloud (voir [infra/providers/hetzner/variables.tf](../providers/hetzner/variables.tf)) |
| Docker `agri_net` | allocation Docker par défaut (typiquement `172.x.0.0/16`) | Réseau bridge interne aux conteneurs d'un même node |
| **WireGuard (nouveau)** | **`10.200.0.0/24`** | Tunnel Hetzner ↔ AWS, **disjoint** des deux ci-dessus (`10.200.x.x` ≠ `10.20.x.x`, deuxième octet différent — pas un sous-réseau de `10.20.0.0/16`) |

Adressage à l'intérieur de `10.200.0.0/24` :

| IP | Rôle |
|---|---|
| `10.200.0.2` | AWS EC2 — Valkey (le "hub" WireGuard, un seul peer join possible aujourd'hui, mais topologie en étoile prête pour plusieurs Hetzner nodes) |
| `10.200.0.1` | Premier node Hetzner (`app` — celui qui cumule tous les rôles en Phase 1, voir [infra/inventory.example.yml](../inventory.example.yml)) |
| `10.200.0.3`, `10.200.0.4`, ... | Nodes Hetzner additionnels si scale-out (§ *Ajouter un node* plus bas) — jamais réutiliser `.1` ou `.2` |

Si votre Hetzner Cloud a un jour un CIDR privé qui chevauche `10.200.0.0/24`
(vérifiez `terraform output` / `infra/providers/hetzner/variables.tf` avant
de commencer), changez `10.200.0.0/24` dans les 2 templates ci-dessous et
dans `scripts/check_valkey_network.sh` — un seul endroit par fichier.

## Fichiers

- [aws-wg0.conf.template](aws-wg0.conf.template) — config à installer sur
  l'EC2 AWS (`/etc/wireguard/wg0.conf`). Contient des placeholders
  (`__AWS_PRIVATE_KEY__`, `__HETZNER_PUBLIC_KEY__`, `__HETZNER_PUBLIC_IP__`)
  — **jamais de vraie clé dans ce dépôt**.
- [hetzner-wg0.conf.template](hetzner-wg0.conf.template) — config à
  installer sur chaque node Hetzner (`/etc/wireguard/wg0.conf`). Mêmes
  placeholders, côté Hetzner.
- [../../scripts/wireguard_setup_aws.sh](../../scripts/wireguard_setup_aws.sh) —
  installe WireGuard sur l'EC2, génère SA PROPRE paire de clés localement
  (jamais transmise), affiche la clé PUBLIQUE à copier vers Hetzner.
- [../../scripts/wireguard_setup_hetzner.sh](../../scripts/wireguard_setup_hetzner.sh) —
  même chose côté Hetzner.
- [../../scripts/check_valkey_network.sh](../../scripts/check_valkey_network.sh) —
  validation complète du tunnel + Valkey (wg0 up, handshake récent, ping IP
  tunnel, TCP 6379, `PING` authentifié) — jamais de secret affiché,
  `exit 0` uniquement si tout est vert.

## Installation — ordre exact

**Ne jamais faire ceci en une seule fois pour un lien de production** : générer
les clés, échanger les clés publiques, valider le tunnel, SEULEMENT ENSUITE
router le trafic Valkey dessus, et retirer l'exposition publique 6379
SEULEMENT APRÈS validation complète (voir §5 "Retrait de l'exposition
publique" — ne JAMAIS inverser cet ordre).

### 1. Sur l'EC2 AWS

```bash
sudo bash scripts/wireguard_setup_aws.sh
```

Installe `wireguard`/`wireguard-tools`, génère une paire de clés dans
`/etc/wireguard/` (permissions `600`, jamais exposées), affiche :

```
Clé PUBLIQUE AWS (à coller dans hetzner-wg0.conf.template, section [Peer]) :
<clé publique, PAS un secret — peut être partagée sans risque>
```

**Ne démarre PAS encore l'interface** — attend la clé publique Hetzner (voir
§2) avant de compléter `/etc/wireguard/wg0.conf` (le script s'arrête après
génération de clé et affiche les instructions exactes pour la suite).

### 2. Sur le node Hetzner

```bash
sudo bash scripts/wireguard_setup_hetzner.sh
```

Même mécanique : génère sa propre paire de clés, affiche sa clé PUBLIQUE à
coller côté AWS.

### 3. Échange manuel des clés PUBLIQUES (jamais les clés privées)

- Coller la clé publique Hetzner dans `/etc/wireguard/wg0.conf` sur l'EC2
  AWS, section `[Peer]` (`PublicKey = ...`), avec
  `AllowedIPs = 10.200.0.1/32` (ou l'IP du node concerné) et
  `Endpoint = <IP_PUBLIQUE_HETZNER>:51820`... en réalité Hetzner initie la
  connexion vers AWS (AWS n'a pas forcément besoin de connaître l'IP source
  si elle est dynamique — voir le template pour la config exacte, `Endpoint`
  optionnel côté serveur avec `PersistentKeepalive` côté client).
- Coller la clé publique AWS dans `/etc/wireguard/wg0.conf` sur le node
  Hetzner, section `[Peer]`, avec `Endpoint = <IP_PUBLIQUE_AWS>:51820`.

### 4. Démarrer le tunnel des deux côtés

```bash
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
sudo wg show wg0   # doit montrer un peer avec un "latest handshake" récent
```

### 5. Valider AVANT de toucher au Security Group

```bash
sudo bash scripts/check_valkey_network.sh
```

Doit sortir `0` (tout vert : interface up, handshake récent, ping tunnel OK,
TCP 6379 joignable, `PING` Valkey authentifié réussi) **avant** de passer à
la section Security Group ci-dessous.

## Security Group AWS — ordre exact (ne jamais inverser)

1. **AJOUTER D'ABORD** une règle inbound `UDP 51820`, source =
   `<IP_PUBLIQUE_HETZNER>/32` (jamais `0.0.0.0/0`).
2. Valider le tunnel (§5 ci-dessus) — `check_valkey_network.sh` doit
   retourner `0`.
3. **SEULEMENT ENSUITE**, retirer la règle inbound `TCP 6379` ouverte
   publiquement (celle utilisée pour le test initial `valkey-cli ... ping`
   avant cette migration).
4. Re-valider `check_valkey_network.sh` — doit rester vert (preuve que
   Valkey reste joignable EXCLUSIVEMENT via le tunnel, pas par accident via
   une autre règle oubliée).
5. `sudo ss -lntp | grep 6379` sur l'EC2 doit montrer Valkey en écoute
   uniquement sur `10.200.0.2:6379` (IP WireGuard) et/ou `127.0.0.1:6379`
   (localhost) — **jamais** `0.0.0.0:6379` ni l'IP publique AWS.

**Ne jamais retirer la règle 6379 publique avant l'étape 2.** Si le tunnel
tombe après avoir retiré 6379 public, vous perdez tout accès à Valkey — voir
§16 du runbook de migration pour le rollback réseau exact
([docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](../../docs/REDIS_VALKEY_MIGRATION_2026-09-20.md)).

## Configuration Valkey — bind contrôlé, ORDRE OBLIGATOIRE

**Ne JAMAIS configurer `bind 10.200.0.2 ...` avant que `wg0` soit UP et
porte RÉELLEMENT cette adresse.** Un `bind()` sur une IP qui n'existe
encore sur AUCUNE interface fait échouer le démarrage de Valkey (ou pire :
un restart qui échoue à mi-chemin peut laisser `valkey.conf` dans un état
incohérent). Ordre imposé :

1. Installer + activer `wg0` des deux côtés (§ étapes 1-4 ci-dessus).
2. `ip addr show wg0` — confirmer que `10.200.0.2` apparaît réellement
   dans la sortie, côté AWS.
3. **Seulement ensuite**, configurer `bind` dans `valkey.conf` et
   redémarrer Valkey.
4. `PONG` via `10.200.0.2` en local (sur l'EC2 lui-même).
5. `PONG` depuis Hetzner (à travers le tunnel).
6. **Seulement APRÈS** cette validation complète, retirer l'exposition
   publique 6379 (§ Security Group AWS ci-dessus).

**N'exécutez jamais ces étapes à la main** — utilisez
[scripts/configure_valkey_bind_wireguard.sh](../../scripts/configure_valkey_bind_wireguard.sh) :
il **refuse structurellement** de toucher à `valkey.conf` si `10.200.0.2`
n'apparaît pas encore sur `wg0` (vérifié via `ip addr show`, pas une
supposition), enchaîne backup → écriture → détection du VRAI nom d'unit
systemd (jamais `valkey` en dur — voir
[scripts/detect_valkey_systemd_unit.sh](../../scripts/detect_valkey_systemd_unit.sh),
certaines installations Debian/Ubuntu utilisent `valkey-server.service`) →
restart → `PONG` local → `PONG` via l'IP WireGuard, dans cet ordre exact :

```bash
sudo bash scripts/configure_valkey_bind_wireguard.sh --wg-ip 10.200.0.2
```

Config finale (`/etc/valkey/valkey.conf`, ou équivalent — chemin exact
dépend du paquet installé) :

```
bind 10.200.0.2 127.0.0.1
protected-mode yes
requirepass <NOUVEAU SECRET, jamais l'ancien — voir §3 du runbook de migration>
```

**Jamais** `bind 0.0.0.0` — même avec `requirepass` et le Security Group
fermé, `bind` contrôlé est une deuxième ligne de défense indépendante (si le
Security Group est un jour mal reconfiguré, Valkey n'écoute quand même pas
sur l'interface publique). Vérifier après coup :

```bash
sudo ss -lntp | grep 6379
# attendu : 10.200.0.2:6379 et/ou 127.0.0.1:6379 — JAMAIS 0.0.0.0:6379
```

### maxmemory — ne jamais hardcoder

```bash
bash scripts/compute_valkey_maxmemory.sh              # calcule depuis la RAM réelle (/proc/meminfo), affiche seulement
sudo bash scripts/compute_valkey_maxmemory.sh --apply  # + écrit valkey.conf (backup automatique)
```

Fraction retenue par palier (40 % ≤2 Go, 50 % 2-8 Go, 60 % >8 Go de RAM) —
voir l'en-tête du script pour le raisonnement complet (headroom
Linux/WireGuard/exporter/fragmentation/fork AOF rewrite).

## Ajouter un node Hetzner (scale-out)

1. Générer une nouvelle paire de clés sur le nouveau node
   (`wireguard_setup_hetzner.sh`).
2. Choisir la prochaine IP libre dans `10.200.0.0/24` (`.3`, `.4`, ... —
   jamais `.1`/`.2`).
3. Ajouter un bloc `[Peer]` supplémentaire dans `/etc/wireguard/wg0.conf`
   côté AWS (`AllowedIPs = 10.200.0.X/32`).
4. `sudo wg-quick down wg0 && sudo wg-quick up wg0` côté AWS (ou
   `wg syncconf` pour un rechargement sans coupure — voir commentaire dans
   le script).
5. Valider avec `check_valkey_network.sh` depuis le NOUVEAU node.

## Rollback réseau

Voir [docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](../../docs/REDIS_VALKEY_MIGRATION_2026-09-20.md)
§16 "Rollback réseau" — en résumé : ré-ouvrir temporairement `TCP 6379` sur
le Security Group AWS (source = IP publique Hetzner uniquement, jamais
`0.0.0.0/0`, même en urgence), revenir à l'ancienne `REDIS_URL` si
nécessaire, PUIS diagnostiquer le tunnel à tête reposée.

## Sécurité — rappels

- Les clés privées WireGuard (`PrivateKey =` dans chaque `wg0.conf` réel)
  **ne sont jamais commitées** — seuls les fichiers `.template` avec des
  placeholders vivent dans ce dépôt. Les fichiers réels
  (`/etc/wireguard/wg0.conf` sur chaque machine) restent sur la machine,
  permissions `600`, jamais copiés ailleurs.
- La clé PUBLIQUE (contrairement à la privée) n'est pas un secret — elle
  peut être copiée dans un ticket/Slack/terminal sans risque, mais reste
  hors de ce dépôt par discipline (pas de valeur d'infra réelle en dur, même
  non sensible).
- `check_valkey_network.sh` n'affiche jamais `REDIS_URL` ni le mot de passe
  Valkey — uniquement des statuts (`up`/`down`, latence de handshake, codes
  de retour).
