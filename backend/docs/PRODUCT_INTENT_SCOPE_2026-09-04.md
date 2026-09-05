# PRODUCT INTENT SCOPE — 2026-09-04 (Phase 4)

## 1. Executive summary

Le catalogue d'intents contenait **70 goals**, dont **27 pointaient un
`tool_name` sans implémentation**. Mais tous n'étaient pas exposés :
un mécanisme préexistant (`_DISABLED_INTENT_PREFIXES = {AGRO_, FINANCE_,
SYSTEM_}`) en retirait déjà 10 du catalogue vu par le LLM.

**Correction d'une affirmation de la Phase 3** : le rapport précédent
disait que les familles STOCK/agronomie « sont déclarées à l'interpréteur,
donc classifiables ». C'est vrai pour STOCK et CROP, **faux pour AGRO_ et
SYSTEM_**, déjà masqués. Le périmètre réel des « faux boutons » n'était
donc pas 27 mais **19 goals** — auxquels s'ajoutaient 2 goals dont l'outil
existe mais dont le handler est volontairement neutralisé
(`PROCUREMENT_SELECT_WINNER`/`ACCEPT_OFFER`, F4) et qui restaient malgré
tout classifiables.

Résultat de cette phase :

```
Catalogue INTENT_CONFIG      70  (inchangé — aucun code métier supprimé)
Exposés au LLM avant         60
Exposés au LLM après         39
Dépréciés (Phase 4)          21
Déjà masqués par préfixe     10
Faux boutons restants         0
```

Aucune capacité fonctionnelle n'a été retirée : chaque goal déprécié est
soit sans implémentation, soit un doublon strict d'un goal exposé qui
fonctionne, soit volontairement neutralisé pour raison de sécurité.

## 2. Current product capability catalogue

39 goals exposés, tous vérifiés par contrat comme réellement exécutables :

**Acheteur (12)** — `SEARCH_PRODUCTS`, `BUYER_REQUEST`, `BUYER_ADD_TO_CART`,
`BUYER_VIEW_CART`, `BUYER_CREATE_PREORDER`, `BUYER_PREORDER_INIT`,
`BUYER_PREORDER_CONFIRM`, `BUYER_NEGOTIATE_PRICE`, `BUYER_LIST_ORDERS`,
`BUYER_CHECK_ORDER_STATUS`, `BUYER_CANCEL_ORDER`, `BUYER_LIST_AUCTIONS`,
`BUYER_CHECK_AUCTION_STATUS`, `PROCUREMENT_CREATE_REQUEST`.

**Producteur (12)** — `SALES_PUBLISH_PRODUCT`, `SALES_UPDATE_PRODUCT`,
`SALES_UNPUBLISH_PRODUCT`, `SALES_UPDATE_PRODUCTION`, `DECLARE_CROP_CYCLE`,
`SALES_RECORD_DIRECT`, `SALES_LIST_ORDERS`, `SALES_GET_CATALOG`,
`SALES_PLACE_BID`, `PRODUCER_CONFIRM_DELIVERY_PAYMENT`,
`PRODUCER_CONFIRM_DELIVERY_OTP`, `STOCK_REGISTER_HARVEST`,
`STOCK_GET_SUMMARY`.

**Marché / RFQ (5)** — `MARKET_BROWSE_REQUESTS`, `MARKET_MY_REQUESTS`,
`MARKET_GET_REQUEST_DETAIL`, `MARKET_GET_MY_PROPOSALS`, `MARKET_SNAPSHOT`.

**Exploitation & profil (6)** — `FARM_CREATE`, `FARM_UPDATE`,
`FARM_GET_MY_LIST`, `PROFILE_SET_GEO`, `PROFILE_SET_PREFS`,
`PROFILE_GET_MCP_USER`, `VALIDATE_PRICE`.

## 3. Valid user-facing goals

Les 39 ci-dessus passent le contrat `test_no_broken_user_goals.py`, **sans
aucune exception tolérée** : outil MCP réel, scope présent, handler
enregistré (pour les WRITE), et entrée `_RESOLVER_PASSTHROUGH` dès qu'un
identifiant technique est requis.

