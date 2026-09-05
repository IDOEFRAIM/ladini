# BACKEND ARCHITECTURE AUDIT — 2026-09-05

```
BACKEND ARCHITECTURE STATUS

Architecture health:     YELLOW
Security:                YELLOW
Domain boundaries:       YELLOW
State ownership:         GREEN
Concurrency:             GREEN
Idempotency:             GREEN
Runtime reachability:    GREEN
Observability:           YELLOW
Scalability:             YELLOW
Technical debt:          MEDIUM
```

**Conclusion générale : l'architecture est saine sur ce qui compte le plus
(propriété des états, concurrence, idempotence, atteignabilité), et faible
sur un seul axe structurel réel : _la surface d'exposition MCP est en
opt-out plutôt qu'en opt-in_.** C'est ce seul choix qui a produit les
quatre failles de sécurité trouvées en Phases 6B et 7 — aucune n'était un
bug isolé, toutes étaient le même défaut structurel.

Le reste des « défauts » observés (cycles entre couches, dépendances
inversées) est du **couplage documenté et maîtrisé**, pas une dette
dangereuse. Voir §38 : ne pas confondre laideur et risque.

## TOP 10 ARCHITECTURAL RISKS

| # | Risque | Sév. | Preuve | Mitigation actuelle |
|---|---|---|---|---|
| 1 | **Exposition MCP par introspection (opt-out)** : toute méthode async publique de `AgriDatabaseService` devient un outil appelable ; seule la carte des scopes l'arrête | **P1** | 113 outils exposés, dont **82 sans aucun goal** ; les 4 failles (update_order_status, mark_escrow_paid, expire_pending_payments, target_status libre) viennent toutes de là | fail-closed + 41 outils sans scope + contrats de test |
| 2 | **Règles métier de pricing/stock hébergées dans le paquet agent** : `services/database/*` importe `graphs/.../domain/pricing_tiers.py` | **P2** | 4 imports `services → graphs.domain.pricing_tiers`, + `order_policy`, + les 3 modules Draft | fonctionne ; direction de dépendance inversée assumée |
| 3 | **Invariant « une commande = un producteur » garanti par le code seul** | **P2** | aucune colonne producteur sur `Order` ⇒ aucune contrainte DB possible | contrat de test + liste blanche des sites créant des `OrderItem` |
| 4 | **`services ↔ workers` et `graphs ↔ services` cycliques**, cassés par imports paresseux | **P2** | 14 imports paresseux `services → workers` (Outbox), 17 `workers → services` | volontaire (Outbox transactionnel) et documenté |
| 5 | **Autorisation portée par convention de méthode**, pas par une frontière unique | **P2** | chaque méthode DB refait `get_*_profile` + filtre ; aucun décorateur/garde central | 5 mutations vérifiées par contrat (`test_order_mutations_require_ownership`) |
| 6 | **Historique/état conversationnel volumineux en JSON** (checkpointer LangGraph) | **P2** | pruning au flush, caps par entrée déjà en place | mécanismes de compaction existants |
| 7 | **Outbox sans dead-letter métier** : statut `DEAD` posé, aucun traitement en aval | **P3** | `outbox_repo` écrit `DEAD` ; aucun consommateur | visibilité via logs/Flower |
| 8 | **Pas de télémétrie structurée sur le domaine Enchère** | **P3** | procurement/preorder/MCP instrumentés, enchère non | connu et accepté |
| 9 | **Pas d'expiration automatique des enchères** (`deadline` déclaratif) | **P3** | aucun cron d'expiration | sorties explicites (sélection/annulation) existent |
| 10 | **Code mort formant une architecture alternative** (`OrderService`, `DeliveryMixin`, `finalize_multi_order`, `product_service`) | **P3** | 0 appelant ; `finalize_multi_order` recréerait le P1 multi-producteurs s'il était câblé | contrat de test qui échoue si câblé |

---

## 1. Current architecture map

Mesurée sur le code (≈ 73 000 lignes, 271 fichiers) :

| Couche | Fichiers | Lignes | Rôle réel |
|---|---:|---:|---|
| `api` | 14 | 2 090 | webhooks (Twilio/WhatsApp/Paydunya), `/health`, `/metrics`, market, admin |
| `graphs` | 73 | **27 613** | runtime conversationnel : interpréteur, routeur, tunnels, nœuds, rendu |
| `actions` | 18 | 2 491 | handlers `prep_*` : goal → (tool_name, args) |
| `domain_agent` | 15 | 5 008 | règles métier côté agent (Drafts, pricing_tiers, order_policy, selection) |
| `services_agent` | 16 | 3 781 | gateways MCP, services de domaine conversationnels |
| `domain` | 17 | 2 685 | modèles SQLAlchemy |
| `services` | 48 | **16 070** | **autorité métier + persistance** (mixins DB) |
| `workers` | 30 | 2 579 | Celery, crons, Outbox |
| `infrastructure` | 8 | 3 243 | runtime MCP, sécurité/scopes |
| `protocols` | 11 | 1 859 | serveurs MCP (stdio/http), introspection des outils |
| `core` | 13 | 3 282 | settings, DB, télémétrie, géofencing |
| `agents` | 8 | 2 337 | onboarding, reducers |

