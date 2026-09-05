# RUNTIME REACHABILITY AUDIT — 2026-09-04 (Phase 3)

Question posée : **pour chaque capability déclarée, peut-on prouver
qu'elle est réellement atteignable depuis une interaction utilisateur et
qu'elle aboutit à un résultat métier correct ?**

Méthode : la chaîne complète a été parcourue **programmatiquement** pour
les 70 goals déclarés, en croisant `INTENT_CONFIG`, `INTENT_ROLE`,
`_TUNNEL_ASSIGNMENTS`, le registre d'actions, `_RESOLVER_PASSTHROUGH`
(lu sur la source réelle du validateur), `TOOL_SCOPE_MAP` et la liste
d'outils MCP réellement exposée (`TOOL_DESCRIPTIONS`, produite par
introspection de `AgriDatabaseService` — 112 outils). Aucune conclusion
n'est tirée d'un nom de fonction.

```
Total goals déclarés : 70   —   WRITE : 39   READ : 31
Outils MCP réellement exposés : 112
```

---

## 1. Le maillon qui casse : `_RESOLVER_PASSTHROUGH`

Mécanisme exact, vérifié sur le code :

```
required contient un identifiant technique (`*_id`, hors farm_id/phone)
        ↓
validation.py::_missing_fields_for_goal  → champ manquant
        ↓
validation.py::_is_technical_id_field    → True
        ↓
branche `blocking_id` → status=COMPLETED, response_strategy=CLARIFICATION
        ↓
le tour se termine.  context_resolver n'est JAMAIS atteint.
```

Correction : une entrée `_RESOLVER_PASSTHROUGH` fait retourner `PLANNING`
sans `missing_fields`, ce qui laisse `DomainRouter.decide` →
`make_route_after_validator` étape 4 → `to_resolver`.

**Précision par rapport au rapport de Phase 2** : le symptôme réel n'est
pas « l'agent réclame un UUID » (ce comportement-là a été corrigé plus
tôt par la branche `blocking_id`) mais « l'agent répond une clarification
générique et le tour se termine sans rien exécuter ». L'impasse est la
même, la formulation était imprécise.

### RESOLVER_PASSTHROUGH findings

| Goal | Type | Identifiant | Tool réel ? | Verdict |
|---|---|---|---|---|
| `STOCK_GET_MOVEMENTS` | READ | `stock_id` | ✅ `get_stock_movements` existe | **REAL GAP** — seul cas où le back-end fonctionne et où seul le câblage manque |
| `STOCK_RECORD_MOVEMENT`, `STOCK_ADJUST`, `STOCK_REMOVE_PARTIAL`, `STOCK_DELETE`, `STOCK_UPDATE_LEVEL` | WRITE | `stock_id` | ❌ `*_by_id` inexistants | **LEGACY** — doublement cassés ; réparer le passthrough seul ne les ferait pas fonctionner |
| `CROP_RECORD_INTERVENTION`, `CROP_RECORD_OBSERVATION`, `CROP_UPDATE_STAGE` | WRITE | `cycle_id` | ❌ | **LEGACY** (verticale agronomie non construite) |
| `AGRO_GET_ECONOMICS` | READ | `cycle_id` | ❌ | **LEGACY** |
| `SYSTEM_REPORT_ANOMALY` | WRITE | `target_id` | ❌ | **DEAD FEATURE** |
| `SYSTEM_COMMIT_TRANSACTION` | WRITE | `staging_id` | ❌ | **NEEDS PRODUCT DECISION** |

Les 12 goals qui **possèdent** une entrée passthrough ont tous été
re-vérifiés : 10 pointent vers un outil réel, 2 vers un outil inexistant
(`SALES_ACCEPT_CONTRACT`, `PROCUREMENT_ACCEPT_OFFER` — ce dernier
volontairement neutralisé par F4).

---

## 2. Tool / permission mismatches

**27 goals déclarent un `tool_name` sans aucune implémentation** (ni outil
MCP exposé, ni méthode de `AgriDatabaseService`). Aucun n'appartient aux
parcours transactionnels principaux.