## 4. Invalid/unimplemented goals

| Goal | tool_name | Rôle | Handler | Impl. DB | Utilisé ailleurs ? | Classification |
|---|---|---|---|---|---|---|
| `CROP_START_CYCLE` | `create_crop_cycle` | PRODUCER | oui | **non** | evals `producer_crop/` | DEPRECATE |
| `CROP_RECORD_INTERVENTION` | `log_intervention` | PRODUCER | oui | **non** | evals | DEPRECATE |
| `CROP_RECORD_OBSERVATION` | `add_growth_log` | PRODUCER | oui | **non** | evals | DEPRECATE |
| `CROP_UPDATE_STAGE` | `add_crop_growth_stage` | PRODUCER | oui | **non** | evals | DEPRECATE |
| `CROP_UPDATE_SOIL` | `update_soil_profile` | PRODUCER | oui | **non** | evals | DEPRECATE |
| `STOCK_RECORD_MOVEMENT` | `add_stock_movement_by_id` | PRODUCER | oui | **nom dérivé** (`add_stock_movement` existe) | evals `producer_stock/` | DEPRECATE |
| `STOCK_ADJUST` | `adjust_stock_by_id` | PRODUCER | oui | **nom dérivé** (`adjust_stock`) | evals | DEPRECATE |
| `STOCK_REMOVE_PARTIAL` | `remove_stock_by_id` | PRODUCER | oui | **nom dérivé** (`remove_stock`) | evals | DEPRECATE |
| `STOCK_DELETE` | `delete_stock_by_id` | PRODUCER | oui | **nom dérivé** (`delete_stock`) | evals P1-STOCK-018, P0-SEC-011 | DEPRECATE |
| `STOCK_UPDATE_LEVEL` | `adjust_stock_by_id` | PRODUCER | oui | **nom dérivé** | evals P1-STOCK-001 | DEPRECATE |
| `STOCK_GET_DETAIL` | `get_farm_stocks` | PRODUCER | — | **non** | — | DEPRECATE |
| `STOCK_GET_MOVEMENTS` | `get_stock_movements` | PRODUCER | — | **oui** | — | DEPRECATE (cohérence ledger) |
| `MARKET_SNAPSHOT_ZONAL` | `get_zone_market_overview` | BOTH | — | **non** | — | DEPRECATE (doublon) |
| `SEARCH_NEARBY` | `get_all_zone_market_overview` | BOTH | — | **non** | — | DEPRECATE |
| `DASHBOARD_PRODUCER` | `get_producer_dashboard` | PRODUCER | — | **non** | — | DEPRECATE |
| `PROFILE_GET_TRUST` | `get_trust_score` | BOTH | — | **non** | — | DEPRECATE |
| `PROFILE_GET_CONTEXT` | `get_user_context` | BOTH | — | **non** | — | SYSTEM_ONLY |
| `PROFILE_SWITCH_ROLE` | `create_agent_action` | BOTH | oui | **non** | eval *blocked* | PRODUCT_DECISION |
| `SALES_ACCEPT_CONTRACT` | `commit_staged_transaction` | PRODUCER | oui | **non** | — | PRODUCT_DECISION |
| `PROCUREMENT_SELECT_WINNER` | `select_winning_bid` | BUYER | neutralisé F4 | oui | — | DEPRECATE (sécurité) |
| `PROCUREMENT_ACCEPT_OFFER` | `accept_bid` | BUYER | neutralisé F4 | **non** | — | DEPRECATE (sécurité) |
| `SYSTEM_*` (4), `AGRO_*` (4) | divers | — | — | **non** | evals `producer_crop`, `market_read` | déjà masqués — DEPRECATE confirmé |

## 5. STOCK classification

Questions du mandat, répondues sur preuves :