**Deux centres de gravité** : `graphs` (38 %) et `services` (22 %). C'est
cohérent avec le produit : un agent conversationnel adossé à une couche
transactionnelle. Les couches intermédiaires (`actions`, `domain_agent`,
`services_agent`) restent fines — bon signe : ce ne sont pas des couches
de cérémonie.

## 2. Domain boundaries

| Domaine | Règles métier | Transitions d'état | Persistance | Autorisation | Idempotence | Notifications | Rendu |
|---|---|---|---|---|---|---|---|
| Order / Checkout | `services/database/buyer.py` | idem | idem | idem (profil + filtre) | garde de statut | Outbox (même txn) | `graphs/nodes/rendering` |
| Fulfillment | `services/database/producer.py` | idem | idem | propriété dual-origine | `ALREADY_COMPLETED` | Outbox | idem |
| Auction / Bid | `services/database/auction.py` | idem | idem | idem | garde de statut + index unique | Outbox | idem |
| Product | `services/database/product.py`, `producer.py` | idem | idem | `producer_id` | — | Outbox (alerte) | idem |
| Drafts (3) | `domain_agent/*_draft.py` (pur) | **CAS** dans `services/database/*_draft_store.py` | idem | `buyer_id` | version CAS | — | `flows/` |
| Notification | — | `workers/repositories/outbox_repo.py` | idem | — | `dedupe_key` unique | dispatcher cron | `workers/outbox/templates.py` |
| MCP | — | — | `mcp_idempotency_store` | `infrastructure/mcp/security.py` | PK composite | — | — |
| LLM | `llm_gateway` | — | — | — | — | — | — |
| GPS | `core/geofencing.py`, `core/location.py` | — | `auth.update_geo_location` | épinglage d'identité | — | — | `flows/buyer/gps_delivery_gate.py` |

**Aucune règle métier dupliquée détectée** entre couches (voir §5).

## 3. Source-of-truth map

| Concept | Source autoritative | Projections dérivées | Autres écrivains |
|---|---|---|---|
| Order status | `Order.status` (PostgreSQL) | dashboards, récaps conversationnels | **aucun hors `services/`** ✅ |
| Payment status | `Order.payment_status` | idem | aucun ✅ |
| Delivery status | `Order.delivery_status` | idem | aucun ✅ |
| Bid status | `Bid.status` | menus, notifications | aucun ✅ |
| Auction status | `Auction.status` | idem | aucun ✅ |
| Product availability | `Product.is_available` | recherche acheteur | `delete_product`, `record_sale` (fantômes) |
| Product quantity | **`Product.quantity_for_sale`** | « stock insuffisant » | checkout/annulation/escrow — jamais la table `Stock` |
| Draft version | colonne `version` (CAS SQL) | état LangGraph | store uniquement ✅ |
| PendingInteraction | `pending_interaction` dans l'état LangGraph (DURABLE) | `expected_input` legacy (dérivé) | un seul module d'écriture ✅ |
| ConfirmationTarget | `confirmation_target` (état) | récap affiché | `confirmation_gate` |
| Checkout group | `Order.checkout_group_id` | regroupement d'affichage | `create_preorder_draft` seul ✅ |

**Vérification décisive** : recherche exhaustive des écritures brutes
`*.status = ` hors de `services/` → **aucune sur une entité métier**. Les
seules occurrences sont des défauts de colonne, un objet de trace, et le
cycle de vie propre de l'Outbox (`SENDING/SENT/DEAD/PENDING`), qui est sa
propre autorité légitime.

→ **Pas de writer concurrent non coordonné. State ownership = GREEN.**

## 4. State ownership

