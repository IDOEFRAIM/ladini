# PRODUCT COMPLETENESS — PHASE 2 (2026-09-04)

## 1. Executive summary

Phase 1 concluait « 9/11 journeys complets, 0 P0, 2 P1 ». Cette phase a
posé une question plus dure : **quelles actions un buyer ou un producer
s'attend raisonnablement à pouvoir faire, mais qu'Ladini ne permet pas
réellement de terminer ?** — en remontant chaque capacité de l'intent
jusqu'à l'état terminal, et en refusant de déclarer COMPLETE une capacité
au motif qu'une fonction en porte le nom.

Résultat : **3 gaps réels supplémentaires**, dont **deux étaient invisibles
aux audits précédents parce que la couche DB était parfaitement correcte —
c'est le câblage conversationnel qui manquait.**

| Gap | Sévérité | Statut |
|---|---|---|
| **P1-3** — `PRODUCER_CONFIRM_DELIVERY_PAYMENT` (F1) : impasse validateur, le résolveur n'était JAMAIS atteint | **P1** | **CORRIGÉ** |
| **P1-4** — retirer un produit du catalogue : `delete_product` complète mais inatteignable | **P1** | **CORRIGÉ** |
| **P2-1** — produit archivé ressuscitable par une simple mise à jour de quantité | **P2** | **CORRIGÉ** |
| **P1-2** — rejet/annulation producteur d'une commande `CONFIRMED` | **P1** | **NEEDS PRODUCT DECISION** (non codé) |
| **P2-2** — `SALES_ACCEPT_CONTRACT`/`SYSTEM_COMMIT_TRANSACTION` → `commit_staged_transaction` inexistant | **P2** | documenté, non corrigé |
| **P3-1** — `SYSTEM_REPORT_ANOMALY` → `report_anomaly` inexistant | **P3** | documenté |
| **P3-2** — `update_production_visibility`, `OrderService`, `DeliveryMixin`, `product_service.py` : code mort | **P3** | documenté |

Le constat central de cette phase : **le moteur transactionnel est solide,
mais une capacité peut être invisible à un audit DB tout en étant
inaccessible à l'utilisateur.** Les deux P1 corrigés ici ont exactement
cette forme — dont un introduit par notre propre chantier F1.

## 2. Capability map

Voir [PRODUCT_CAPABILITY_MAP_2026-09-04.md](PRODUCT_CAPABILITY_MAP_2026-09-04.md) :
38 COMPLETE · 3 familles BROKEN · 6 DEAD · 2 MISSING.

## 3. Buyer capabilities

Toutes COMPLETE (recherche → paliers → panier → checkout → confirmation →
suivi → annulation → historique ; RFQ → offres → négociation → sélection du
gagnant → clôture). Seule exception : **recevoir une annulation producteur**,
qui n'existe pas parce que l'action producteur correspondante n'existe pas
(P1-2).

## 4. Producer capabilities

COMPLETE sur toute la chaîne de vente (publication, édition, **retrait**,
production future, offres, mise à jour/retrait d'offre, notification
gain/perte, suivi des commandes des 3 origines, clôture livraison+paiement,
vente directe cash, escrow OTP).

Restent hors service : `SALES_ACCEPT_CONTRACT` (P2-2), les `STOCK_*`
(déjà déprioritisés), `SYSTEM_REPORT_ANOMALY` (P3-1) — et manquent :
accepter/refuser une commande (P1-2).

## 5. Order lifecycle

```
                       ┌────────────── buyer cancel (P1-1, Phase 1) ─────────────┐
                       │                                                          ▼
create_preorder_draft  │   confirm_preorder_draft        confirm_delivery_and_payment
      ──────────► DRAFT ──────────────────► CONFIRMED ─────────────────────► COMPLETED
                   │                            │                    (payment=PAID, delivery=DELIVERED)
                   │ cancel_preorder_draft      │  verify_delivery_otp (escrow uniquement)
                   ▼                            └──────────────────────────► PAID_OUT
               CANCELLED / SUPERSEDED            
                                            select_winning_bid
                        Auction(OPEN) ──────────────────────► Order(CONFIRMED) ──► (idem ci-dessus)

                        record_sale ──────────────────────────────────► COMPLETED (direct)
```