| Question | Réponse |
|---|---|
| Existe-t-il un parcours utilisateur actuel ? | Partiellement : `STOCK_REGISTER_HARVEST` (`add_stock`) et `STOCK_GET_SUMMARY` (`get_stocks`) **fonctionnent** aujourd'hui. |
| Utilisé par un dashboard / une UI ? | Aucune référence hors backend. |
| Capacité DB réelle ? | **Oui, complète** : `adjust_stock`, `remove_stock`, `delete_stock`, `add_stock_movement`, `get_stock_movements` existent, avec contrôle de propriété (testé : P0-SEC-011 sur la branche de rejet de `delete_stock`). |
| Implémenter créerait-il une fonctionnalité isolée ? | **Oui.** Les ventes débitent `Product.quantity_for_sale` (catalogue) et `MarketOffer.available_quantity` — **jamais** `Stock`. Le ledger d'inventaire est parallèle, sans effet sur ce qui est vendu. |
| Implique-t-il une sémantique de mouvement/réservation non définie ? | **Oui** pour `STOCK_RECORD_MOVEMENT` (Entrée/Sortie/Perte) : sa relation aux débits de vente n'est définie nulle part. |

**Décision appliquée** : les 5 écritures + les 2 lectures à identifiant
sont **dépréciées** (hors catalogue), conformément à la décision produit
antérieure de ne pas exposer un ledger d'inventaire sans besoin démontré.
Les 2 capacités qui fonctionnent restent exposées.

**Incohérence assumée, à trancher** : un producteur peut donc créer des
lignes de stock (`STOCK_REGISTER_HARVEST`) et les consulter
(`STOCK_GET_SUMMARY`) mais jamais les corriger ni les supprimer. Les
données s'accumulent sans correction possible.

> **Option chiffrée si la décision change** : les 4 méthodes DB existent et
> sont testées ; le défaut est un **glissement de nom** (`*_by_id`) dans
> `intent.py`, pas une absence de code. Coût réel : corriger 4 `tool_name`,
> ajouter 5 entrées `_RESOLVER_PASSTHROUGH` (le résolveur `_resolve_stock`
> existe déjà et est déjà dispatché), retirer les goals de
> `_DEPRECATED_INTENTS`. Ce n'est **pas** « construire un stock engine ».
> Non fait ici : la question est l'exposition produit, pas la faisabilité.

## 6. Agronomy classification

`CROP_START_CYCLE`, `CROP_RECORD_INTERVENTION`, `CROP_RECORD_OBSERVATION`,
`CROP_UPDATE_STAGE`, `CROP_UPDATE_SOIL` : **aucune** méthode DB, aucun
modèle de données de suivi cultural, aucune référence frontend, aucune
intégration externe. Seules traces : les handlers `actions/agro.py` (qui
construisent des args pour des outils inexistants) et des scénarios
d'évaluation.

Les 4 lectures `AGRO_*` sont déjà masquées depuis la revue de coût LLM
d'août 2026.

**DEPRECATE.** La capacité « culture » réellement supportée est
`DECLARE_CROP_CYCLE` → `declare_future_production` (production future
réservable par un acheteur) — **exposée et fonctionnelle**, elle couvre le
seul usage agronomique branché sur le métier.

## 7. SYSTEM tool classification

| Goal | Verdict |
|---|---|
| `PROFILE_GET_CONTEXT` (`get_user_context`) | **SYSTEM_ONLY** — enrichissement de contexte pour l'agent, pas une action utilisateur. `get_buyer_context` existe déjà pour l'usage interne. Retiré du catalogue. |
| `SYSTEM_GET_PENDING`, `SYSTEM_BIND_ZONE`, `SYSTEM_REPORT_ANOMALY`, `SYSTEM_COMMIT_TRANSACTION` | déjà masqués (préfixe) — **SYSTEM_ONLY / DEPRECATE** confirmé. À noter : `bind_user_to_zone` existe réellement, donc `SYSTEM_BIND_ZONE` serait recâblable ; sans besoin utilisateur démontré, laissé masqué. |
| `DASHBOARD_PRODUCER`, `PROFILE_GET_TRUST` | **DEPRECATE** — aucun agrégat équivalent côté DB. |