| Famille | Goals | `tool_name` déclaré | Classement |
|---|---|---|---|
| STOCK (écriture) | `STOCK_RECORD_MOVEMENT`, `STOCK_ADJUST`, `STOCK_REMOVE_PARTIAL`, `STOCK_DELETE`, `STOCK_UPDATE_LEVEL` | `add_stock_movement_by_id`, `adjust_stock_by_id`, `remove_stock_by_id`, `delete_stock_by_id` | **LEGACY** — les vraies méthodes existent sans le suffixe `_by_id` ; famille déprioritisée par décision produit |
| Agronomie | `CROP_START_CYCLE`, `CROP_RECORD_INTERVENTION`, `CROP_RECORD_OBSERVATION`, `CROP_UPDATE_STAGE`, `CROP_UPDATE_SOIL` | `create_crop_cycle`, `log_intervention`, `add_growth_log`, `add_crop_growth_stage`, `update_soil_profile` | **DEAD FEATURE** — verticale jamais construite côté DB |
| Agronomie (lecture) | `AGRO_GET_CYCLES`, `AGRO_GET_STANDARDS`, `AGRO_GET_ECONOMICS`, `AGRO_GET_RISKS` | `get_crop_cycles`, `get_crop_requirements`, `get_cycle_economics`, `get_active_sanitary_risks` | **DEAD FEATURE** |
| Contrat / staging | `SALES_ACCEPT_CONTRACT`, `SYSTEM_COMMIT_TRANSACTION` | `commit_staged_transaction` | **NEEDS PRODUCT DECISION** |
| Enchère (héritée) | `PROCUREMENT_ACCEPT_OFFER` | `accept_bid` | **DEAD** (neutralisé F4, volontaire) |
| Système / profil | `SYSTEM_REPORT_ANOMALY`, `SYSTEM_BIND_ZONE`, `PROFILE_SWITCH_ROLE` | `report_anomaly`, `create_agent_action` | **DEAD FEATURE** |
| Lectures diverses | `STOCK_GET_DETAIL`, `MARKET_SNAPSHOT_ZONAL`, `PROFILE_GET_TRUST`, `PROFILE_GET_CONTEXT`, `DASHBOARD_PRODUCER`, `SEARCH_NEARBY`, `SYSTEM_GET_PENDING` | `get_farm_stocks`, `get_zone_market_overview`, `get_trust_score`, `get_user_context`, `get_producer_dashboard`, `get_all_zone_market_overview`, `get_pending_actions` | **DEAD FEATURE** — plusieurs sont des quasi-homonymes de méthodes réelles (`get_producer_stocks`, `get_producer_orders`…), mais aucune équivalence sémantique n'est démontrable depuis le code |

**Aucune anomalie de permission** : tout `tool_name` réellement
implémenté et dispatché possède son entrée `TOOL_SCOPE_MAP` (vérifié par
contrat, `test_real_tools_have_an_mcp_scope`). Le `ToolScopeWarmup`
enregistre au démarrage les 28 noms déclarés-mais-inexistants : cela leur
donne un scope, jamais une implémentation — ce n'est donc pas un
contournement d'autorité.

**Modèle d'autorité, re-vérifié** : la frontière de sécurité n'est pas
« quel rôle voit quel intent » (le blocage par préfixe de `graphs/roles.py`
est du code mort depuis la refonte double-rôle) mais **l'épinglage
d'identité** : `services/mcp/schema_resolver.py::lookup_arg_value` résout
`phone`/`user_phone`/`producer_id`/`user_id` TOUJOURS depuis le `state`
authentifié, jamais depuis le payload (texte libre, forgeable), puis
chaque méthode DB filtre la ressource par le profil ainsi dérivé. Les deux
capacités ajoutées récemment respectent ce modèle :
`prep_sales_unpublish_product` et `prep_confirm_delivery_and_payment`
prennent `DomainContext.from_state(state).phone`, jamais un identifiant
fourni par l'utilisateur ; `delete_product` et
`confirm_delivery_and_payment` re-filtrent ensuite par propriété
(`Product.producer_id`, `OrderItem→Product.producer_id` ou
`Order.winning_bid_id→Bid.producer_id`). Couvert par
`tests/chaos/test_role_isolation.py`.

