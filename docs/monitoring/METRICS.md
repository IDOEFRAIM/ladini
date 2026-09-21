# Cockpit `/admin/monitoring` — définition de chaque métrique

Règle : **aucune métrique n'est estimée**. Une métrique dont la définition ne serait pas fiable est déclarée
« non disponible » (section 6) — le cockpit l'affiche comme telle, jamais avec une valeur inventée.

Sources : PostgreSQL uniquement (`intelligence.agent_turns`, `agent_tool_calls`, `agent_llm_calls` + tables métier).
Langfuse n'est qu'un lien (`trace_id`) ; le cockpit fonctionne sans lui.

## 1. Unités et vocabulaire

| Terme | Définition |
|---|---|
| **Tour** | un message utilisateur traité par l'agent (une exécution de `process_agent_task`), écrit après l'envoi de la réponse. Les doublons (`duplicate_skipped`) ne sont pas enregistrés ; un retry Celery = une ligne de plus (`task_retries`). |
| **Session (conversation)** | suite de tours d'un même utilisateur (`phone_hash`) séparés de moins de `AGENT_MONITORING_SESSION_GAP_MINUTES` (30). `conversation_id` est un identifiant de regroupement calculé, sans FK. |
| **Workflow** | un but de l'agent (`current_goal`, catalogue `INTENT_CONFIG` : `SALES_PUBLISH_PRODUCT`, `BUYER_PREORDER_INIT`, …) mené dans une session : le couple (session, but). |
| **Étape** | `pending_interaction.kind` de l'état final du tour (`ENTER_PRICE`, `CONFIRM_ACTION`, …). |
| **Issue d'un tour** | `COMPLETED`, `CLARIFICATION`, `WAITING_USER`, `BLOCKED`, `HUMAN_REQUIRED`, `ERROR` (`FALLBACK` réservé). Dérivée de `security_status`, `requires_human`, `status`, `response_strategy`, `goal_status`. |

## 2. Vue d'ensemble

