# Agent Pilot Runbook — Restricted Pilot

Statut : clôture du chantier reliability hardening, 2026-09-28. Compagnon de
`AGENT_PRODUCTION_READINESS.md` (verdict, invariants, statut des issues),
`AGENT_RELIABILITY_MATRIX.md` (détail par intent) et
`AGENT_POST_PILOT_BACKLOG.md` (ce qui reste, et à quelle condition).
Runbooks d'infrastructure génériques : `docs/runbooks/incident.md`.

Ce document répond à une seule question : **quand un signal reliability
s'allume pendant le pilote, que fait-on ?**

## 1. Périmètre du pilote restreint (garde de lancement)

Le pilote restreint est une **garde de lancement**, pas une limitation produit
permanente. Elle existe parce qu'un risque résiduel connu (pas de
dead-letter durable en cas de panne broker soutenue, voir
`AGENT_POST_PILOT_BACKLOG.md` §B1) est couvert par un contrôle
**opérationnel** — des humains qui regardent — et non par du code. Retirer la
garde sans fermer ce risque retirerait aussi son seul contrôle.

Conditions (toutes) :

| # | Condition | Pourquoi |
|---|---|---|
| G1 | Utilisateurs pilotes **connus** (producteurs/acheteurs identifiés, liste tenue hors code) | On peut les joindre pour vérifier « ce message est-il arrivé ? » |
| G2 | Volume **faible** | Un rejeu manuel d'un message perdu reste faisable à la main |
| G3 | Monitoring **actif** (alertes §4 appliquées, logs consultés chaque jour ouvré) | C'est le contrôle compensatoire du risque B1 |
| G4 | **Support humain** joignable pendant les heures d'usage réelles des pilotes | Décision de suspendre / corriger une transaction |
| G5 | **Aucune promesse 24/7 sans supervision** faite aux pilotes | Hors des heures supervisées, un incident peut attendre |
| G6 | Possibilité de **corriger manuellement** une transaction (accès DB/admin par une personne nommée) | Les ~15 goals sans draft n'ont pas de record de réconciliation (matrice, P2 #10) |
| G7 | Politique de **retry du provider vérifiée** avant lancement (voir §7, point L5) | Le 503 sur échec d'enqueue ne récupère le message QUE si le provider redélivre |

Limite de ce périmètre : **aucun mécanisme technique d'allow-list n'a été
vérifié dans cette mission.** G1 est une règle de communication (qui reçoit le
numéro), pas une barrière dans le code. Ne pas la présenter comme telle.

## 2. Comment lire les signaux (ce qui existe réellement)