**Faux positifs écartés** : 9 goals (`BUYER_ADD_TO_CART`, `BUYER_VIEW_CART`,
`BUYER_CREATE_PREORDER`, `BUYER_PREORDER_INIT`, `BUYER_PREORDER_CONFIRM`,
`BUYER_NEGOTIATE_PRICE`, `BUYER_CHECK_ORDER_STATUS`, `BUYER_LIST_ORDERS`,
`BUYER_CANCEL_ORDER`) déclarent un `tool_name` symbolique
(`add_to_cart`, `cancel_order`…) qui n'est **pas** un outil MCP : ils sont
`handled_by_flow` + tunnelés, et leur flow appelle lui-même les vraies
méthodes via les gateways. Chaîne vérifiée manuellement pour chacun.

---

## 3. DB capability without conversation path

Recherche inverse (méthode métier → existe-t-il un parcours qui l'appelle ?) :

| Méthode | Statut |
|---|---|
| `delete_product` | **CORRIGÉ en Phase 2** (`SALES_UNPUBLISH_PRODUCT`) |
| `update_production_visibility` | **DEAD** — publier/dépublier un lot de production future ; complète (propriété vérifiée), aucun intent. Câblage identique à celui de `delete_product` si le besoin est confirmé |
| `OrderService.*` (`create_order`, `advance_order_status`, `record_payment`, `schedule_reminder`…) | **DEAD** — jamais montée dans les mixins |
| `DeliveryMixin.*` (`create_delivery`, `claim_delivery`, OTP coursier) | **DEAD** — modèle « réseau de livreurs tiers » sans rapport avec le modèle métier réel |
| `product_service.py::search_products` | **DEAD** — la vraie recherche est `buyer.py::search_products` |
| `expire_pending_payments`, `get_pending_buyer_verifications`, `revoke_buyer_trust_badge` | appelées par les workers / l'admin, hors chemin conversationnel — **normal** |

## 4. Conversation capability without valid DB path

Exactement la table §2 : 27 goals déclarés sans implémentation. Aucun
n'appartient aux parcours acheteur/producteur principaux — ceux-ci ont
tous été re-vérifiés maillon par maillon (§6).

---

## 5. READ capabilities

Le bug `is_available` de Phase 2 (read model ignorant un invariant métier)
a été généralisé aux lectures principales :

| Lecture | Invariant attendu | Vérifié |
|---|---|---|
| `search_products` (acheteur) | ni produit archivé, ni produit fantôme de vente directe | ✅ corrigé Phase 2 (`is_available`) |
| `get_buyer_orders_dashboard` | pas de `DRAFT`/`SUPERSEDED` ; `CANCELLED`/`COMPLETED` correctement libellés | ✅ |
| `get_transaction_summary` | idem sur la branche « transaction récente » ; lookup explicite non filtré (volontaire) | ✅ |
| `get_producer_orders` | pas de `DRAFT`/`SUPERSEDED` ; inclut les 3 origines (catalogue, préorder, RFQ) | ✅ |
| `get_auction_bids` / `get_auctions` | propriété acheteur vérifiée | ✅ |
| `get_my_active_bids` | filtre sur `User.phone` **uniquement** — retourne aussi les offres `WITHDRAWN`/`LOST` malgré le nom « active » ; le statut de l'enchère est bien SELECTé, donc le rendu peut les qualifier | ⚠️ **P3** — sémantique du nom, aucun impact transactionnel démontré |
| `get_my_products` | renvoie tout, y compris archivés et fantômes | ⚠️ **P3** — filtré côté conversation là où c'est nécessaire (menu de retrait) |
| `get_stock_movements` | — | ⚠️ **P2** — inatteignable (§1) |

Aucun contournement de propriété (`ownership bypass`) détecté : toutes les
lectures de commandes/enchères passent par le profil résolu depuis le
téléphone.

---

## 6. Parcours critiques — chaîne complète re-vérifiée

| Capability | role | validator | passthrough | resolver | handler | tool | scope | DB | terminal |
|---|---|---|---|---|---|---|---|---|---|
| `BUYER_ADD_TO_CART` | PASS | PASS | n/a (flow) | PASS | flow | flow | n/a | `active_cart` | PASS |
| `BUYER_PREORDER_INIT` | PASS | PASS | n/a (flow) | PASS | flow | flow | n/a | `Order(DRAFT)` | PASS |
| `BUYER_PREORDER_CONFIRM` | PASS | PASS | n/a (flow) | PASS | flow | flow | n/a | `CONFIRMED` | PASS |
| `BUYER_CANCEL_ORDER` | PASS | PASS | n/a (flow) | PASS | flow | flow | n/a | `CANCELLED` | PASS |
| `BUYER_CHECK_AUCTION_STATUS` → winner | PASS | PASS | n/a (flow) | PASS | flow | `select_winning_bid` | PASS | `CONFIRMED` | PASS |
| `BUYER_NEGOTIATE_PRICE` | PASS | PASS | n/a (flow) | PASS | flow | flow | n/a | session | PASS |
| `PROCUREMENT_CREATE_REQUEST` | PASS | PASS | n/a | n/a | PASS | `create_auction` | PASS | `Auction(OPEN)` | PASS |
| `SALES_PUBLISH_PRODUCT` | PASS | PASS | n/a | n/a | PASS | `create_product` | PASS | `Product` | PASS |
| `SALES_UNPUBLISH_PRODUCT` | PASS | PASS | **PASS** | PASS | PASS | `delete_product` | PASS | archivage/suppression | PASS |
| `SALES_UPDATE_PRODUCT` | PASS | PASS | PASS | PASS | PASS | `update_product_price_and_qty` | PASS | `Product` | PASS |
| `SALES_PLACE_BID` | PASS | PASS | PASS | PASS | PASS | `place_bid` | PASS | `Bid(PENDING)` | PASS |
| `SALES_RECORD_DIRECT` | PASS | PASS | n/a | n/a | PASS | `record_sale` | PASS | `COMPLETED` | PASS |
| `SALES_LIST_ORDERS` | PASS | PASS | n/a | n/a | PASS | `get_producer_orders` | PASS | lecture | PASS |
| `PRODUCER_CONFIRM_DELIVERY_PAYMENT` | PASS | PASS | **PASS** (corrigé Ph.2) | PASS | PASS | `confirm_delivery_and_payment` | PASS | `COMPLETED` | PASS |
| `PRODUCER_CONFIRM_DELIVERY_OTP` | PASS | PASS | n/a (tunnel) | PASS | flow | `verify_delivery_otp` | PASS | `PAID_OUT` | PASS |
| `SALES_ACCEPT_CONTRACT` | PASS | PASS | PASS | PASS | PASS | **FAIL** | — | — | **BROKEN** |

---

## 7. Renderer gaps

Les renderers des goals WRITE réellement atteignables produisent tous un
message pour succès, erreur métier (`BusinessRuleException` préservée),
erreur technique (message neutre) et rejeu idempotent. Deux points relevés :

- `flows/buyer/order_tracking.py::cancel_order` affichait « Seules les
  commandes *en attente* peuvent être annulées » — formulation devenue
  fausse après l'élargissement du garde en Phase 1. **Corrigé en Phase 1.**
- Les goals dont le `tool_name` n'existe pas produisent un message
  technique générique via le catch-all de l'exécuteur : dégradé propre,
  jamais une mutation silencieuse.

## 8. Dead-end conversations

| Cas | Verdict |
|---|---|
| Goal exigeant un identifiant technique sans passthrough | dead-end **clarification** — 12 goals (§1), tous legacy sauf `STOCK_GET_MOVEMENTS` |
| Goal avec `tool_name` inexistant | dead-end **erreur technique** — 27 goals (§2) |
| Parcours transactionnels principaux | aucun dead-end (§6) |
| Producteur voulant refuser une commande `CONFIRMED` | pas de dead-end technique : aucune capacité déclarée — décision produit ouverte (Phase 2, P1-2) |

---

## 9. Priorisation

**P0 — aucun.** Aucune action critique impossible sur les parcours
principaux, aucune autorité contournable (le fail-closed MCP tient, F4
reste intact).

**P1 — aucun nouveau.** Les deux P1 de Phase 2 sont corrigés ; le P1
restant (`rejet/annulation producteur`) est une décision produit,
inchangée.

**P2**
1. `STOCK_GET_MOVEMENTS` — back-end fonctionnel, câblage manquant.
   *Correction minimale (non appliquée)* : ajouter
   `"STOCK_GET_MOVEMENTS": ("stock_id", [])` à `_RESOLVER_PASSTHROUGH` et
   `"STOCK_GET_MOVEMENTS"` au set dispatchant `_resolve_stock` dans
   `flows/producer/flow.py`. **Non appliqué** : la famille STOCK a été
   explicitement déprioritisée par décision produit — la corriger seule
   donnerait une capacité de lecture isolée dans une famille dont toutes
   les écritures restent cassées.
2. `SALES_ACCEPT_CONTRACT` / `SYSTEM_COMMIT_TRANSACTION` — inchangé
   (décision produit, Phase 2).

**P3**
- 27 `tool_name` sans implémentation (familles STOCK/agronomie/système) —
  soit à recâbler vers les vraies méthodes, soit à retirer du catalogue
  d'intents ; les deux exigent une décision, aucune ne bloque un parcours.
- `get_my_active_bids` : nom trompeur vs contenu.
- `get_my_products` : mélange actifs / archivés / fantômes.
- Code mort : `update_production_visibility`, `OrderService`,
  `DeliveryMixin`, `product_service.py`.

---

## 10. Tests added

| Fichier | Rôle |
|---|---|
| [test_write_capability_reachability.py](../tests/architecture/test_write_capability_reachability.py) | **Contrat de reachability** — vérifications paramétrées sur les 70 goals : rôle, handler, `tool_name` réellement exposé, scope MCP, et surtout « identifiant technique requis ⇒ entrée `_RESOLVER_PASSTHROUGH` » (le contrôle qui aurait attrapé F1). Les exceptions connues sont listées **avec leur justification** (`legacy` / `product-decision` / `disabled` / `system-only` / `real-gap`) ; une capacité réparée fait échouer son exception, ce qui force à la retirer. |
| ↳ `TestRealEntryPointReachesTheResolver` | **Preuve dynamique** (mandat §5/§6 : « ne teste pas uniquement le résolveur directement ») — entre par le VRAI validateur puis le VRAI `DomainRouter.decide`, et exige la décision `to_resolver`, pour `PRODUCER_CONFIRM_DELIVERY_PAYMENT`, `SALES_UNPUBLISH_PRODUCT`, `SALES_UPDATE_PRODUCT` et `MARKET_GET_REQUEST_DETAIL`. |

**Note de méthode** : la première version de ce test dynamique échouait
sur 3 goals sur 4 — non pas à cause du produit, mais parce que
`make_state()` pose `interpreted_event="UNKNOWN"`, que le routeur traite
(à raison) comme une dérive vers `to_strategy`. Le test a été corrigé pour
poser un événement nominal (`NEW_TASK`) : sans cette vérification, il
aurait mesuré le harnais de test et signalé un faux gap produit.

La table `_RESOLVER_PASSTHROUGH` est lue **sur la source réelle** du
validateur, jamais recopiée : une copie dériverait, ce qui est exactement
le bug traqué.

**Validation par mutation** : l'entrée `PRODUCER_CONFIRM_DELIVERY_PAYMENT`
a été retirée temporairement du validateur — le contrat échoue avec le
message attendu ; entrée restaurée, il repasse. Le garde détecte donc
réellement le bug F1, il ne se contente pas de décrire l'état courant.

## 11. Fixed in this phase

Aucune correction de code n'était requise : les deux gaps de wiring
identifiés (F1 et `delete_product`) avaient été corrigés en Phase 2, et le
seul gap résiduel (`STOCK_GET_MOVEMENTS`) appartient à une famille
explicitement déprioritisée. **Cette phase livre la preuve et le garde,
pas de nouveaux changements produit** — conformément à la règle
anti-refactor.

## 12. Needs product decision

1. **Rejet/annulation producteur d'une commande `CONFIRMED`** (Phase 2, P1-2) — 3 options documentées.
2. **`SALES_ACCEPT_CONTRACT` / `SYSTEM_COMMIT_TRANSACTION`** — que doit faire « valider un contrat verrouillé » ?
3. **Familles STOCK et agronomie** — recâbler vers les vraies méthodes, ou retirer du catalogue d'intents ? Aujourd'hui elles sont déclarées à l'interpréteur, donc classifiables, donc atteignables par un utilisateur… pour finir en erreur technique.

---

# CRITÈRE DE SORTIE

## A. READY / COMPLETE

Parcours prouvés traversables de bout en bout (chaîne complète vérifiée, §6) :

```
BUYER    recherche → paliers → panier → checkout → confirmation
         → suivi → annulation → historique
         RFQ → offres → négociation → sélection gagnant → commande → COMPLETED
PRODUCER publication → édition → retrait de produit
         offres (poser / mettre à jour / retirer) → gain/perte notifiés
         commandes (catalogue + préorder + RFQ) → livraison + paiement → COMPLETED
         vente directe cash → COMPLETED ; escrow OTP → PAID_OUT
```

## B. FIXED IN THIS PHASE

```
(aucune correction produit — les gaps de wiring avaient été fermés en Phase 2)
+ garde de non-régression : tests/architecture/test_write_capability_reachability.py
  - contrat statique sur les 70 goals (validé par mutation sur le bug F1)
  - preuve dynamique validateur -> routeur -> to_resolver sur 4 goals
```

## C. NOT COMPLETE

| Catégorie | Éléments |
|---|---|
| **Real gap** | `STOCK_GET_MOVEMENTS` (back-end OK, câblage manquant — correction minimale documentée, non appliquée : famille déprioritisée) |
| **Legacy** | familles STOCK (5 écritures) et agronomie CROP_/AGRO_ (9 goals) : `tool_name` sans implémentation ; lectures système `get_trust_score`, `get_user_context`, `get_producer_dashboard`, `get_farm_stocks`, `get_zone_market_overview`, `get_all_zone_market_overview`, `get_pending_actions` ; code mort (`update_production_visibility`, `OrderService`, `DeliveryMixin`, `product_service.py`) |
| **Needs product decision** | rejet/annulation producteur d'une commande confirmée ; sémantique de `SALES_ACCEPT_CONTRACT`/`SYSTEM_COMMIT_TRANSACTION` ; sort des familles STOCK/agronomie (recâbler ou retirer) |
| **Out of scope** | paiement en ligne (escrow implémenté mais désactivé — paiement à la livraison), remboursement, exécution partielle, remplacement, report de livraison, réservation de stock, litige |

## KNOWN HISTORICAL TEST FAILURES

```
tests/unit/test_create_auction_catalog_gate.py::TestCreateAuctionCatalogResolution::
  test_no_candidate_at_all_gets_auto_provisioned_not_rejected
  test_a_fuzzy_matched_but_unconfident_candidate_falls_back_to_auto_provisioning
  test_a_confidently_matched_candidate_is_used_as_is
  test_the_second_stage_fuzzy_matcher_is_also_confidence_gated
(bug de date codée en dur, antérieur à cette session, jamais touché)
```