| Métrique | Formule |
|---|---|
| Messages reçus / envoyés | nombre de tours / tours avec `response_status = SENT` |
| Utilisateurs actifs 24 h / 7 j | `count(distinct phone_hash)` sur les 24 h / 7 j glissants (indépendant de la période choisie) |
| Conversations | `count(distinct conversation_id)` sur la période |
| Nouveaux utilisateurs | `auth.users.created_at` dans la période |
| Intention comprise | `intent` renseigné, ≠ `UNKNOWN*`, et tour non terminé en `CLARIFICATION` |
| Taux de clarification | tours `CLARIFICATION` / tours |
| Taux d'erreur / repli | tours `ERROR` ou `FALLBACK` / tours (le texte de repli est envoyé à l'utilisateur) |
| Confiance moyenne | moyenne de `intent_confidence` (tours renseignés) |
| Appels d'outils réussis / en échec | `SUCCESS`+`REPLAY` / `ERROR`+`DENIED` |
| Workflows démarrés / terminés / abandonnés | voir §4 |
| p50 / p95 / p99 | `percentile_cont` de `duration_ms` (durée du tour, de l'arrivée dans le worker à la fin du traitement) ; comparaison à la période précédente de même durée |
| **Funnel** | étages **imbriqués** : ① tours ② ∧ intention comprise ③ ∧ workflow renseigné ④ ∧ ≥ 1 outil réussi ⑤ ∧ ≥ 1 outil **d'écriture** réussi ⑥ ∧ réponse envoyée. Pour chaque étage : nombre, % du premier, % de l'étage précédent, perte. Un tour d'information (lecture seule) s'arrête donc à l'étage ④ : le funnel mesure le parcours transactionnel, pas la qualité de chaque réponse. |

## 3. Conversations — statut de session (seuils configurables)

Dérivé du **dernier tour** et de son ancienneté (`MONITORING_THRESHOLDS`, défauts entre parenthèses) :
`HUMAN_REQUIRED` / `BLOCKED` / `ERROR` = issue du dernier tour ; `ACTIVE` = dernier tour < `activeMinutes` (5) ;
puis `CLARIFICATION` ; `COMPLETED` (but fermé) ; pour une session en attente de l'utilisateur : `WAITING_USER`,
`STALLED` après `stalledMinutes` (10), `ABANDONED` après `abandonedMinutes` (30).

## 4. Workflows

Par couple (session, but) sur la période : **démarré** = présent ; **terminé** = un tour a fermé le but
(`goal_status = COMPLETED`, règle identique à celle qui ferme le tunnel dans l'orchestrateur) ; **en erreur** =
un tour `ERROR` et non terminé ; **abandonné** = ni terminé ni en erreur, sans activité depuis `abandonedMinutes` ;
**en cours** = le reste. Succès = terminés / démarrés. Durée = première → dernière activité des workflows terminés (p50/p95).
**Étapes atteintes** = nombre de sessions ayant atteint chaque `pending_interaction.kind`, dans leur ordre moyen d'apparition
(les étapes sont celles réellement enregistrées : le catalogue d'étapes n'est pas supposé).

## 5. Outils, performance, santé

* **Outils** : par outil — appels, succès, erreurs, taux, p50/p95/p99, dernier appel, dernière erreur. Catégorie `READ`/`WRITE` d'après
  `TOOL_SCOPE_MAP`. **Aucun argument ni résultat n'est stocké** : le détail d'un outil montre durées, codes d'erreur, sessions concernées et
  trace, jamais les payloads (secrets, données personnelles).
* **Performance** : chaque poste est mesuré indépendamment — file Celery (`publication → début`), Redis (cumul des commandes du tour),
  SQL (cumul des requêtes du tour), LLM interprétation / réponse (par appel, sans double comptage gateway/adaptateur), MCP (cumul des
  outils), WhatsApp/Twilio (durée du dispatch). Les postes **peuvent se chevaucher** (SQL et Redis s'exécutent à l'intérieur des
  outils et des LLM) ; « Autre » = durée − LLM − MCP − WhatsApp. Fenêtres fixes 1 h / 24 h / 7 j / 30 j indépendantes de la période.
* **Santé** (états `HEALTHY` / `DEGRADED` / `CRITICAL` / `UNKNOWN`, seuils dans `MONITORING_THRESHOLDS`) — chaque dépendance indique son
  **origine** : *sonde directe* (ping PostgreSQL du cockpit ; `/health/ready` du backend si `MONITORING_BACKEND_HEALTH_URL` est défini),
  *télémétrie des tours* (MCP, Groq, Bedrock, WhatsApp, latences Redis/SQL), ou *dérivé* (Celery : temps d'attente en file ; Beat :
  retards et échecs de l'outbox). Il n'existe **pas** de sonde directe des workers/Beat : « pas de trafic » n'est pas présenté comme une panne.

## 6. Business — définitions et métriques non disponibles

| Métrique | Définition |
|---|---|
| Offres publiées | produits créés (`marketplace.products.created_at`) dans la période ; volume par unité (les unités ne sont pas additionnées entre elles) |
| Commandes | `status <> 'DRAFT'` créées dans la période ; précommandes = `order_type = 'PREORDER'` |
| **GMV (FCFA)** | Σ `total_amount` des commandes de la période au statut `CONFIRMED`, `DELIVERED` ou `COMPLETED`, devise `XOF` (exclut brouillons, en attente, annulées ; autres devises non additionnées) |
| Commandes / GMV via l'agent | commandes dont `source` ∈ {`WHATSAPP`, `AGENT`} ou `is_agent_order` (marqueurs posés à la création par le backend) ; GMV = même règle que ci-dessus restreinte à ces commandes |
| Taux publication → commande | publications de la période ayant ≥ 1 ligne de commande / publications de la période |
| Paiements réussis / échoués | `captured_at` renseigné / statut commençant par `FAIL` ; répartition par statut affichée telle quelle |
| Enchères terminées | `awarded_at` dans la période |
| Annulations | commandes `CANCELLED` créées dans la période |
| Livraisons | `delivered_at` dans la période |
| Vendeurs / acheteurs actifs | vendeurs distincts ayant publié, misé ou vendu ; acheteurs distincts ayant commandé ou créé une enchère |
| Demandes sans offre | `intelligence.demand_signals` (termes demandés sans offre correspondante) |
| Entonnoir vendeur | utilisateurs distincts avec le workflow `SALES_PUBLISH_PRODUCT` → publications → commandes sur ces publications → dont payées |

**Non disponibles (volontairement, et affichées comme telles)** :

* *Produits « recherchés »* : les recherches réussies ne sont pas journalisées (seules les demandes sans offre le sont).
* *Publications avec intérêt* : aucune mesure de vues ou de paniers par offre.
* *GMV multi-devises*, *conversion exacte publication → paiement* au-delà de ce qui est joignable via `order_items`.

*Valeur générée par l'agent* : mesurée par les commandes portant le marqueur agent (ci-dessus). Les commandes créées côté web ne sont pas comptées ; une commande agent créée par un chemin qui ne poserait pas le marqueur ne serait pas comptée (sous-estimation, jamais surestimation).
