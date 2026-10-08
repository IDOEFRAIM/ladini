# PRODUCTION CONVERSATION HARDENING

> WhatsApp ne livre pas « exactement une fois ». Les workers meurent, un message arrive deux fois ou en retard, l'état est rechargé.
> **La conversation peut réessayer. L'effet métier, non.**

Objectif réaliste (jamais « exactly-once distribué ») : **livraison au-moins-une-fois + traitement idempotent = un seul effet logique**.

## 1. Chaîne de traitement et ce qui survit

| Étape | Persisté ? | Clé d'idempotence | Version | Survit à un redémarrage | Rejouable | Dupliquable (avant cette phase) |
|---|---|---|---|---|---|---|
| Webhook (Twilio / WhatsApp Cloud) | non | `msg:{id}` Redis `SET NX EX 3600` ; **signature vérifiée AVANT** (Twilio : dépendance du routeur ; Cloud : HMAC sur le corps brut) | — | non (Redis) | oui | oui si Redis indisponible (fail-open) |
| Tâche Celery `process_agent_task` | non | `task_claim:{sid}` (300 s, relâché si échec) + `task_done:{sid}` (24 h) | — | non (Redis) | oui (`autoretry_for`, 3 essais) | oui si Redis indisponible ou **après échec d'ENVOI** (voir R6) |
| Verrou de conversation | non | `conversation_turn_lock:{phone}` Redis, attente bornée | — | non | — | dégradé (fail-open) si Redis indisponible |
| Workspace / checkpointer | **oui** (`agri_workspaces`, JSONB, aller-retour JSON) | — | pas de colonne de version (dernier écrivain gagne, protégé par le verrou) | **oui** | — | — |
| **Résultat du dernier tour** (nouveau) | **oui**, même écriture que l'état | `message_sid` (id fournisseur) | — | **oui** | **oui** | non |
| Menu producteurs / références naturelles | oui (état LangGraph : `vendor_selection_context.created_at`, `menu_facts.created_at`) | péremption par horloge | — | oui | — | — |
| `menu_snapshot_store` | **non** (RAM du process) | — | — | **non** : filet de secours seulement ; son absence ne résout rien (échec fermé) | — | — |
| Panier (`active_cart`, `cart_meta`) | oui (état) | valeur identique -> `UNCHANGED` | `cart_meta.version` + `expected_version` | oui | oui | non |
| Brouillons (précommande, achat groupé, publication vendeur, besoin récurrent) | oui (tables dédiées) | `execution_key` du brouillon | `version` (CAS) + `ConfirmationTarget` | oui | oui | non |
| Appel MCP d'écriture | oui (`marketplace.mcp_idempotency_records`, PK `(clé, outil)`, **sans TTL**) | clé du brouillon, sinon `{outil}:msg:{sid}` | — | oui | oui | non |
| Envoi sortant | non | `ResponsePlan.event_id = message_sid` (`claim_response_item`) | — | non (Redis) | oui | oui si Redis indisponible |

## 2. Couches de dédoublonnage (de l'extérieur vers l'intérieur)

1. **Authenticité** puis **webhook** `msg:{id}` (1 h, fail-open).
2. **Tâche** `task_claim` / `task_done` (fail-open ; le claim est relâché sur toute exception pour ne jamais perdre un message retryable).
3. **Rejeu durable (cette phase)** : `agri_workspaces.metadata.last_turn = {message_sid, final_response, interactive}` est écrit **avec l'état du tour** ;
   `Orchestrator.handle` la relit sous le verrou de conversation et, si le `message_sid` est celui du dernier tour, **rejoue la réponse sans relancer le graphe**.
   La base fait autorité ; Redis n'est qu'un accélérateur.
4. **Domaine** : brouillons versionnés (CAS), `ConfirmationTarget` (version confirmée), registre d'exécution, table d'idempotence MCP (sans TTL).
5. **Envoi** : `claim_response_item` (un item visible par `event_id`).

Fenêtre du rejeu durable : **le dernier tour** de la conversation. Un message plus ancien retombe sur les couches 1-2 puis 4 (le domaine ne ré-exécute rien).
Un échec **technique** (LLM indisponible, `unknown_reason=TECHNICAL_FAILURE`) n'est jamais mémorisé.
Identité d'un message = id fournisseur, jamais le texte : deux messages distincts de même texte sont deux messages ; id absent -> aucune clé inventée.

## 3. Fenêtres de panne

| Fenêtre | Comportement |
|---|---|
| A. avant la mutation | rien n'est appliqué ; la reprise retraite le message |
| B. pendant la mutation | transaction du domaine ; erreur -> échec visible, jamais « c'est noté » |
| C. après COMMIT, avant l'état | registre d'exécution + clé MCP : la reprise reconnaît l'exécution |
| **D. après l'état, avant/pendant l'envoi** | **corrigé** : la reprise rejoue la réponse d'origine (R6) |
| E. après l'envoi, avant `task_done` | la reprise rejoue (couche 3) ; l'envoi est dédoublonné par `event_id` quand Redis répond |

## 4. Redémarrage, état périmé, ordre, concurrence