## 8. `SALES_ACCEPT_CONTRACT` analysis

```
Goal:                SALES_ACCEPT_CONTRACT (et son jumeau SYSTEM_COMMIT_TRANSACTION)
Current tool:        commit_staged_transaction
Role:                PRODUCER
Current runtime path: intent -> _RESOLVER_PASSTHROUGH(bid_id) -> _resolve_bid
                     -> prep_sales_accept_contract -> SalesService.accept_contract
                     -> ToolId.COMMIT_STAGED_TRANSACTION -> ***outil inexistant***

Evidence found:
- `commit_staged_transaction` n'existe ni comme outil MCP (112 exposés) ni
  comme méthode de `AgriDatabaseService`.
- `SalesService.accept_contract` retourne ce tool_id INCONDITIONNELLEMENT
  (aucune branche alternative).
- Aucune entité `Contract`, `StagedTransaction` ou table équivalente dans
  `domain/` ni `services/`.
- `ToolResolver` n'a aucun override en production.
- Aucun dataset d'évaluation, aucune référence frontend ou documentaire.

Classification: PRODUCT_DECISION
```

Les 8 questions obligatoires du mandat restent **sans réponse déterminable
depuis le dépôt** : qu'est-ce qu'un contrat, qui le crée, qui l'accepte,
quelle entité le stocke, quels statuts changent, qui est notifié, quel est
l'état terminal, quel rapport avec `Order` ? Rien dans le code ne permet
d'y répondre.

**Interprétations possibles** (aucune retenue) :
1. **Doublon d'un mécanisme existant** — « accepter un contrat » = accepter une offre de négociation ou une enchère gagnante ; les deux ont déjà leur chemin sécurisé. Conséquence : suppression pure.
2. **Contractualisation à terme** — un engagement d'achat récurrent producteur↔acheteur, entité et cycle de vie à créer. Conséquence : nouveau domaine complet (entité, statuts, notifications, litiges).
3. **Reliquat d'un flux « staging » abandonné** — `staging_id` de `SYSTEM_COMMIT_TRANSACTION` suggère un mécanisme de transaction en deux temps aujourd'hui remplacé par les Drafts + CAS. Conséquence : suppression.

**Décision requise** avant toute ligne de code. En attendant : retiré du
catalogue exposé (plus de faux bouton), code intact.

## 9. Goals to KEEP

Les 39 exposés (§2) — vérifiés par contrat, sans exception.

## 10. Goals to IMPLEMENT

**Aucun.** Aucune capacité déclarée ne réunit les deux conditions requises
(nécessaire au produit actuel **et** comportement métier suffisamment
déterminé). Les deux candidats techniquement faciles — le ledger STOCK et
`SYSTEM_BIND_ZONE` — échouent sur la première condition, pas sur la
seconde : c'est une décision d'exposition produit, documentée en §5 et §7.

## 11. Goals to DEPRECATE

21 goals, retirés du catalogue exposé, code intact :
`CROP_START_CYCLE`, `CROP_RECORD_INTERVENTION`, `CROP_RECORD_OBSERVATION`,
`CROP_UPDATE_STAGE`, `CROP_UPDATE_SOIL`, `STOCK_RECORD_MOVEMENT`,
`STOCK_ADJUST`, `STOCK_REMOVE_PARTIAL`, `STOCK_DELETE`,
`STOCK_UPDATE_LEVEL`, `STOCK_GET_DETAIL`, `STOCK_GET_MOVEMENTS`,
`MARKET_SNAPSHOT_ZONAL`, `SEARCH_NEARBY`, `DASHBOARD_PRODUCER`,
`PROFILE_GET_TRUST`, `PROFILE_GET_CONTEXT`, `PROFILE_SWITCH_ROLE`,
`SALES_ACCEPT_CONTRACT`, `PROCUREMENT_SELECT_WINNER`,
`PROCUREMENT_ACCEPT_OFFER`.