| Machine | Définition | Autorité de transition | Récupération | Classement |
|---|---|---|---|---|
| `Order` | constantes de chaîne | `buyer.py` / `producer.py` / `auction.py` | garde de statut + verrou | **SAFE** |
| `Bid` | idem | `auction.py` | index unique + verrou | **SAFE** |
| `Auction` | idem | `auction.py` | verrou `of=Auction` | **SAFE** |
| `ProcurementDraft` | Enum | store CAS | service de réconciliation | **SAFE** |
| `PreorderDraft` | Enum | store CAS | service de réconciliation + `EXECUTION_UNKNOWN` | **SAFE** |
| `SalesPublishDraft` | Enum | store CAS | cron de réconciliation | **SAFE** |
| `PendingInteraction` | Enum | `core/pending_interaction.py` (point d'écriture unique) | checkpointer DURABLE | **SAFE** |
| Outbox row | constantes | `outbox_repo` | retry, puis `DEAD` | **CONVENTION-BASED** (pas de traitement du terminal `DEAD`) |

Les statuts métier sont des **chaînes**, pas des Enum — convention, mais
sans écrivain dispersé, le risque réel est faible. Ce serait un
changement cosmétique à coût non nul : **ne pas le faire**.

## 5. Business logic leaks

Recherche ciblée de conditions métier (`if order.status ==`,
`payment_status ==`, `bid.status ==`) dans `graphs/`, `api/`, `workers/` :
**zéro occurrence**.

Les couches d'orchestration orchestrent ; elles ne re-décident pas. C'est
un résultat fort et non évident pour un runtime conversationnel de cette
taille.

## 6. Authorization architecture

**Frontière canonique** = la méthode de service DB elle-même :

```
message utilisateur
  → identité ÉPINGLÉE (schema_resolver::lookup_arg_value : phone/producer_id/user_id
    lus depuis l'état authentifié, JAMAIS depuis le payload)
  → gateway
  → méthode de service : get_*_profile(phone) puis filtre par l'acteur résolu
  → mutation
```

Points faibles réels :

1. **La frontière est une convention de méthode**, pas un garde central : chaque nouvelle méthode doit refaire le contrôle. Rien ne l'oblige structurellement → c'est exactement ce qui a manqué à `update_order_status`.
2. **L'exposition MCP est opt-out** (risque n° 1) : une méthode sans contrôle devient appelable par défaut si quelqu'un lui déclare un scope.

Contrôle compensatoire en place : `test_order_mutations_require_ownership.py`
(propriété prouvée sur les 5 mutations d'état, outils système fail-closed,
statut arbitraire interdit).

## 7. Idempotency architecture

| Opération | Frontière de retry | Mécanisme | Stockage | Portée |
|---|---|---|---|---|
| Écriture MCP | appel d'outil | clé d'idempotence | `mcp_idempotency_records` (**PK composite**) | par (clé, outil) |
| Confirmation preorder | tour conversationnel | CAS version + garde `DRAFT` | `preorder_drafts` | par draft |
| Création procurement | idem | CAS version | `procurement_drafts` | par draft |
| Publication produit | idem | CAS version | `sales_publish_drafts` | par draft |
| Clôture livraison | commande producteur | garde de statut → `ALREADY_COMPLETED` | `orders` | par commande |
| Annulation (2 sens) | idem | garde de statut → `ALREADY_CANCELLED` | `orders` | par commande |
| Sélection gagnant | idem | garde `auction.status`/`bid.status` + verrou | `auctions`/`bids` | par enchère |
| Notification | dispatch Outbox | `dedupe_key` **UNIQUE + ON CONFLICT DO NOTHING** | `outbox` | par (événement, destinataire) |
| Offre producteur | dépôt d'offre | **index unique** `(auction_id, producer_id)` | `bids` | par couple |

**Ce sont quatre mécanismes distincts, pas un framework** — et c'est
justifié : ils protègent des choses différentes (rejeu d'outil, conflit
d'écriture concurrent, rejeu métier, doublon de message). Trois des quatre
sont **garantis par la base**. Aucun trou détecté sur les mutations
critiques. **Ne pas unifier.**

## 8. Transaction boundaries

`AgriDatabaseService.__getattribute__` enveloppe toute méthode non
read-only dans `@transactional(write=True)` : **frontière unique,
générique, non contournable par oubli**. C'est la meilleure décision
structurelle du dépôt.

| Propriété | État |
|---|---|
| Outbox enfilé dans la MÊME transaction que la mutation | ✅ vérifié |
| Envoi WhatsApp direct depuis la couche DB | ❌ **aucun** (vérifié) |
| Appel externe (LLM/HTTP) à l'intérieur d'une transaction DB | non détecté sur les chemins critiques |
| Verrous pris avant écriture | ✅ sur toutes les mutations d'état |
| Ordre de verrouillage déterministe | ✅ tri par `product_id` (et par `id` pour le groupe de checkout) |

Garanties : **fortes** pour Order/Bid/Auction/Drafts. **Best-effort
assumé** pour le DDL de démarrage et les notifications de confort.
**Ambigu et explicitement modélisé** pour `EXECUTION_UNKNOWN` (escrow/IPN)
— une opération ambiguë n'est jamais promue en succès.

## 9. Concurrency

| Opération | Garde | Mécanisme | Race possible |
|---|---|---|---|
| `confirm_preorder_draft` | verrou | `FOR UPDATE` order(s) + produits, ordre déterministe | non |
| `confirm_delivery_and_payment` | verrou + statut | `FOR UPDATE` | non |
| `cancel_confirmed_order` | verrou + statut | `FOR UPDATE` (order + produits) | non |
| `cancel_pending_order` | verrou + statut | `FOR UPDATE` | non |
| `select_winning_bid` | verrou + statuts | `FOR UPDATE` + UPDATE en masse `.returning()` | non |
| `place_bid` | **contrainte DB** | index unique `(auction_id, producer_id)` | non |
| `cancel_auction` / `withdraw_bid` | verrou + statut | `FOR UPDATE` | non |
| Drafts | **CAS** | `WHERE version = :expected` | non |
| Outbox | **contrainte DB** | `dedupe_key` unique | non |
| Idempotence MCP | **contrainte DB** | PK composite | non |

Couverture des verrous : `escrow.py` 6/6, `product.py` 4/5, `buyer.py`
13/22, `auction.py` 10/21 (les méthodes restantes sont des lectures).

→ **Concurrency = GREEN.** C'est le résultat direct des chantiers
transactionnels précédents ; il n'y a plus rien à y investir.

## 10. Ownership / identity model

Modèle **double-rôle** : une identité (téléphone → `User`), deux profils
(`BuyerProfile`, `Producer`), **rôle déduit de l'action**, jamais d'un
rôle de session. `INTENT_ROLE` ne sert qu'à composer le catalogue vu par
le LLM, jamais de garde post-classification.

L'identité est **épinglée** au point d'entrée MCP
(`schema_resolver::lookup_arg_value`) : un numéro présent dans le texte
libre ne peut pas faire agir l'agent au nom d'autrui. Les méthodes DB
re-résolvent ensuite le profil depuis ce téléphone épinglé.

Chemins ne passant pas par l'épinglage : les tâches système (crons, IPN)
qui appellent `AgriDatabaseService()` en direct — **par nature sans
acteur**, et c'est précisément pourquoi leur exposition MCP a été retirée
(Phase 7).

## 11. MCP architecture

```
Outil MCP (nom) → TOOL_SCOPE_MAP (fail-closed) → runtime wrapper
                → méthode AgriDatabaseService (transaction auto) → PostgreSQL
```

Cartographie complète mesurée :

| Catégorie | Nombre |
|---|---:|
| Outils exposés (introspection de `AgriDatabaseService`) | **113** |
| Scope WRITE | 39 |
| Scope READ | 32 |
| **Sans scope → refusés (fail-closed)** | **41** |
| Référencés par un goal utilisateur | **31** |
| Exposés sans aucun goal (système / interne / gateway / mort) | 82 |

**Le défaut structurel** : l'exposition est produite par
`h.py::_compute_exposed_methods()`, qui introspecte **toutes** les
méthodes async publiques du service. Autrement dit :

> Ajouter une méthode publique à `AgriDatabaseService` crée un outil MCP.
> Seule l'absence de scope l'empêche d'être appelable.

C'est un modèle **deny-by-omission** : il fonctionne (41 outils sont
effectivement refusés), mais il fait dépendre la sécurité d'un oubli
plutôt que d'une décision. Les 4 failles trouvées venaient toutes de là :
trois outils système avaient reçu un scope « par cohérence », et un
quatrième acceptait un statut libre.

**Recommandation (non réalisée ici)** : inverser en allow-list explicite
(un décorateur `@mcp_tool(scope=...)` sur les méthodes réellement
destinées à être des outils). Coût modéré, bénéfice structurel élevé —
voir §27.

## 12. Conversational architecture

Voie canonique unique, vérifiée par contrat :

```
message → interpreter (LLM, catalogue de 39 goals exposés)
        → goal_planner → validator (_RESOLVER_PASSTHROUGH)
        → DomainRouter.decide → tunnel dédié OU context_resolver
        → resolver (résolution de cible : menu numéroté, jamais implicite)
        → confirmation_gate → action prep_* → gateway → outil MCP
        → ResponsePlan → renderer → dispatcher
```

Bypass historiquement présents, tous fermés : exécuteur générique vers
`select_winning_bid` (F4), goals exposés sans outil réel (Phase 4),
résolveurs inatteignables (Phase 3). Deux contrats les maintiennent
fermés (`test_write_capability_reachability`, `test_no_broken_user_goals`).

**Reste un second chemin légitime** : les flows appellent les gateways
directement (`ProductGateway.get_my_products`), sans passer par un
`tool_name` de goal. C'est voulu (résolution de contexte), documenté, et
soumis aux mêmes scopes.

## 13. Response / notification architecture

Les deux responsabilités sont **correctement séparées** :

```
Résultat de domaine → ResponsePlan → renderer → réponse synchrone à l'acteur
Événement métier    → Outbox (MÊME transaction) → cron → WhatsApp au tiers
```

Vérifications : aucun envoi WhatsApp depuis `services/database/` ; aucun
renderer ne mute d'état ; les templates vivent en un seul endroit
(`workers/outbox/templates.py`, 12 templates). La résolution du
destinataire est faite **dans la mutation** (là où la propriété est
connue), pas dans le dispatcher — correct.

## 14. Outbox architecture

`enqueue(session, entries)` dans la transaction métier → `dedupe_key`
unique + `ON CONFLICT DO NOTHING` → cron `outbox-dispatch` → transitions
`PENDING → SENDING → SENT`, ou `DEAD` après épuisement des retries.

**Trou réel** : rien ne consomme `DEAD`. Une notification définitivement
perdue n'est visible que dans les logs. P3 — coût faible si un jour on
veut une alerte.

## 15. Worker architecture

7 tâches planifiées, **toutes enregistrées** (vérifié : `include` de 10
modules → 14 tâches ; les 7 du beat résolvent). `acks_late` +
`visibility_timeout=660s`.

| Tâche | Idempotence | Peut répéter une mutation ? |
|---|---|---|
| outbox-dispatch | `dedupe_key` + statut de ligne | non |
| auction-solicitation / proximity-matching | Outbox | non |
| order-payment-expiry | garde de statut | non |
| 3 × réconciliation | CAS | non |
| IPN Paydunya | `EXECUTION_UNKNOWN` explicite | non |

## 16. External providers

| Fournisseur | Timeout | Retry | Fallback | Sémantique d'échec |
|---|---|---|---|---|
| LLM | oui (chantier timeouts) | oui | **gateway multi-fournisseurs + registre de santé Redis** | dégradation, jamais blocage |
| WhatsApp/Twilio | oui | via Outbox | — | message retardé, jamais perdu sauf `DEAD` |
| Paydunya | — | retry Celery | réconciliation | `EXECUTION_UNKNOWN` |
| PostgreSQL | pool préchauffé | — | — | erreur remontée |
| Redis | — | — | dégradation | cache/registre perdus |

Le **LLM Gateway** est une abstraction **CORE** : elle a une raison
d'être (multi-fournisseurs, santé, coût, timeouts) et un propriétaire
clair. Ne pas y toucher.

## 17. Error architecture

`BusinessRuleException(message, reason=...)` porte le refus métier ;
`SafeDatabaseError`/`sanitize_error_message` neutralisent le technique.
Le wrapper MCP journalise en détail et ne renvoie qu'un message assaini.

Point d'attention connu et déjà exploité volontairement : l'exécuteur
distingue `ValueError` (déclenche un « self-heal » de champ manquant) des
autres exceptions (catch-all propre) — F4 s'appuie sur cette distinction
en levant `RuntimeError`. **C'est subtil et non documenté ailleurs que
dans le code de F4** : à conserver tel quel, mais c'est une convention
implicite (voir §29).

## 18. Data model

| Table | Constat | Classement |
|---|---|---|
| `Order` | large (≈ 40 colonnes), agrège escrow + préorder + RFQ + livraison ; `checkout_group_id` sans état | **TECHNICAL DEBT** (colonnes escrow inutilisées tant qu'`ESCROW_PAYMENT_ENABLED=False`) |
| `Order.version` (Auction) | `Auction.version` documenté DEPRECATED, plus écrit | **SAFE** (colonne conservée, pas de DROP dans ce dépôt) |
| `OrderItem` | pas de statut — **volontaire** : c'est ce qui a fermé le débat multi-producteurs | **SAFE** |
| `Bid` | index unique `(auction_id, producer_id)` | **SAFE** |
| `Auction` | `orders_auction_unique` garantit une commande par enchère | **SAFE** |
| Drafts ×3 | JSONB `payload` + colonnes indexées (`status`, `version`, `order_id`) | **SAFE** — JSONB justifié (forme conversationnelle variable) |
| `outbox` | `dedupe_key` unique | **SAFE** |
| `mcp_idempotency_records` | PK composite | **SAFE** |
| `Product.images`, `pricing_tiers` | JSONB/array | **SAFE** |

Aucune migration recommandée. Les colonnes escrow ne sont pas un risque :
elles sont nullables et le chemin est désactivé.

## 19. DB constraints vs application logic

| Invariant | DB | Code | Test | Risque |
|---|---|---|---|---|
| 1 offre / producteur / enchère | ✅ index unique | ✅ | ✅ | faible |
| 1 commande / enchère | ✅ index unique | ✅ | ✅ | faible |
| 1 livraison / commande | ✅ index unique | — | — | faible |
| Dédoublonnage notification | ✅ unique + ON CONFLICT | ✅ | ✅ | faible |
| Idempotence MCP | ✅ PK composite | ✅ | ✅ | faible |
| Version de draft monotone | ✅ CAS SQL | ✅ | ✅ | faible |
| **1 nouvelle commande = 1 producteur** | ❌ impossible (pas de colonne producteur) | ✅ | ✅ contrat | **moyen** |
| **Seul le propriétaire mute sa ressource** | ❌ | ✅ par méthode | ✅ 5 mutations | **moyen** |
| Statut cible d'un draft ∈ {CANCELLED, SUPERSEDED} | ❌ | ✅ (Phase 7) | ✅ | faible |

Les deux invariants « moyens » ne sont pas contraignables en base sans
changer le modèle — le contrat de test est la bonne réponse ici.

## 20. Redis

Usages : registre de santé LLM, cache de menus/photos, sessions/mappings
conversationnels, broker/backend Celery. **Aucune donnée métier
autoritative** : tout est dérivé ou éphémère, la perte de Redis dégrade
sans corrompre. Pas de seconde source de vérité.

## 21. Draft vs LangGraph state

Décision antérieure : **PostgreSQL est l'autorité, l'état LangGraph est
une projection.** Vérifié :

- Les transitions de draft passent **exclusivement** par les stores CAS.
- `PreorderDraft.order_id` reste l'identité (le groupe voyage à côté).
- En cas de divergence, la réconciliation relit la base et n'invente
  jamais un succès (`EXECUTION_UNKNOWN`).
- `pending_interaction` est un état **conversationnel**, pas métier : sa
  perte fait repartir le tour, jamais une transaction.

**Le code respecte réellement la décision.**

## 22. Dead / legacy architecture

| Élément | État | Danger |
|---|---|---|
| `OrderService` | mort (0 appelant) | **architecture alternative** de cycle de vie de commande |
| `DeliveryMixin` | mort | modèle logistique tiers, sans rapport avec le produit |
| `finalize_multi_order` | mort | **recréerait le P1 multi-producteurs** s'il était câblé — verrouillé par contrat |
| `product_service.py` | mort | recherche produit parallèle |
| `update_production_visibility` | mort | capacité DB sans chemin |
| `update_order_status`, `mark_escrow_paid`, `expire_pending_payments` | vivants pour le système, **fail-closed** en MCP | neutralisés |
| `ensure_extensions()` | mort (pg_trgm créé ailleurs) | aucun |
| `SALES_ACCEPT_CONTRACT` / `commit_staged_transaction` | goal déprécié, outil inexistant | décision produit ouverte |

**Ne rien supprimer** : le risque est la ré-activation accidentelle, et il
est déjà couvert par des contrats de test.

## 23. Abstraction quality

| Abstraction | Raison d'être | Classement |
|---|---|---|
| `@transactional` via `__getattribute__` | frontière transactionnelle unique | **CORE** |
| Mixins de service | découpage par domaine d'un service unique | **USEFUL** |
| Gateways MCP | typage + point d'appel unique vers les outils | **CORE** |
| `PreorderDraft`/`ProcurementDraft`/`SalesPublishDraft` | état transactionnel durable hors LangGraph | **CORE** |
| `PendingInteraction` | discriminant conversationnel unique | **CORE** |
| Outbox | notification transactionnelle | **CORE** |
| `ResponsePlan`/renderers | séparation décision/rendu | **USEFUL** |
| Registry d'actions + `prep_*` | goal → outil, intégrité vérifiée à l'import | **USEFUL** |
| DTO/Command du domaine agent | typage des payloads | **USEFUL**, mais **DUPLICATE** pour les actions à 1 champ (F1/Phase 5 s'en sont volontairement passées) |
| `ToolResolver` | indirection ToolId → nom | **OVERENGINEERED** (aucun override en production) |
| `OrderService`, `DeliveryMixin` | — | **LEGACY** |

## 24. Dependency direction

18 dépendances inversées mesurées. Les significatives :

| Inversion | Nature | Verdict |
|---|---|---|
| `services → graphs.domain.pricing_tiers` / `order_policy` | **règles métier hébergées dans le paquet agent** | **vraie violation** (risque n° 2) |
| `services → graphs.domain.*_draft` | définitions d'état de draft | même famille |
| `services → workers` (Outbox) | 14 imports paresseux | **acceptable** : c'est le prix de l'Outbox transactionnel |
| `workers → api` (celery_app) | 15 imports paresseux | acceptable (structure Celery) |
| `core → services` | warm-up/health | acceptable |
| `actions → graphs` | contexte d'état | acceptable |

## 25. Circular dependencies

11 cycles au niveau des couches, **tous cassés par imports paresseux**
(≈ 80 imports en corps de fonction). Classement :

- `services ↔ workers` : **acceptable** (Outbox, choix assumé).
- `graphs ↔ services`, `graphs ↔ services_agent` : **acceptable** (gateways ↔ flows).
- `domain_agent ↔ graphs`, `actions ↔ domain_agent` : **fragile** — le domaine agent connaît l'orchestration.
- `api ↔ workers`, `infrastructure ↔ protocols` : **acceptable** (bootstrap).

Aucun cycle ne provoque d'échec d'import : ils sont tous stabilisés. Mais
leur nombre explique pourquoi il faut des imports paresseux partout — un
coût de lisibilité permanent.

## 26. Observability

Corrélation disponible : `draft_id`, `order_id`, clé d'idempotence,
identifiant de requête. Événements structurés sur procurement, preorder,
exécution MCP, réconciliation. OTEL + Prometheus + Langfuse initialisés au
démarrage de l'API.

Trou connu : **aucune télémétrie structurée sur le domaine Enchère**
(P3 accepté). Suivre une sélection de gagnant de bout en bout demande de
lire les logs.

## 27. Performance / scalability

| Sujet | Classement |
|---|---|
| Requêtes N+1 | `get_producer_orders` résout le téléphone producteur par article (boucle) — **FUTURE RISK**, volumes actuels faibles |
| État JSON volumineux | checkpointer LangGraph — **maîtrisé** (pruning au flush, caps par entrée) |
| Historique non borné | `tool_execution_history` capé ; `OrderStatusHistory` croît linéairement — **NOT A PROBLEM** |
| Outbox non borné | pas de purge des `SENT` — **FUTURE RISK** (table qui grossit) |
| Index manquants | couverture correcte (`orders_*`, `bids_*`, trigram) — **NOT A PROBLEM** |
| Contention de verrous | verrous par ligne, ordre déterministe — **NOT A PROBLEM** |
| Transactions longues | pas d'appel LLM dans une transaction DB — **NOT A PROBLEM** |

## 28. Test architecture

| Niveau | Fichiers | Tests |
|---|---:|---:|
| unit | 75 | ≈ 828 |
| nodes | 30 | ≈ 681 |
| **architecture (contrats)** | **31** | **≈ 270** |
| integration | 16 | ≈ 148 |
| interpreter | 4 | ≈ 111 |
| chaos | 8 | ≈ 61 |

≈ 2 100 tests. **La couche « contrats d'architecture » (270) est
inhabituellement fournie** — c'est elle qui protège réellement les
invariants (atteignabilité, faux boutons, propriété, un-producteur,
outils fail-closed).

Classe de test absente à ROI élevé : **un test de bout en bout sur base
réelle** (tout est en doubles). Assumé : pas de DB de test dans
l'environnement.

## 29. Architectural invariants

| Invariant | Où garanti | DB | Code | Test |
|---|---|:--:|:--:|:--:|
| Une nouvelle commande de checkout = un producteur | checkout | ❌ | ✅ | ✅ |
| Un gagnant par enchère | `select_winning_bid` | ✅ | ✅ | ✅ |
| Une offre par producteur par enchère | `place_bid` | ✅ | ✅ | ✅ |
| Seul le propriétaire mute sa ressource | méthodes de service | ❌ | ✅ | ✅ |
| Outil MCP désactivé = inatteignable | scope map | ❌ | ✅ | ✅ |
| Goal WRITE = atteignable de bout en bout | validator/routeur | ❌ | ✅ | ✅ |
| Mutation + Outbox = même transaction | `@transactional` | ❌ | ✅ | ✅ |
| Écriture MCP idempotente | store | ✅ | ✅ | ✅ |
| Version de draft monotone | CAS | ✅ | ✅ | ✅ |
| État LangGraph = projection | stores | ❌ | ✅ | ✅ |
| **`RuntimeError` ≠ `ValueError` dans l'exécuteur** (self-heal) | `executor.py` | ❌ | ✅ | partiel |

Le dernier est le seul invariant **encore purement conventionnel** : la
distinction est exploitée par F4 mais n'est décrite nulle part hors du
commentaire de F4.

## 30–31. Risques et remèdes

Voir la table TOP 10 en tête. Classement des remèdes :

| Risque | Remède | Effort |
|---|---|---|
| 1 — exposition MCP opt-out | allow-list explicite (`@mcp_tool`) | **SMALL/MEDIUM REFACTOR** |
| 2 — règles métier dans le paquet agent | déplacer `pricing_tiers`/`order_policy` vers un `domain/` partagé | **MEDIUM REFACTOR** |
| 3 — invariant un-producteur non contraignable | statu quo + contrat | **DO NOTHING** |
| 4 — cycles/imports paresseux | statu quo | **DO NOTHING** |
| 5 — autorisation par convention | garde déclaratif sur les mutations | **MEDIUM REFACTOR** |
| 6 — état JSON | statu quo (déjà capé) | **DO NOTHING** |
| 7 — Outbox `DEAD` sans consommateur | alerte/mesure | **QUICK FIX** |
| 8 — télémétrie Enchère | instrumenter comme procurement | **QUICK FIX** |
| 9 — expiration enchères | décision produit d'abord | **DO NOTHING** |
| 10 — code mort | statu quo + contrats | **DO NOTHING** |

## 32. Architecture cible

La cible **est** l'architecture actuelle, avec deux corrections de
frontière — pas une refonte :

```
Transport (api : webhooks, health, admin)
        ↓
Orchestration conversationnelle (graphs : interpreter → router → tunnels → resolvers → renderers)
        ↓
Actions de domaine (actions : goal → tool)
        ↓
Domaine métier PARTAGÉ            ← [CIBLE] pricing_tiers, order_policy, drafts
        ↓                            aujourd'hui dans graphs/…/domain
Services / persistance (services : autorité d'état + transactions)
        ↓
PostgreSQL

Transverse : Autorisation (frontière déclarative [CIBLE]) · Idempotence ·
             Outbox · Observabilité · Gateways externes (LLM/WhatsApp/Paydunya)
Exposition : MCP en ALLOW-LIST explicite [CIBLE]
```

| Actuel | Cible | Pourquoi | Coût |
|---|---|---|---|
| MCP exposé par introspection | allow-list explicite | supprime la classe entière des 4 failles | moyen |
| `pricing_tiers`/`order_policy` sous `graphs/` | `domain/` partagé | supprime l'inversion `services → graphs` | moyen |
| Autorisation par convention de méthode | garde déclaratif | rend l'oubli impossible | moyen |

---

# WHAT WE SHOULD NOT TOUCH

1. **La frontière transactionnelle** (`@transactional` via `__getattribute__`) — meilleure décision du dépôt, générique et infaillible par oubli.
2. **Les machines à états Order/Bid/Auction et les Drafts CAS** — verrouillage, gardes et récupération sont corrects et éprouvés. Ne pas les convertir en Enum ni en framework.
3. **L'architecture Outbox** — séparation domaine/notification propre, dédoublonnage garanti par la base.
4. **Le modèle d'idempotence à quatre mécanismes** — chacun protège une chose différente ; les unifier ferait perdre des garanties.
5. **`PendingInteraction` et le checkpointer** — un seul point d'écriture, projections dérivées, décision « LangGraph = projection » réellement respectée.
6. **Le LLM Gateway** — abstraction justifiée, propriétaire clair.
7. **Le catalogue d'intents et ses deux contrats** — l'atteignabilité est désormais prouvée, pas supposée.
8. **Le code mort identifié** — le laisser inerte, protégé par contrat, est moins risqué que de le supprimer maintenant.

# WHAT WE SHOULD IMPROVE NEXT (maximum 5)

### 1. Exposition MCP en allow-list explicite
**Pourquoi** : source unique des 4 failles trouvées. **Problème actuel** : toute méthode publique devient un outil ; la sécurité dépend d'une omission. **Bénéfice** : supprime la classe entière. **Complexité** : moyenne (décorateur + liste, `_compute_exposed_methods` filtre dessus). **Risque** : faible (un test compare l'ancienne et la nouvelle liste). **Priorité : P1.**

### 2. Déplacer les règles métier partagées hors du paquet agent
**Pourquoi** : `services/` importe `graphs/…/domain/` — inversion réelle. **Problème** : pricing/paliers/minimum de commande vivent dans l'agent alors qu'ils sont des règles produit. **Bénéfice** : supprime l'inversion, clarifie la propriété. **Complexité** : moyenne (déplacement + imports). **Risque** : faible (pur déplacement, aucune logique modifiée). **Priorité : P2.**

### 3. Garde d'autorisation déclaratif sur les mutations
**Pourquoi** : la propriété est une convention répétée à la main. **Bénéfice** : l'oubli devient impossible plutôt que détecté après coup. **Complexité** : moyenne. **Risque** : moyen (toucher à toutes les mutations). **Priorité : P2 — à faire après 1 et 2.**

### 4. Traiter les `DEAD` de l'Outbox + instrumenter l'Enchère
**Pourquoi** : deux angles morts d'observabilité, tous deux bon marché. **Bénéfice** : une notification définitivement perdue et une sélection de gagnant deviennent visibles. **Complexité** : faible. **Risque** : nul. **Priorité : P3.**

### 5. Purge/archivage des lignes Outbox `SENT`
**Pourquoi** : seule table qui croît sans borne sur le chemin critique. **Bénéfice** : évite une dette d'exploitation. **Complexité** : faible (un cron). **Risque** : faible. **Priorité : P3, quand le volume réel le justifiera.**

---

**Conclusion : ARCHITECTURE STABLE.** Aucun chantier structurel n'est
nécessaire avant la mise en production. Les trois améliorations utiles
(1, 2, 3) sont des corrections de frontière, pas des refontes, et peuvent
attendre le retour du terrain. Le reste de l'énergie doit aller au
déploiement et à l'usage réel, pas au backend.