| Transition | Acteur | Intent | Handler | Mutation | Notification | Dashboard |
|---|---|---|---|---|---|---|
| ∅ → `DRAFT` | buyer | `BUYER_PREORDER_INIT` | `create_preorder_draft` | `Order(DRAFT)` + CAS | — | exclu |
| `DRAFT` → `CONFIRMED` | buyer | `BUYER_PREORDER_CONFIRM` | `confirm_preorder_draft` | débit stock | `PREORDER_CONFIRMED_PRODUCER` | oui |
| `DRAFT` → `CANCELLED`/`SUPERSEDED` | buyer | `_cancel_preorder`/ADD_MORE | `cancel_preorder_draft` | — | — | exclu |
| ∅ → `CONFIRMED` (RFQ) | buyer | winner selection | `select_winning_bid` | `Auction→CLOSED`, `Bid→WINNING/LOST` | `AUCTION_WON/LOST_PRODUCER` | oui |
| `CONFIRMED` → `CANCELLED` | **buyer** | `BUYER_CANCEL_ORDER` | `cancel_pending_order` | recrédit stock, limite anti-abus | `ORDER_CANCELLED_BY_BUYER_PRODUCER` | oui |
| `CONFIRMED` → `CANCELLED` | **producer** | — | — | — | — | — → **MISSING (P1-2)** |
| `CONFIRMED` → `COMPLETED` | producer | `PRODUCER_CONFIRM_DELIVERY_PAYMENT` | `confirm_delivery_and_payment` | `PAID`/`DELIVERED` + 3 `OrderStatusHistory` | `ORDER_COMPLETED_AT_DELIVERY_BUYER` | oui |
| `ESCROWED` → `PAID_OUT` | producer | `PRODUCER_CONFIRM_DELIVERY_OTP` | `verify_delivery_otp` | — | — | oui |
| ∅ → `COMPLETED` | producer | `SALES_RECORD_DIRECT` | `record_sale` | — | — | oui |

**Asymétrie unique restante** : `CONFIRMED → CANCELLED` existe côté buyer,
pas côté producer. Aucun statut n'est créable sans acteur, et aucun statut
n'est terminal-sans-sortie **pour l'acheteur** ; pour le producteur,
`CONFIRMED` n'a qu'une seule sortie (livrer), jamais « je ne peux pas
honorer ».

## 6. Cancellation lifecycle

| Action | Initiateur | État initial | État final | Notification | Supported |
|---|---|---|---|---|---|
| Abandon panier | buyer | `active_cart` | purgé au changement de but | — | ✅ (rien en DB) |
| Annuler un brouillon | buyer | `DRAFT` | `CANCELLED` | — | ✅ |
| Remplacer un brouillon (ADD_MORE) | buyer | `DRAFT` | `SUPERSEDED` | — | ✅ |
| Annuler une commande confirmée | buyer | `PENDING`/`CONFIRMED` | `CANCELLED` | producteur notifié | ✅ (Phase 1) |
| **Refuser/annuler une commande confirmée** | **producer** | `CONFIRMED` | — | — | ❌ **P1-2** |
| Annuler un appel d'offres | buyer | `OPEN` | `CANCELLED` | — | ✅ |
| Retirer une offre | producer | `PENDING` | `WITHDRAWN` | — | ✅ |
| Expiration automatique d'enchère | système | `OPEN` | — | — | ❌ (aucun cron d'expiration ; `deadline` est déclaratif) — **P3** |
| Réconciliation des brouillons figés | système | `EXECUTING`/`AWAITING_PAYMENT` | résolu | — | ✅ (`PreorderReconciliationService`) |

Aucune « politique d'annulation » générique n'a été créée : chaque ligne
réutilise le mécanisme déjà en place pour son entité.

## 7. Product / catalog lifecycle