Format détaillé pour les cas non évidents :

```
Goal:                 PROCUREMENT_SELECT_WINNER
Current tool:         select_winning_bid (RÉEL et fonctionnel)
Role:                 BUYER
Current runtime path: exécuteur générique -> handler neutralisé (RuntimeError, F4)
Evidence found:
- F4 avait neutralisé le HANDLER (contournement du tunnel sécurisé) mais
  le goal restait CLASSABLE : un utilisateur pouvait donc l'atteindre et
  ne récolter qu'une erreur technique.
- Le chemin légitime (`BUYER_CHECK_AUCTION_STATUS` -> confirm/finalize)
  reste exposé et fonctionnel.
Classification:       DEPRECATE (sécurité)
Reason:               une seule entrée métier vers la sélection du gagnant.
Risk of leaving it exposed: erreur technique côté utilisateur + surface de
                      contournement rouverte si le handler était un jour
                      « réparé » par inadvertance.
Impact of changing it: nul — capacité couverte ailleurs.
Tests required:       test_no_broken_user_goals (exposition) +
                      test_winner_selection_single_entrypoint (F4, conservé).
```

```
Goal:                 MARKET_SNAPSHOT_ZONAL
Current tool:         get_zone_market_overview (inexistant)
Role:                 BOTH
Evidence found:
- `MARKET_SNAPSHOT` (`get_market_snapshot`, MÊME `required=['zone']`)
  existe, fonctionne et reste exposé : doublon strict.
Classification:       DEPRECATE
Risk of leaving it exposed: le LLM peut choisir la variante cassée plutôt
                      que la variante fonctionnelle pour le même message.
Impact of changing it: nul, capacité identique conservée.
```

## 12. Goals to REMOVE