| Signal | Où | Notes |
|---|---|---|
| `TWILIO_WEBHOOK_ENQUEUE_FAILED`, `WHATSAPP_WEBHOOK_ENQUEUE_FAILED` | logs `api` (ERROR) | contient `message_sid`/`id` à rejouer |
| `TWILIO_WEBHOOK_DUPLICATE`, `WHATSAPP_WEBHOOK_DUPLICATE` | logs `api` | dédup normale ; un pic isolé n'est pas un incident |
| `inbound_queued` | logs `api` (INFO) | corrélation : enqueue réussi pour ce `message_sid` |
| `MESSAGE_ALREADY_COMPLETED`, `RESPONSE_DISPATCH_DUPLICATE_SUPPRESSED` | logs `worker` | double livraison neutralisée (bon signe si isolé) |
| `flow_certified` | logs `worker` (INFO) | goal + `message_sid` ; preuve que la confirmation exécutée est le snapshot gelé |
| `workspace_reconciled` | logs `worker` (INFO) | `goal_before` / `goal_after` après un tour en échec |
| `WORKSPACE_RECONCILIATION_FAILED` | logs `worker` (WARNING) | la relecture du checkpoint a échoué (ajouté à la clôture : c'était du DEBUG) |
| `MCP_EXEC_AUDIT` | logs `worker` | outil MCP exécuté (args masqués) |
| Table `intelligence.agent_turns` | Postgres | une ligne par tour : `message_sid`, `task_retries`, `outcome`, `error_code`, `error_category` (dont `SECURITY`), `phone_last4` — jamais le numéro complet (`phone_hash`) |
| Table `marketplace.mcp_idempotency_records` | Postgres | clés d'idempotence côté serveur MCP |
| Compteurs Prometheus/OTel | `/metrics` (service `api` seulement) | voir la réserve ci-dessous |

**Réserve importante (gap d'infra connu, `infra/alloy/README.md`)** : seul le
service `api` expose `/metrics`. Les compteurs incrémentés côté **worker**
(`workspace_reconciliation_failures`, `transaction_retry_count`) ne sont donc
visibles que si le push OTLP les transporte jusqu'à Grafana — non vérifié ici.
Pour ces deux-là, **le signal de repli fiable est le log** (tableau ci-dessus,
et `agent_turns.task_retries > 0`). Ne pas conclure « tout va bien » sur
l'absence d'une alerte métrique worker.

Recherche de logs (adapter au déploiement, cf. `docs/runbooks/incident.md`) :

```bash
dc(){ docker compose -f docker-compose.prod.yml "$@"; }
dc logs --since=24h api    | grep -E "ENQUEUE_FAILED|WEBHOOK_DUPLICATE"
dc logs --since=24h worker | grep -E "WORKSPACE_RECONCILIATION_FAILED|workspace_reconciled|MESSAGE_ALREADY_COMPLETED"
```

## 3. Conduite à tenir — les 7 cas

Règle générale de suspension : la suspension du bot passe par
`bash scripts/celery_maintenance_mode.sh on` (les webhooks répondent 503 et
**relâchent leur claim**, donc le provider redélivre) — voir §5 pour ses
limites. Elle ne se déclenche jamais « par précaution » sur un signal isolé de
sévérité warning.

### 1. `inbound_enqueue_failures > 0`

- **Signal** : alerte `ladini-inbound-enqueue-failure` (critique, `for: 0s`) ;
  logs `*_WEBHOOK_ENQUEUE_FAILED`.
- **Ce que ça veut dire** : un webhook a échoué à mettre le message en file
  Celery, a répondu **503** et relâché son claim (invariant I6). Le message
  n'est récupéré **que si le provider redélivre**.
- **Vérification** : broker/Redis joignable ? `dc ps redis worker` ;
  `dc exec worker celery -A ladini.api.celery_app inspect ping` ;
  voir aussi `docs/runbooks/incident.md` (Redis, worker down).
- **Action immédiate** : rétablir le broker. Noter tous les `message_sid`/`id`
  des lignes `ENQUEUE_FAILED` de la fenêtre. Après rétablissement, vérifier
  pour chacun qu'une ligne `inbound_queued` (redélivraison) ou un tour
  `agent_turns` existe ; sinon **contacter l'utilisateur** pour qu'il renvoie
  son message (c'est le rejeu manuel du risque B1).
- **À inspecter** : logs `api` de la fenêtre, `agent_turns` par `message_sid`.
- **Suspendre le bot ?** Oui si l'échec **persiste** (broker toujours KO après
  la première vérification) : mieux vaut un 503 propre (maintenance) qu'un
  flux de messages qui dépassera la fenêtre de redélivrance du provider. Non si
  c'est un échec isolé déjà résorbé et tous les `message_sid` retrouvés.

### 2. `workspace_reconciliation_failures > 0`

- **Signal** : alerte `ladini-workspace-reconciliation-failure` (warning —
  seulement si les métriques worker arrivent) ; log
  `WORKSPACE_RECONCILIATION_FAILED` (fiable).
- **Ce que ça veut dire** : après un tour en erreur, la relecture du
  checkpoint a échoué ; `ws.active_goal` garde sa valeur **pré-tour**, qui peut
  être périmée (risque résiduel de l'invariant I5 sur ce seul tour).
- **Vérification** : erreur de checkpointer (Redis ? sérialisation ?) dans la
  trace jointe au log. Vérifier le tour suivant de l'utilisateur concerné :
  a-t-il reçu une question/confirmation qui ne correspond pas à ce qu'il
  vient de demander ?
- **Action immédiate** : si un seul cas et le tour suivant est cohérent, noter
  et investiguer sans urgence. Si le tour suivant est incohérent, traiter
  comme le cas 6 (mauvais state).
- **À inspecter** : log + `agent_turns` (`outcome`, `error_code`) des 2 tours
  autour de l'événement pour ce `phone_last4`.
- **Suspendre le bot ?** Non sur un cas isolé. Oui si plusieurs utilisateurs
  différents sont touchés dans la même heure (cause commune : checkpointer).

### 3. Pic de `transaction_retry_count`

- **Signal** : alerte `ladini-transaction-retry-spike` (warning, seuil
  **initial sans donnée de prod**, à recalibrer) ; en repli
  `SELECT count(*) FROM intelligence.agent_turns WHERE task_retries > 0 AND created_at > now() - interval '1 hour';`
- **Ce que ça veut dire** : des tâches agent sont rejouées (timeout MCP/LLM,
  worker instable). Le rejeu ne doit **pas** créer de double écriture
  (invariant I3) — c'est ce qu'il faut confirmer.
- **Vérification** : pour les `message_sid` avec `task_retries > 0`, une seule
  écriture métier existe-t-elle ? (cas 5). Cause : `dc logs worker`, latence
  MCP/LLM, `docs/runbooks/incident.md` (worker runaway, LLM gateway).
- **Action immédiate** : traiter la cause (worker, provider LLM). Aucune action
  sur les données si le contrôle du cas 5 est propre.
- **À inspecter** : `agent_turns` (`task_retries`, `error_category`),
  `marketplace.mcp_idempotency_records`, logs `MCP_EXEC_AUDIT`.
- **Suspendre le bot ?** Non tant que le contrôle du cas 5 montre zéro double
  écriture. Oui dès qu'**une** double écriture est confirmée.

### 4. Un message utilisateur semble perdu

- **Signal** : l'utilisateur dit « je n'ai pas eu de réponse » (aucune alerte
  ne le détecte directement — c'est exactement le risque B1).
- **Vérification (dans cet ordre)** :
  1. Y a-t-il un log `inbound_queued` pour ce message ? Non → chercher
     `*_ENQUEUE_FAILED` / `*_WEBHOOK_DUPLICATE` autour de l'heure : le message
     n'a jamais atteint Celery (cas 1).
  2. `inbound_queued` oui mais pas de ligne `agent_turns` → tâche perdue ou
     encore en file (`docs/runbooks/incident.md`, worker down).
  3. Ligne `agent_turns` avec `response_status = 'FAILED'` → le tour a tourné
     mais l'envoi WhatsApp a échoué (provider).
  4. `response_status = 'DUPLICATE'` → réponse déjà envoyée (dédup), l'utilisateur
     l'a peut-être manquée.
- **Action immédiate** : demander à l'utilisateur de renvoyer son message ;
  jamais rejouer à la main une action d'écriture sans avoir vérifié qu'elle
  n'a pas déjà eu lieu (cas 5).
- **À inspecter** : `agent_turns` par `phone_last4` + fenêtre horaire, logs
  `api`/`worker`.
- **Suspendre le bot ?** Non pour un cas unique dont la cause est identifiée.
  Oui si la cause reste inconnue ET qu'un second utilisateur signale la même
  chose.

### 5. Double action suspectée

- **Signal** : un utilisateur signale deux publications / deux commandes /
  deux offres identiques ; ou le contrôle du cas 3.
- **Vérification** : (a) retrouver les tours : `agent_turns` par
  `phone_last4` — deux `message_sid` **distincts** ou le même ? (b) même
  `message_sid` avec `task_retries > 0` → l'invariant I3 est violé (grave) ;
  (c) `message_sid` distincts → deux messages WhatsApp réellement envoyés par
  l'utilisateur (« double OK »), cas **accepté** pour les goals génériques sans
  draft sous verrou dégradé (voir readiness, IDEMPOTENCE `PARTIAL`) ;
  (d) `marketplace.mcp_idempotency_records` : deux enregistrements pour la même
  clé ?
- **Action immédiate** : annuler/corriger la donnée en double (G6), prévenir
  l'utilisateur. Ne pas supprimer sans identifier laquelle est la bonne.
- **À inspecter** : `agent_turns`, `mcp_idempotency_records`, `MCP_EXEC_AUDIT`,
  `flow_certified` pour le `message_sid` concerné.
- **Suspendre le bot ?** **Oui immédiatement** si (b) est confirmé (même
  `message_sid`, deux écritures) : c'est une violation de I3 qui peut se
  reproduire sur chaque retry. Non pour (c) — c'est un doublon utilisateur,
  à corriger à la main et à noter dans le backlog (`AGENT_POST_PILOT_BACKLOG.md`).

### 6. Mauvais state détecté

- **Signal** : l'agent affiche une quantité/un prix/une unité/un produit que
  l'utilisateur n'a jamais dit (le motif 461 000 / `UNITE`), redemande ce qui
  vient d'être dit, ou pose une question d'un flow abandonné.
- **Vérification** : quel goal et quel `message_sid` ? Le tour précédent
  s'était-il terminé en erreur (`outcome = 'ERROR'`, log
  `workspace_reconciled` / `WORKSPACE_RECONCILIATION_FAILED`) ?
- **Action immédiate** : **ne jamais confirmer** le récapitulatif erroné.
  L'utilisateur peut dire « annuler » ; si le state reste corrompu, purger la
  conversation via le mécanisme de reset existant
  (`core/conversation_reset.py`, ou l'action admin équivalente). Conserver
  le contenu du tour (`agent_turns.user_message_excerpt` /
  `agent_response_excerpt`) pour un test de régression.
- **À inspecter** : `agent_turns` des 3 derniers tours, logs `flow_certified`
  (a-t-on exécuté le payload gelé ?).
- **Suspendre le bot ?** Non pour un cas isolé **sans exécution** (rien n'a été
  écrit). Oui si un mauvais state a conduit à une **écriture** (invariant I2
  violé : ce que l'utilisateur a confirmé ≠ ce qui a été exécuté).

### 7. Anomalie d'autorisation détectée

- **Signal** : `agent_turns.error_category = 'SECURITY'` inexpliqué ; erreur
  `not_owner` / `PermissionDenied` dans les logs pour un identifiant que
  l'utilisateur ne devrait pas connaître ; ou pire, une écriture réussie sur
  une entité qui n'appartient pas à l'appelant (I4).
- **Vérification** : qui (`phone_last4`, rôle), quel outil, quelle entité ?
  Le rejet `not_owner` est le **système qui fonctionne** ; l'anomalie est une
  écriture **acceptée** sur la mauvaise entité.
- **Action immédiate** : rejets isolés → noter, pas d'action. Écriture acceptée
  sur une entité tierce → annuler la modification, geler le compte concerné si
  nécessaire, conserver les logs.
- **À inspecter** : `MCP_EXEC_AUDIT`, `agent_turns`, propriétaire réel de
  l'entité en base.
- **Suspendre le bot ?** **Oui immédiatement** dès qu'une écriture cross-acteur
  est confirmée. Toute violation d'autorisation confirmée remet la
  sortie du pilote à zéro (critère d'échec du §6 de
  `AGENT_POST_PILOT_BACKLOG.md`).

## 4. Alertes minimales

Définies dans `infra/grafana/alerts/alerts.yaml` (groupe
`ladini-agent-reliability`, uid stables, idempotent). **Statut d'application :
fichier valide, non appliqué depuis cet environnement** — l'application exige
les identifiants Grafana Cloud (voir `infra/grafana/provision.sh` : il ne fait
qu'une validation syntaxique sans identifiants). Voir la checklist L4.

| Condition | Règle | Sévérité | Portée réelle |
|---|---|---|---|
| `inbound_enqueue_failures > 0` | `ladini-inbound-enqueue-failure` | critique, attention immédiate | métrique côté `api` → scrapée |
| `workspace_reconciliation_failures > 0` | `ladini-workspace-reconciliation-failure` | warning, investigation | métrique côté worker : **effet conditionnel** ; repli = log `WORKSPACE_RECONCILIATION_FAILED` |
| pic de `transaction_retry_count` | `ladini-transaction-retry-spike` (seuil initial : > 5 sur 15 min, **sans baseline**) | warning, investigation | métrique côté worker : **effet conditionnel** ; repli = `agent_turns.task_retries` |
| plusieurs erreurs transactionnelles, même utilisateur | **pas d'alerte automatique** | — | revue manuelle quotidienne (requête ci-dessous) |

Le dernier cas n'est volontairement pas automatisé : `phone`/`user_id` ne
doivent jamais devenir un label de métrique (cardinalité, PII — discipline de
`core/telemetry.py`). Revue quotidienne pendant le pilote :

```sql
-- utilisateurs avec ≥ 2 tours en erreur sur 24 h (phone_last4 = repère, pas un identifiant unique)
SELECT phone_hash, max(phone_last4) AS last4, count(*) AS errors,
       array_agg(DISTINCT error_code) AS codes
FROM intelligence.agent_turns
WHERE created_at > now() - interval '24 hours' AND outcome = 'ERROR'
GROUP BY phone_hash HAVING count(*) >= 2
ORDER BY errors DESC;
```

Les seuils marqués « initial » sont des points de départ **sans donnée**. La
première semaine de pilote sert aussi à établir la baseline réelle et à les
recalibrer ; ne pas les considérer comme validés avant cela.

## 5. Suspendre et reprendre le bot

```bash
bash scripts/celery_maintenance_mode.sh status
bash scripts/celery_maintenance_mode.sh on     # webhooks -> 503, claim relâché, provider redélivre
bash scripts/celery_maintenance_mode.sh off
```

Limites à connaître avant de l'utiliser :

- Le mécanisme fait répondre 503 aux webhooks ; **il ne met pas les messages
  en attente chez nous**. Ils sont récupérés uniquement par la redélivrance du
  provider, dans sa fenêtre de retry. Une suspension **longue** peut donc
  perdre des messages : c'est exactement le risque B1. Prévenir les pilotes
  d'une suspension prévue plutôt que de laisser le provider réessayer dans le
  vide.
- Il agit sur le conteneur `api` local (un seul node aujourd'hui).
- Après `off`, vérifier que les messages arrivés pendant la pause ont bien
  produit des lignes `inbound_queued` puis `agent_turns`.

## 6. Scénario de validation manuel après déploiement (WhatsApp réel)

Non exécutable dans l'environnement de développement de la clôture (pas de
canal WhatsApp ni de compte pilote ici). **À exécuter une fois après
merge/déploiement**, depuis un numéro de test producteur avec une ferme, et à
consigner (captures + heure) dans le dossier de lancement. Le comportement
attendu est celui que `tests/integration/
test_sales_publish_cross_flow_state_leak.py::
test_pilot_acceptance_scenario_boeufs_asks_next_question_without_premature_confirmation`
verrouille avec un LLM scripté ; ce test manuel confirme la même chose avec le
vrai canal et le vrai LLM.

**Test A — état vierge**

1. Envoyer : `je veux vendre mes boeufs`
2. Attendu : l'agent pose une question de suite (le flow peut d'abord demander
   « maintenant ou plus tard », ou directement la quantité — répondre pour
   avancer dans les deux cas). À ce stade il ne doit apparaître **aucun**
   récapitulatif de confirmation, **aucun** chiffre que vous n'avez pas donné,
   et l'unité mentionnée doit être « tête(s) », pas « UNITE ».
3. Donner la quantité (`3`), puis le prix (`450000`). Attendu : récapitulatif
   « 3 têtes de boeufs à 450 000 FCFA » (ou équivalent) — jamais 461 000.
4. Répondre `oui`. Attendu : une seule publication.
5. Renvoyer immédiatement `ok` (double confirmation). Attendu : **pas** de
   seconde publication ; vérifier en base/admin qu'il n'existe qu'un produit.

**Test B — rejeu du motif d'incident (état périmé)**

1. Démarrer une vente en donnant quantité et prix sans produit
   (ex. `je veux vendre 461000 à 461000`), puis abandonner sans donner de
   produit (changer de sujet ou attendre).
2. Envoyer : `je veux vendre mes boeufs`
3. Attendu : produit = boeufs, unité = tête, quantité et prix **redemandés**.
   Si 461 000 réapparaît, c'est une régression P0 : suspendre (cas 6) et ne pas
   confirmer.

**Test C — échec d'enqueue et redélivrance (staging, ou fenêtre calme avec
l'équipe prévenue)**

1. `bash scripts/celery_maintenance_mode.sh on`
2. Envoyer un message depuis le numéro de test. Attendu : pas de réponse ;
   log `api` de la branche maintenance (503, claim relâché).
3. `bash scripts/celery_maintenance_mode.sh off`
4. Attendre la redélivrance du provider. Attendu : **exactement une** réponse
   et **exactement une** ligne `agent_turns` pour ce `message_sid`. Si le
   provider ne redélivre pas dans un délai raisonnable, c'est l'information la
   plus importante de ce test : reporter dans L5.

## 7. Checklist de lancement du pilote

| # | Vérification | Preuve attendue |
|---|---|---|
| L1 | Ce PR mergé, image de release construite et déployée par le pipeline habituel (`docs/runbooks/deployment.md`) | tag de release / manifeste |
| L2 | Test manuel A et B du §6 passés | captures + heure |
| L3 | Test manuel C du §6 passé (ou explicitement reporté avec justification) | captures + logs |
| L4 | Règles `ladini-agent-reliability` **appliquées** sur Grafana et vérifiées (au moins un déclenchement de test de la règle critique) | capture de l'état de la règle |
| L5 | **Politique de retry du provider vérifiée** dans sa console/doc actuelle (Meta WhatsApp Cloud en production : `MESSAGING_PROVIDER` par défaut ; Twilio si utilisé — ne pas supposer qu'un 5xx est réessayé par défaut) | note datée dans le dossier de lancement |
| L6 | `/metrics` non exposé publiquement (le Caddyfile le laisse en 404 via le handler générique — revérifier après déploiement) | `curl` externe → 404 |
| L7 | Personne nommée pour G4 (support) et G6 (correction manuelle), avec accès | noms dans le dossier |
| L8 | Liste des utilisateurs pilotes (G1) et consigne « pas de promesse 24/7 » (G5) communiquées | message envoyé |
| L9 | Première revue quotidienne des logs/`agent_turns` planifiée (qui, quand) | agenda |
| L10 | Ce runbook lu par les personnes de L7 | accusé |

Pas de lancement tant que L1, L2, L4, L5, L7 ne sont pas cochées. L3 peut être
reporté si le staging n'existe pas, **avec** une décision écrite.