* **Redémarrage** : panier, version du panier, brouillons, menu et sa date de création, `profile_gate` (commande suspendue) se retrouvent identiques ; vérifié par des tests qui relancent orchestrateur + graphe + Redis sur le même store JSON.
* **Confirmation périmée** : la confirmation vise `(draft_id, version)` ; une édition crée la version N+1 et l'ancienne cible est `STALE_TARGET` (tests existants : précommande, achat groupé, publication vendeur). Un « oui » tardif ne confirme jamais une version plus récente.
* **Menu périmé** : une désignation NATURELLE (« le quatrième », « Gilbert ») sur un menu plus vieux que `MENU_FACTS_TTL_SECONDS` est refusée (clarification), y compris après redémarrage (la date est dans l'état persisté). Un numéro nu reste l'index du menu affiché (choix produit antérieur).
* **Ordre** : aucun numéro de séquence fournisseur fiable n'est exploité -> **pas de réordonnancement**. Politique : un message sans objet en attente (« oui » arrivé avant la demande) ne mute rien ; un message qui arrive hors ordre est évalué contre l'état et la **version** courants, jamais contre une version antérieure.
* **Concurrence** : un tour par conversation (verrou Redis, attente bornée) ; deux éditions concurrentes sont sérialisées, aucune n'est perdue (`cart_meta.version` +2) ; deux confirmations concurrentes -> un seul engagement. Si Redis est indisponible le verrou est dégradé (fail-open) : le domaine (CAS / registre) reste la dernière protection.

## 5. Inventaire des mutations

| Opération | Service | Idempotente ? | Versionnée ? | Transactionnelle ? | Rejeu sûr ? | Risque |
|---|---|---|---|---|---|---|
| Création de précommande | `preorder_draft` + `create_preorder_draft` | oui (`execution_key`, table MCP) | oui (CAS) | oui (brouillon + exécution) | oui | **HAUT** |
| Sélection du gagnant / acceptation d'offre | `bid_award` (`idempotency_key` = empreinte de décision) | oui | décision figée | côté MCP | oui (tests `test_bid_award_decision`) | **HAUT** |
| Mouvement de stock / récolte | outil MCP via exécuteur | **clé `{outil}:msg:{sid}`** : sûr pour le même message ; un 2ᵉ message distinct est protégé par la consommation du pending + verrou | non | côté MCP | oui (même id) ; **résiduel : deux ids distincts « double-tap » sur un delta additif** | **HAUT (résiduel documenté)** |
| Publication vendeur | `sales_publish_draft` | oui (`sales_publish:{id}:{v}`) | oui (CAS) | oui | oui | MOYEN |
| Mutation de besoin récurrent | `recurring_need_draft` + registre | oui | oui (B25/B26) | oui | oui | MOYEN |
| Édition de ligne de panier | `cart_edit` | oui (même valeur -> `UNCHANGED`) | oui (`cart_meta.version`) | état du workspace | oui | MOYEN |
| Mise à jour du profil (région / nom / capacité) | `complete_user_profile` | oui par nature (SET) | non | oui | oui | BAS |
| Résultat de tour (nouveau) | `WorkspaceStore.save` | oui (clé = `message_sid`) | écrasé au tour suivant | une écriture | oui | BAS |

## 6. Observabilité et métriques

Événements : `inbound_duplicate_detected | layer=task_claim|task_done`, `inbound_replay_processed | message_ref=…` (référence = SHA-256 tronqué, jamais l'id fournisseur), plus ceux déjà en place (`business_edit_*`, `STALE_TARGET`/`SALES_PUBLISH_VERSION_CONFLICT`, `MESSAGE_ALREADY_*`, `WHATSAPP_WEBHOOK_DUPLICATE`).
Compteurs : `ladini_duplicate_inbound_messages_total` (existant), **`ladini_inbound_replays_total`** (nouveau), `ladini_transaction_retry_count_total` (existant).
À alerter : un effet métier dupliqué (deux exécutions pour une même clé), un profil persisté mais reprise en échec répétée, une dérive de brouillon à la reprise.

## 7. Stratégie de test

* `tests/integration/test_production_conversation_hardening.py` (vendeur/récurrent + porte de profil) et `…_buyer.py` (menu -> panier -> édition -> précommande) : moteur réel, `FakeRedis` perdable, store JSON, Celery eager avec ses retries, `RecordingDispatcher.fail_next`.
* `tests/schema/test_workspace_turn_replay_pg.py` : rejeu contre un **vrai PostgreSQL** migré (CI ; ignoré localement sans `SCHEMA_TEST_DSN`).
* `tests/unit/test_test_environment_isolation.py` + garde de `tests/conftest.py` : **fail-closed** — toute URL de base/Redis non locale (variable ou `.env`) est remplacée par une adresse fermée ; la suite ne peut pas joindre une infrastructure réelle.
* Preuves rouges : voir le rapport de PR (7 tests passent au rouge sans le rejeu).

## 8. Limites déclarées

* La fenêtre de rejeu durable est le **dernier** tour ; au-delà, la protection repose sur les couches Redis (best-effort) puis le domaine.
* Mouvement de stock additif : deux messages **distincts** (ids différents) confirmant la même chose restent protégés par le verrou et la consommation du pending, pas par une clé métier ; non couvert par un test de bout en bout.
* Sans Redis, verrou de conversation et dédoublonnage Redis sont dégradés (fail-open) ; le rejeu durable et le domaine restent actifs.
* Pas de test Redis réel (le harnais utilise un `FakeRedis` fidèle) ; aucun conteneur disponible localement.
* Ordre fournisseur non exploité : pas de réordonnancement, échec sûr uniquement.