**Aucun.** La suppression physique n'a été retenue pour aucun goal : tous
conservent des consommateurs (handlers enregistrés exigés par
`registry.validate_integrity`, scénarios d'évaluation, tests de nœuds).
Retirer une entrée d'`INTENT_CONFIG` casserait ces conventions sans
bénéfice — la dépréciation d'exposition atteint l'objectif produit
(« aucun faux bouton ») sans risque de rupture.

## 13. Goals requiring PRODUCT_DECISION

1. **`SALES_ACCEPT_CONTRACT` / `SYSTEM_COMMIT_TRANSACTION`** — §8, 3 interprétations.
2. **`PROFILE_SWITCH_ROLE`** — écrit une ligne `agent_actions` qu'aucun code ne lit (prouvé dans `tests/evals/blocked/PROFILE_SWITCH_ROLE.md` : « la fonctionnalité est échafaudée mais branchée à rien »). Options : construire la bascule de rôle, ou la retirer du produit. Note : la refonte double-rôle rend la bascule explicite largement inutile — un même utilisateur vend et achète message par message.
3. **Ledger d'inventaire STOCK** — §5, avec le coût chiffré de l'option « exposer ».
4. **Rejet/annulation producteur d'une commande CONFIRMED** — hérité de la Phase 2, inchangé.

## 14. Runtime safety rules

- La dépréciation agit **uniquement** sur la surface de classification
  (`allowed_intents_for_role`), consommée par le prompt d'interprétation et
  par la validation de l'intent renvoyé par le LLM. `INTENT_CONFIG`,
  `INTENT_ROLE`, les handlers, les DTO et les services restent **intacts** :
  aucune règle métier supprimée, `registry.validate_integrity()` inchangé.
- Effet de bord favorable : le catalogue décrit au LLM passe de 60 à 39
  entrées, ce qui réduit d'autant le prompt d'interprétation (le même levier
  que la revue de coût d'août 2026 sur le quota Groq).
- Un goal déprécié qui redeviendrait fonctionnel est **détecté** par
  `test_deprecating_a_working_goal_is_flagged` : il faut alors le ré-exposer
  ou justifier explicitement son maintien hors catalogue.

**Cas problématique documenté (non refactoré)** : plusieurs goals utilisent
un nom d'outil MCP directement comme identité de capacité utilisateur
(`INTENT_CONFIG["…"]["tool_name"]`), au lieu de passer par une action de
domaine qui choisit ensuite un outil autorisé. C'est ce couplage qui a
permis le glissement `adjust_stock` → `adjust_stock_by_id` sans que rien
ne le détecte. Les contrats de Phase 3/4 compensent ce couplage ; le
découplage lui-même n'est pas entrepris ici.

## 15. Tests added

| Fichier | Rôle |
|---|---|
| [test_no_broken_user_goals.py](../tests/architecture/test_no_broken_user_goals.py) | **Contrat « aucun faux bouton »** — pour chaque goal réellement exposé : outil MCP réel, scope, handler (WRITE), passthrough si identifiant technique. **Zéro exception tolérée.** Plus : la dépréciation est honnête (goals toujours présents au catalogue, aucun goal déprécié encore exposé), un goal déprécié redevenu fonctionnel est signalé, et 25 capacités vivantes doivent rester exposées (garde anti-dépréciation trop large). |
| `test_write_capability_reachability.py` (Phase 3, conservé) | contrat sur l'ensemble des 70 goals, avec exceptions documentées — couvre le pattern générique `required + resolver + validator` qui avait produit le bug F1. |

Le contrat a immédiatement prouvé son utilité : il a rejeté
`STOCK_GET_MOVEMENTS` (exposé, outil fonctionnel, mais sans passthrough →
impasse garantie), ce qui a forcé un arbitrage explicite plutôt qu'un oubli
silencieux.

## 16. Final intent count

```
GOAL CATALOG BEFORE                 70
KEEP (exposés, vérifiés)            39
IMPLEMENTED                          0
DEPRECATED (retirés du catalogue)   21
REMOVED (code supprimé)              0
SYSTEM_ONLY                          1  (PROFILE_GET_CONTEXT) + 4 déjà masqués SYSTEM_*
PRODUCT_DECISION                     4  (contrat, bascule de rôle, ledger STOCK, annulation producteur)
BROKEN USER-FACING GOALS             0
RUNTIME REACHABILITY CONTRACT     PASS
```

---

# PRODUCT CAPABILITY STATUS

```
BUYER              COMPLETE
  recherche, paliers, panier, checkout, confirmation, suivi, historique,
  annulation, RFQ, offres, négociation, sélection du gagnant, réception
  des notifications de clôture

PRODUCER           COMPLETE (1 gap en décision produit)
  publication, édition, retrait de produit, production future, offres,
  suivi des commandes des 3 origines, clôture livraison+paiement, vente
  directe, escrow OTP
  GAP : refuser/annuler une commande CONFIRMED (décision produit)

CATALOG            COMPLETE
  créer, modifier, retirer ; pas de résurrection d'un produit archivé ;
  paliers et minimum d'achat cohérents au checkout

RFQ/AUCTION        COMPLETE
  création, offres, mise à jour, retrait, sélection du gagnant par un
  chemin unique et sécurisé, notification gagnant + perdants

ORDER/FULFILLMENT  COMPLETE
  DRAFT → CONFIRMED → COMPLETED, annulation acheteur à chaque étape,
  paiement à la livraison et escrow OTP

INVENTORY (STOCK)  HORS CATALOGUE (décision produit)
  création et consultation exposées ; correction, suppression et
  mouvements retirés — le ledger n'est pas relié aux ventes
```

## Known historical test failures

```
tests/unit/test_create_auction_catalog_gate.py::TestCreateAuctionCatalogResolution::
  test_no_candidate_at_all_gets_auto_provisioned_not_rejected
  test_a_fuzzy_matched_but_unconfident_candidate_falls_back_to_auto_provisioning
  test_a_confidently_matched_candidate_is_used_as_is
  test_the_second_stage_fuzzy_matcher_is_also_confidence_gated
(bug de date codée en dur, antérieur à cette session, jamais touché)
```