| Question du mandat | Réponse vérifiée |
|---|---|
| Un producteur peut-il modifier un produit publié ? | Oui — `SALES_UPDATE_PRODUCT`, verrou FOR UPDATE, propriété vérifiée. |
| Un changement de prix est-il visible des prochains acheteurs ? | Oui — la recherche lit `Product` en direct, aucun cache. |
| Les pricing tiers restent-ils cohérents après modification ? | Oui — `validate_pricing_tiers` revalide contre l'unité **finale** (nouvelle si changée dans la même mise à jour). |
| Un produit peut-il être rendu indisponible ? | **Maintenant oui** — `SALES_UNPUBLISH_PRODUCT` (P1-4). C'était impossible avant cette phase. |
| Un produit retiré continue-t-il d'apparaître ? | **Non, maintenant** — exclu du catalogue producteur ET de la recherche acheteur (P2-1). |
| Un ancien produit peut-il encore être commandé ? | Non — `is_available=False` exclut désormais de la recherche, et `delete_product` refuse le retrait tant que des commandes sont actives. |
| Le minimum order est-il appliqué au checkout après modification ? | Oui — revalidé à chaque checkout depuis `SubCategory` (politique plateforme), jamais figé dans le panier. |
| `is_available` est-il réellement exploité ? | **Maintenant oui** (avant : écrit par `delete_product`/`record_sale`, jamais lu par la recherche). |
| Produits orphelins / non administrables ? | Les produits fantômes créés par `record_sale` (`quantity=0`, `is_available=False`) : correctement invisibles, volontairement conservés pour l'historique des ventes. |

## 8. Conversation recovery

`pending_interaction` (DURABLE, `core/pending_interaction.py`, persisté par
le checkpointer) reste le discriminant canonique unique. Scénarios revus :
retour après une heure (reprise), correction de quantité (rejouée sur le
même panier), nouvelle commande pendant une sélection (breakout explicite),
double confirmation (idempotence naturelle : le 2ᵉ appel échoue sur le garde
de statut), notification producteur traitée des heures plus tard (Outbox,
pas d'état conversationnel requis), GPS manquant / état ancien (revalidation
avant exécution, chantier winner-selection). **Aucun nouveau cas où un état
ancien gagne silencieusement.** Le refactor state-management n'est pas
rouvert.

## 9. Notifications (event coverage matrix)

| Business event | Qui doit savoir | Quand | Exactement une fois | Outbox | Contenu | Téléphone absent |
|---|---|---|---|---|---|---|
| Order confirmed (préorder) | producteur(s) | à la confirmation | ✅ transition non répétable | ✅ | jamais « paiement reçu » | ignoré, n'échoue pas |
| Order cancelled (par le buyer) | producteur(s) | à l'annulation | ✅ `dedupe_key` + garde de statut | ✅ | « annulée avant livraison » | idem |
| Order cancelled (par le producteur) | acheteur | — | — | — | — | **MISSING (P1-2)** |
| Bid placed | acheteur | — | ❌ pas de notification | — | — | **P3** (visible en tirant le dashboard) |
| Bid updated | acheteur | — | ❌ | — | — | **P3** |
| Winner selected | producteur gagnant | à la sélection | ✅ | ✅ | contact acheteur | idem |
| Bid lost | producteurs perdants | même transition | ✅ dérivé du même UPDATE `.returning()` | ✅ | jamais aux `WITHDRAWN` | idem |
| Delivery + payment completed | acheteur | à la clôture | ✅ idempotent (`ALREADY_COMPLETED`) | ✅ | montant réellement encaissé | idem |
| Escrow payé / échoué / expiré | acheteur + producteur | IPN | ✅ | ✅ | — | idem |

## 10. Dashboards

| Vue | Contenu | Vérifié |
|---|---|---|
| Buyer — commandes actives/terminées/annulées | exclut `DRAFT`/`SUPERSEDED` ; `CANCELLED` et `COMPLETED` correctement libellés | ✅ |
| Buyer — commandes RFQ **et** directes | même requête (`Order.buyer_id`), aucune dépendance à `OrderItem` | ✅ |
| Buyer — enchères | `BUYER_LIST_AUCTIONS`, tous statuts | ✅ |
| Producer — commandes entrantes | exclut `DRAFT`/`SUPERSEDED` | ✅ |
| Producer — commandes issues d'enchères gagnées | incluses depuis le chantier winner-lifecycle (`Bid.producer_id`) | ✅ |
| Producer — offres perdues | `MARKET_GET_MY_PROPOSALS` + notification `AUCTION_LOST_PRODUCER` | ✅ |
| Producer — produits actifs | `get_my_products` renvoie TOUT (archivés + fantômes de vente directe) ; filtré côté conversation là où c'est nécessaire | ⚠️ **P3** : une vue « catalogue actif » dédiée serait plus lisible, mais aucun impact transactionnel démontré |

Aucun ghost order, aucune commande active invisible, aucune commande
terminée mal catégorisée.

## 11. P0

**Aucun.** Tous les journeys principaux atteignent un état terminal.

## 12. P1

### P1-3 — `PRODUCER_CONFIRM_DELIVERY_PAYMENT` : le résolveur n'était jamais atteint — **CORRIGÉ**

**Capability** : producteur — confirmer livraison + paiement à la livraison.
**Severity** : P1 (niveau 1 : empêchait un journey principal de terminer).

**Current behavior (avant)** : le producteur écrit « j'ai livré, j'ai été
payé ». Le validateur voit `required=["order_id"]` manquant → `missing_fields`
non vide → `DomainRouter.decide` ne trouve aucune règle (le goal n'est pas
dans `_TUNNEL_ASSIGNMENTS`, donc dans aucun `*_GOALS`) → retombe sur
`make_route_after_validator`, dont l'étape 2 renvoie `to_strategy` dès que
`missing_fields` est non vide. **`context_resolver` n'est jamais atteint**,
donc `_resolve_order_for_delivery_payment` non plus : le producteur se voit
réclamer « le numéro de la commande », c'est-à-dire un UUID qu'il ne peut
pas connaître.

**Expected behavior** : le validateur laisse passer, le résolveur propose la
liste des commandes livrables (ou auto-sélectionne s'il n'y en a qu'une).

**Root cause** : le chantier F1 a ajouté le goal + le résolveur, mais pas
l'entrée `_RESOLVER_PASSTHROUGH` — le mécanisme qui existe précisément pour
ça, et dont le commentaire en place décrit déjà cette impasse mot pour mot
(« le validateur bloquait donc AVANT, en réclamant `auction_id`/`bid_id` — un
UUID que l'utilisateur ne peut pas connaître […] Impasse conversationnelle »).
Les tests F1 validaient le résolveur **en l'appelant directement**, jamais la
chaîne validateur → routeur → résolveur : le bug était invisible.

**Fichiers/fonctions** : [validation.py](../src/ladini/graphs/agents/market_coach/nodes/validation.py) (`_RESOLVER_PASSTHROUGH`).

**Business impact** : aucune commande RFQ ni préorder non-escrow ne pouvait
être clôturée conversationnellement — c'est-à-dire que le gap E1, censé
fermé par F1, restait ouvert en pratique.

**Fixable sans décision produit ?** Oui.
**Correction minimale** : `"PRODUCER_CONFIRM_DELIVERY_PAYMENT": ("order_id", [])`.
**Test de non-régression** : `tests/nodes/test_nodes_behaviour.py::TestValidatorNeverAsksForIds::test_goals_with_resolver_pass_through_to_it` (paramétré, couvre aussi le nouveau goal).

### P1-4 — Retirer un produit du catalogue : capacité inatteignable — **CORRIGÉ**

**Capability** : producteur — retirer/dépublier un produit.
**Severity** : P1 (niveau 2 : empêchait un acteur de gérer un objet déjà créé).

**Current behavior (avant)** : impossible. `delete_product` existait,
complète et sûre (verrou FOR UPDATE, propriété vérifiée, **refus si
commandes actives**, archivage doux si historique de commandes, suppression
physique sinon) — mais la chaîne `"delete_product"` n'apparaissait nulle
part ailleurs dans le dépôt : aucun intent, aucun tunnel, aucun handler,
aucune entrée de scope MCP. Un producteur ne pouvait jamais retirer un
produit épuisé ou erroné, que les acheteurs continuaient de voir.

**Root cause** : couche DB complète, câblage conversationnel absent — le
type de gap qu'un audit « la fonction existe donc la capacité existe »
manque systématiquement.

**Fichiers/fonctions** : [intent.py](../src/ladini/graphs/agents/market_coach/interpreter/intent.py) (`SALES_UNPUBLISH_PRODUCT`), [sales.py](../src/ladini/graphs/agents/market_coach/actions/sales.py) (`prep_sales_unpublish_product`), [flow.py](../src/ladini/graphs/agents/market_coach/flows/producer/flow.py) (`_resolve_product_for_unpublish`), [gateway.py](../src/ladini/graphs/agents/market_coach/services/mcp/gateway.py) (`ProductGateway.delete_product`), [security.py](../src/ladini/infrastructure/mcp/security.py), [validation.py](../src/ladini/graphs/agents/market_coach/nodes/validation.py).

**Fixable sans décision produit ?** Oui — toute la politique métier était
déjà encodée dans `delete_product` ; rien n'a été redécidé, rien n'a été
réimplémenté côté agent (test structurel à l'appui).

**Correction minimale** : nouveau goal + handler + résolveur de sélection
(auto-sélection si un seul produit, menu numéroté sinon — jamais de choix
implicite sur une action destructrice), confirmation assurée par le
`confirmation_gate` générique.

**Test de non-régression** : [test_product_unpublish_capability.py](../tests/nodes/test_product_unpublish_capability.py) (15 tests).

### P1-2 — Rejet/annulation producteur d'une commande `CONFIRMED` — **NEEDS PRODUCT DECISION**

Ré-analysé en profondeur cette phase, conformément au mandat §4. **Le dépôt
ne contient PAS assez de règles pour trancher** : le seul précédent
d'annulation (`cancel_pending_order`) est explicitement buyer-only
(`cancellation_role="BUYER"`, compteur anti-abus filtré sur `Order.buyer_id`),
et aucune règle producteur (pénalité, réputation, compensation) n'existe
nulle part. Décider seul reviendrait à inventer la politique métier.

Réponses **partielles** que le code permet déjà d'établir :

| Question | Ce que le code impose déjà | Ce qui reste à décider |
|---|---|---|
| Qui peut initier ? | techniquement : le producteur propriétaire, résolu comme dans `confirm_delivery_and_payment` (via `OrderItem→Product.producer_id` OU `Order.winning_bid_id→Bid.producer_id`) | doit-il pouvoir le faire **unilatéralement**, ou seulement demander l'accord de l'acheteur ? |
| Quels statuts annulables ? | `CONFIRMED` uniquement (jamais `COMPLETED`/`CANCELLED`, et `DRAFT` n'a pas de producteur engagé) | faut-il un délai limite (ex. avant date de livraison prévue) ? |
| Que devient la commande ? | `CANCELLED` (statut existant, aucun nouveau statut nécessaire), `cancellation_role="PRODUCER"` (colonne déjà présente) | — |
| Que devient le stock ? | recrédit symétrique du buyer-side via `resolve_stock_debit` pour les commandes préorder ; **aucun effet** pour les RFQ (jamais d'`OrderItem`) | le produit doit-il être automatiquement dépublié après un refus pour rupture ? |
| Que devient le paiement ? | rien à rembourser (paiement à la livraison, jamais encaissé avant) | l'escrow, s'il est réactivé un jour, exigerait un remboursement — hors périmètre actuel |
| Notifications ? | un template acheteur, même motif Outbox que les 12 existants | le message doit-il proposer une alternative (relancer un RFQ, autre producteur) ? |
| Anti-abus ? | `MAX_CANCELLATIONS=3` existe mais est câblé buyer-only | seuil producteur identique ? conséquence identique (blocage de compte) ou dégradation de visibilité ? |
| Historique / dashboard | `OrderStatusHistory` + `CANCELLED` déjà correctement affiché des deux côtés | — |

**Options explicites** (aucune n'est retenue ici) :
1. **Symétrie stricte** — le producteur annule comme l'acheteur, mêmes règles, même compteur anti-abus. *Simple, cohérent ; risque : annulations unilatérales fréquentes côté offre.*
2. **Demande d'annulation** — le producteur signale, l'acheteur confirme ; sans réponse sous N jours, annulation automatique. *Protège l'acheteur ; ajoute un état intermédiaire et un cron.*
3. **Refus au moment de la notification seulement** — fenêtre courte après réception de la commande, au-delà le producteur doit livrer. *Proche des marketplaces réelles ; demande une notion de délai qui n'existe pas encore.*

**Recommandation de méthode** (pas de contenu) : trancher par une question
produit explicite, comme cela a été fait pour F1, **avant** toute ligne de
code.

## 13. P2

- **P2-1 — Produit archivé ressuscitable — CORRIGÉ.** La recherche acheteur ne filtrait que `quantity_for_sale > 0`, jamais `is_available`. Un produit archivé (ou un produit fantôme créé par `record_sale`) redevenait donc visible dès qu'une quantité était remise. Corrigé dans [buyer.py](../src/ladini/services/database/buyer.py) (`search_products`) + filtrage des archivés dans le menu de retrait. Devenu réellement atteignable **par** P1-4, d'où sa correction immédiate.
- **P2-2 — `SALES_ACCEPT_CONTRACT` / `SYSTEM_COMMIT_TRANSACTION` : BROKEN.** Les deux goals résolvent `commit_staged_transaction`, un `tool_name` **sans aucune méthode DB** (vérifié sur tout `src/`, y compris `ToolResolver` qui n'a aucun override en production). `SalesService.accept_contract` retourne ce `tool_id` inconditionnellement. Reachability faible (`bid_id`/`staging_id` = UUID rarement fournis spontanément), échec propre via le catch-all de l'exécuteur. **Non corrigé** : ce que « valider définitivement un contrat verrouillé » doit faire n'est pas déterminable depuis le code — même famille de décision que P1-2, et le mandat interdit d'inventer.

## 14. P3

- **P3-1** — `SYSTEM_REPORT_ANOMALY` → `report_anomaly` : aucune méthode DB (même diagnostic que P2-2, fonction périphérique).
- **P3-2** — Code mort confirmé : `update_production_visibility` (publier/dépublier une production future — complète, aucun intent), `OrderService`, `DeliveryMixin`, `product_service.py`. Rien supprimé : aucune nécessité démontrée, et `update_production_visibility` est un candidat naturel au câblage si le besoin produit apparaît (même forme exacte que P1-4).
- **P3-3** — Aucune notification sur `bid placed`/`bid updated` (l'acheteur doit consulter son tableau de bord).
- **P3-4** — Aucune expiration automatique d'enchère (`deadline` déclaratif, pas de cron).
- **P3-5** — `get_my_products` mélange produits actifs, archivés et fantômes de vente directe ; filtré côté conversation là où c'est nécessaire.

## 15. Needs product decision

| Sujet | Classification |
|---|---|
| Rejet/annulation producteur d'une commande `CONFIRMED` (P1-2) | **NEEDS PRODUCT DECISION** |
| Sémantique réelle de `SALES_ACCEPT_CONTRACT` (P2-2) | **NEEDS PRODUCT DECISION** |
| Remboursement, exécution partielle, remplacement, report de livraison, réservation de stock, litige de paiement | **OUT OF CURRENT PRODUCT SCOPE** (paiement à la livraison, aucun encaissement préalable) |
| Notifications `bid placed`/`bid updated`, expiration automatique d'enchère | **IMPLEMENTABLE FROM EXISTING BUSINESS RULES**, mais P3 — aucun blocage démontré |

## 16. Implemented fixes

1. **`_RESOLVER_PASSTHROUGH`** : `PRODUCER_CONFIRM_DELIVERY_PAYMENT` + `SALES_UNPUBLISH_PRODUCT` (P1-3).
2. **Capacité « retirer un produit »** (P1-4) : intent `SALES_UNPUBLISH_PRODUCT`, rôle PRODUCER, `prep_sales_unpublish_product`, `_resolve_product_for_unpublish`, `ProductGateway.delete_product`, scope MCP `delete_product`. Aucune règle métier réimplémentée.
3. **Cohérence catalogue** (P2-1) : `Product.is_available.is_(True)` dans la recherche acheteur ; produits archivés exclus du menu de retrait.

Aucun refactor, aucune nouvelle abstraction, aucun nouveau statut, aucun
mécanisme générique d'annulation. Rien n'a été touché dans Procurement,
Preorder, Auction locking, F1 (hors correction de son propre routage), F2/F3
Outbox, F4 anti-bypass, LLM Gateway, PendingInteraction, ResponsePlan.

## 17. Tests added

| Fichier | Ce qu'il verrouille |
|---|---|
| [test_product_unpublish_capability.py](../tests/nodes/test_product_unpublish_capability.py) (15) | câblage complet de la capacité (intent/rôle/action/gateway/scope), passthrough validateur, résolveur (auto-sélection, menu, `selection_index`, hors-bornes, catalogue vide), non-résurrection des archivés, non-duplication de la règle métier |
| `test_nodes_behaviour.py::test_goals_with_resolver_pass_through_to_it` (étendu) | les deux goals à résolveur ne réclament plus jamais d'UUID |

Tests E2E métier déjà en place et re-vérifiés (Phase 1) : catalogue →
COMPLETED, préorder → COMPLETED, RFQ → COMPLETED, annulation → terminal,
dashboard producteur 3 origines, rejeu sans double mutation ni double
notification.

## 18. Remaining gaps

1. **P1-2** (décision produit) — rejet/annulation producteur.
2. **P2-2 / P3-1** — `commit_staged_transaction`, `report_anomaly` : `tool_name` sans implémentation.
3. **STOCK_\*** — inchangés, déprioritisés par décision antérieure.
4. **P3** — notifications d'offres, expiration d'enchères, vue « catalogue actif », code mort.

## 19. What is now CLOSED

- Clôture livraison + paiement (F1) — **réellement** atteignable depuis la conversation, ce qui n'était pas le cas avant cette phase.
- Cycle de vie catalogue producteur : créer → modifier → **retirer**, sans résurrection possible.
- Annulation acheteur sur toute la chaîne (panier, brouillon, préorder, commande confirmée, enchère).
- Notifications de toutes les transitions métier réelles sauf celle qui dépend de P1-2.
- Dashboards des deux côtés : aucune commande fantôme, aucune commande active invisible, les 3 origines visibles côté producteur.
- Anti-bypass winner-selection (F4) : re-vérifié intact.

---

# PRODUCT STATUS

```
COMPLETE:
  Buyer   : recherche, paliers, panier, checkout, confirmation, suivi,
            historique, annulation (panier/brouillon/commande/enchère),
            RFQ, offres, négociation, sélection du gagnant, réception
            de la clôture livraison+paiement, reprise conversationnelle
  Producer: publication, édition, RETRAIT de produit, production future,
            offres (poser/mettre à jour/retirer), notification gain/perte,
            suivi des commandes (catalogue + préorder + RFQ), clôture
            livraison+paiement (cash et escrow), vente directe

FIXED (cette phase):
  P1-3  impasse validateur sur PRODUCER_CONFIRM_DELIVERY_PAYMENT (F1)
  P1-4  capacité « retirer un produit du catalogue » inatteignable
  P2-1  produit archivé ressuscitable via la recherche acheteur

P0:
  aucun

P1:
  P1-2  rejet/annulation producteur d'une commande CONFIRMED
        -> décision produit requise, non codé

P2:
  P2-2  SALES_ACCEPT_CONTRACT / SYSTEM_COMMIT_TRANSACTION
        (commit_staged_transaction sans implémentation)

P3:
  report_anomaly sans implémentation ; STOCK_* (déjà déprioritisés) ;
  notifications bid placed/updated ; expiration automatique d'enchères ;
  code mort (update_production_visibility, OrderService, DeliveryMixin,
  product_service.py) ; vue « catalogue actif » producteur

PRODUCT DECISIONS REQUIRED:
  1. Le producteur peut-il annuler/refuser une commande confirmée ?
     (3 options documentées §12 — symétrie stricte / demande soumise à
     l'acheteur / fenêtre de refus courte)
  2. Que doit faire « SALES_ACCEPT_CONTRACT » ?

OUT OF SCOPE:
  remboursement, exécution partielle, remplacement, report de livraison,
  réservation de stock, litige de paiement, paiement en ligne (escrow
  implémenté mais désactivé : PAIEMENT = À LA LIVRAISON)

READY FOR FRONTEND / FIELD TEST:
  les deux parcours transactionnels complets (catalogue/préorder et RFQ),
  de la recherche jusqu'à COMPLETED, avec notifications aux deux parties,
  annulation acheteur, et cycle de vie catalogue producteur

KNOWN HISTORICAL TEST FAILURES (inchangées, sans rapport):
  tests/unit/test_create_auction_catalog_gate.py::
    TestCreateAuctionCatalogResolution::
      test_no_candidate_at_all_gets_auto_provisioned_not_rejected
      test_a_fuzzy_matched_but_unconfident_candidate_falls_back_to_auto_provisioning
      test_a_confidently_matched_candidate_is_used_as_is
      test_the_second_stage_fuzzy_matcher_is_also_confidence_gated
  (bug de date codée en dur, antérieur, jamais touché)
```
