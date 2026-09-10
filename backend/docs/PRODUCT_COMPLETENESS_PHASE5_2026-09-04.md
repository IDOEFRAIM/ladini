# PRODUCT COMPLETENESS — PHASE 5 (2026-09-04)

## 1. Executive summary

Les 4 décisions produit ouvertes sont **fermées** : 2 par implémentation ou
dépréciation, 1 par défaut documenté, 1 qui reste explicitement un
arbitrage humain. Le registre complet est dans
[PRODUCT_DECISION_REGISTER_2026-09-04.md](PRODUCT_DECISION_REGISTER_2026-09-04.md).

| Décision | Issue |
|---|---|
| #1 Annulation/rejet producteur d'une commande `CONFIRMED` | **FERMÉE — implémentée** (symétrie stricte du chemin acheteur) |
| #2 `SALES_ACCEPT_CONTRACT` / sémantique du « contrat » | **OUVERTE — PRODUCT_DECISION** (aucun code) |
| #3 `PROFILE_SWITCH_ROLE` | **FERMÉE — dépréciée** (le modèle double-rôle rend la bascule inutile) |
| #4 Exposition du ledger STOCK | **FERMÉE par défaut** (`Product.quantity_for_sale` est la seule autorité de vente) |

L'audit ciblé qui a suivi a produit **un nouveau P1 démontré** : sur une
commande multi-producteurs, n'importe lequel des producteurs peut
terminaliser la commande **entière** (clôture ou annulation), y compris la
part de l'autre. Non corrigé — toute correction exige un changement de
modèle ou de flux, ce que cette phase interdit sans décision produit (§7).

## 2. Decisions closed

**3 fermées / 1 restante** — détail en §3 à §6.

## 3. Producer cancellation decision (#1) — IMPLÉMENTÉE

**État avant** : `CONFIRMED` était le seul état du produit sans sortie côté
producteur. Ne pouvant ni livrer (rupture, aléa) ni se rétracter, il devait
demander à l'acheteur d'annuler — contournement hors-app — ou laisser la
commande figée indéfiniment.

**Ce que le dépôt déterminait déjà** (donc non arbitré par moi) : statut
cible `CANCELLED`, seul `CONFIRMED` annulable, aucun remboursement
(paiement à la livraison), recrédit via `resolve_stock_debit`, propriété
dual-origine, `Order.cancellation_role` (colonne existante), motif libre
journalisé, notification miroir.

**Ce qui restait un vrai choix — et pourquoi (a) l'emporte** :

* **(a) symétrie stricte** — retenue : n'exige aucun statut, aucun cron, aucune table.
* **(b) demande soumise à l'accord de l'acheteur** — écartée : exigerait un état `CANCELLATION_REQUESTED` et un délai automatique. Le mandat interdit de créer un statut sans nécessité (§5) et d'ouvrir un nouveau mécanisme (§19).
* **(c) fenêtre de refus courte** — écartée : exigerait une échéance de livraison opposable, qui n'existe pas dans le modèle `Order`.

**Délibérément NON implémenté** (décisions distinctes, documentées) :
aucune limite anti-abus producteur (`MAX_CANCELLATIONS` reste spécifique à
l'acheteur — inventer une sanction serait arbitraire) ; aucune réouverture
d'enchère après annulation d'une commande RFQ (le chemin acheteur ne la
rouvre pas non plus — la symétrie est préservée).

**Chaîne livrée** (les 8 maillons exigés par les contrats des Phases 3/4) :

```
PRODUCER_CANCEL_ORDER  (intent, rôle PRODUCER)
  → _RESOLVER_PASSTHROUGH("order_id")
  → _resolve_order_for_cancellation   (auto-sélection / menu numéroté strict)
  → confirmation_gate générique
  → prep_producer_cancel_order        (handler enregistré)
  → OrderTrackingGateway.cancel_confirmed_order
  → scope MCP cancel_confirmed_order
  → ProducerMgmtMixin.cancel_confirmed_order
  → Order CANCELLED + stock recrédité + OrderStatusHistory + Outbox acheteur
```

## 4. Contract decision (#2) — RESTE OUVERTE

Recherche exhaustive : **aucune** entité `Contract`/`StagedTransaction`,
aucune table, aucune référence frontend/doc/eval. `SalesService.accept_contract`
retourne `commit_staged_transaction` inconditionnellement — un outil qui
n'existe pas. Les 8 questions métier du mandat restent sans réponse
déterminable.

**`contract ≠ order` n'est pas démontrable non plus** : le dépôt ne permet
pas d'affirmer que le concept contractuel diffère d'`Order`, ni qu'il en
est un ancien nom. Les trois interprétations (doublon d'un mécanisme
existant / contractualisation à terme / reliquat d'un flux « staging »
remplacé par Drafts+CAS) restent équiprobables sur preuves.

Aucun code écrit. Le goal reste déprécié (hors catalogue) depuis la Phase 4.

## 5. Role-switch decision (#3) — FERMÉE, DÉPRÉCIÉE

Le dépôt répond à la question du mandat : **oui**, un utilisateur est déjà
buyer ET producer avec la même identité. La refonte double-rôle est
définitive et documentée dans les fichiers cœur (`graph_builder`, `router`,
`intent`, `routing`) : graphe unifié, aucun rôle de graphe, le rôle est
**implicite par action**, message par message. La sécurité se fait en aval
par épinglage d'identité + propriété en base, jamais par un rôle de session.

`PROFILE_SWITCH_ROLE` écrit une ligne `agent_actions` qu'**aucun code ne
lit** (prouvé indépendamment dans `tests/evals/blocked/PROFILE_SWITCH_ROLE.md`),
et son `create_agent_action` n'existe pas comme outil. Une bascule
explicite n'ajouterait donc rien au modèle actuel.

**DEPRECATE** (déjà effectif depuis la Phase 4). Pas de suppression :
`tests/architecture/test_catalog_and_graph.py` référence encore
`PROFILE_SWITCH_ROLE.target_role`.

## 6. Stock decision (#4) — FERMÉE PAR DÉFAUT

Réponses aux questions du mandat, **sur preuves de code** :

| Question | Réponse |
|---|---|
| `Stock` est-il autoritatif ? | **Non.** Aucune méthode `Stock` n'est appelée par un chemin transactionnel. |
| `Product.quantity_for_sale` est-il autoritatif ? | **Oui.** Débité au checkout (`buyer.py:662`, `2074`), recrédité à l'annulation (`917`), écrit par escrow/création/édition/retrait. |
| Les deux sont-ils maintenus ? | **Non, indépendamment** : `Stock` par `add_stock`/`adjust_stock`/… (jamais depuis une vente) ; `Product.quantity_for_sale` par les transactions. |
| Qui écrit quoi ? | cf. ci-dessus. |
| Lequel conditionne la commande ? | `Product.quantity_for_sale` — le message « Stock insuffisant » côté acheteur lit ce champ, jamais la table `Stock`. |

Le ledger `Stock` est donc **parallèle et non autoritatif**. L'exposer
créerait une capacité sans effet sur ce qui est vendu.

**Décision : ne pas exposer** (règle par défaut du mandat §9, confortée par
la preuve d'autorité). Reste ouvert, si le besoin apparaît : relier le
ledger aux ventes (sémantique de mouvement à définir) ou simplement le
recâbler (4 `tool_name` dérivés `*_by_id`, ~2 lignes chacun).

## 7. Remaining business gaps

### P1 — Terminalisation d'une commande multi-producteurs *(nouveau, non corrigé)*

**Démonstration** : `create_preorder_draft` crée **une seule** `Order` et y
attache tous les `OrderItem` du panier, **sans regroupement par
producteur** — un panier contenant des produits de A et de B produit donc
une commande unique à deux producteurs (cas explicitement supporté :
`confirm_preorder_draft` notifie « chaque producteur distinct »).

Or `confirm_delivery_and_payment` (F1) et `cancel_confirmed_order`
(Phase 5) contrôlent la propriété par « le producteur possède **au moins
un** article », puis écrivent le statut de la commande **entière**.

**Conséquence** : sur une commande A+B, si A confirme livraison et
paiement, la part de B passe `PAID`/`DELIVERED`/`COMPLETED` sans que B ait
agi ni livré. Symétriquement, A peut annuler la part de B.

**Pourquoi ce n'est pas corrigé ici** : les trois corrections possibles
sortent du périmètre autorisé.

| Option | Obstacle |
|---|---|
| Éclater la commande par producteur au checkout | change le modèle de commande — interdit (§12 : « Ne modifie pas le modèle de commande s'il fonctionne déjà ») |
| Statut par ligne | `OrderItem` **n'a aucune colonne de statut** — exige une migration de schéma |
| Refuser la terminalisation d'une commande multi-producteurs | recréerait un état sans sortie — exactement l'anti-pattern éliminé depuis la Phase 1 |

**PRODUCT_DECISION requise** : Ladini doit-il supporter une commande
unique multi-producteurs, ou une commande par producteur ?

### P3 — Montant affiché au producteur sur une commande multi-producteurs

`get_producer_orders` filtre correctement les **lignes** (A ne voit que ses
articles — vérifié) mais expose le `total_amount` de la commande entière,
qui inclut la part de B. Aucune fuite d'identité ni de produit ; imprécision
d'affichage seulement.

## 8. Buyer/producer capability asymmetries

| Capability | Buyer | Producer | Required ? | Status |
|---|---|---|---|---|
| Annuler avant livraison | ✅ `BUYER_CANCEL_ORDER` | ✅ `PRODUCER_CANCEL_ORDER` *(Phase 5)* | oui | **symétrique** |
| Clôturer livraison + paiement | ❌ | ✅ | non — décision F1 assumée (un seul acteur, un seul geste, l'échange est simultané) | asymétrie **volontaire** |
| Suivre une commande | ✅ `BUYER_CHECK_ORDER_STATUS` | ✅ `SALES_LIST_ORDERS` | oui | symétrique |
| Historique | ✅ | ✅ (3 origines) | oui | symétrique |
| Être notifié d'une annulation de l'autre partie | ✅ *(Phase 5)* | ✅ *(Phase 1)* | oui | **symétrique** |
| Créer une demande / une offre | ✅ RFQ | ✅ enchère : `SALES_PLACE_BID` | oui | symétrique |
| Se rétracter avant engagement | ✅ `cancel_auction` | ✅ `withdraw_bid` | oui | symétrique |
| Choisir le gagnant | ✅ | ❌ | non — c'est la décision de l'acheteur | asymétrie **volontaire** |
| Retirer son offre du catalogue | n/a | ✅ `SALES_UNPUBLISH_PRODUCT` *(Phase 2)* | oui | n/a |

Aucune asymétrie non intentionnelle ne subsiste.

## 9. Multi-producer validation

| Vérification | Résultat |
|---|---|
| Une commande, plusieurs producteurs | **oui**, par conception (`create_preorder_draft` n'éclate pas) |
| Producteur A ne voit que ses lignes | **PASS** — `get_producer_orders` filtre `relevant_items` sur `product.producer_id` |
| L'acheteur voit la commande complète | **PASS** |
| Notification par producteur, une seule fois | **PASS** (F2, testé) |
| Pas de commande dupliquée | **PASS** — une seule `Order` |
| Pas de ligne manquante | **PASS** |
| Sémantique de complétion correcte | **FAIL — P1**, voir §7 |

## 10. Failure-path validation

| Scénario d'échec | État terminal attendu | Réel | Notification | Reprise |
|---|---|---|---|---|
| Produit indisponible au checkout | article écarté, commande poursuivie | ✅ `unresolved_items` remonté à l'acheteur | message explicite | oui |
| Minimum d'achat non satisfait | article écarté | ✅ revalidé au checkout (jamais figé au panier) | message | oui |
| Palier indisponible | recalcul serveur | ✅ prix résolu au checkout, jamais celui du panier | — | oui |
| Producteur sans téléphone | transaction inchangée | ✅ notification ignorée, jamais un échec | — | oui |
| Producteur annule | `CANCELLED` | ✅ *(Phase 5)* | acheteur notifié | oui |
| Acheteur annule | `CANCELLED` | ✅ | producteur notifié | oui |
| Double confirmation | idempotent | ✅ `ALREADY_COMPLETED` / `ALREADY_CANCELLED` | aucune 2ᵉ notification | oui |
| Clôture après annulation | rejet propre | ✅ `order_not_confirmed` (testé) | — | oui |
| Enchère fermée / offre retirée | exclue de la sélection | ✅ gardes de statut + verrou | perdants notifiés | oui |
| Conversation périmée (GPS, récap gagnant) | revalidation avant exécution | ✅ | re-confirmation demandée | oui |

## 11. Terminal-state matrix

| Entité | États non terminaux | États terminaux | Tous atteignables ? |
|---|---|---|---|
| `Order` | `DRAFT`, `CONFIRMED` | `COMPLETED`, `CANCELLED`, `SUPERSEDED` | **oui** — `DRAFT`→annulation acheteur ou remplacement ; `CONFIRMED`→clôture producteur, annulation acheteur **ou producteur** *(Phase 5 ferme le dernier état sans sortie)* |
| `Auction` | `OPEN` | `CLOSED`, `CANCELLED` | oui — sélection du gagnant ou annulation acheteur (verrouillée) |
| `Bid` | `PENDING`, `WINNING` | `LOST`, `WITHDRAWN` | oui — décision acheteur ou retrait producteur |
| `PreorderDraft` | `PENDING`, `EXECUTING`, `AWAITING_PAYMENT` | `CONFIRMED`, `CANCELLED`, `EXECUTION_UNKNOWN` | oui — + `PreorderReconciliationService` pour les états figés |
| `ProcurementDraft` | `PENDING`, `EXECUTING` | `EXECUTED`, `FAILED` | oui — + service de récupération/réconciliation |
| `SalesPublishDraft` | `PENDING` | `PUBLISHED`, `ABANDONED` | oui |

**Aucun état non terminal sans action de sortie exposée à un acteur réel.**
C'était le cas de `Order.CONFIRMED` côté producteur jusqu'à cette phase.

## 12. Implemented fixes

Une seule capacité, la décision #1 :
[producer.py](../src/ladini/services/database/producer.py) (`cancel_confirmed_order`),
[intent.py](../src/ladini/graphs/agents/market_coach/interpreter/intent.py),
[validation.py](../src/ladini/graphs/agents/market_coach/nodes/validation.py),
[flow.py](../src/ladini/graphs/agents/market_coach/flows/producer/flow.py) (`_resolve_order_for_cancellation`),
[sales.py](../src/ladini/graphs/agents/market_coach/actions/sales.py),
[gateway.py](../src/ladini/graphs/agents/market_coach/services/mcp/gateway.py),
[security.py](../src/ladini/infrastructure/mcp/security.py),
[templates.py](../src/ladini/workers/outbox/templates.py) (`ORDER_CANCELLED_BY_PRODUCER_BUYER`).

Aucun nouveau statut, aucune nouvelle table, aucune abstraction. Aucun des
domaines protégés (Procurement, Preorder, Auction locking, F1/F2/F3/F4,
LLM Gateway, PendingInteraction, ResponsePlan) n'a été modifié.

## 13. Tests

| Fichier | Couverture |
|---|---|
| [test_producer_cancel_confirmed_order.py](../tests/unit/test_producer_cancel_confirmed_order.py) (13) | annulation préorder (stock recrédité, acheteur notifié, historique) ; annulation RFQ (aucun stock touché, propriété via `winning_bid_id`) ; producteur non propriétaire rejeté ; `COMPLETED`/`DRAFT`/`SUPERSEDED` rejetés ; identifiant invalide ; **double annulation idempotente sans 2ᵉ recrédit ni 2ᵉ notification** ; **clôture livraison impossible après annulation** ; verrou `FOR UPDATE` prouvé sur le SQL ; aucun statut de paiement touché ; **aucun compteur anti-abus producteur introduit** |
| [test_resolve_order_for_cancellation.py](../tests/nodes/test_resolve_order_for_cancellation.py) (7) | auto-sélection, menu strict, `selection_index`, hors-bornes, aucune commande annulable, commandes escrow jamais candidates |
| `test_write_capability_reachability.py` (étendu) | `PRODUCER_CANCEL_ORDER` traverse le VRAI chemin `validator → DomainRouter.decide → to_resolver` |
| `test_no_broken_user_goals.py` (étendu) | la capacité doit rester exposée (garde anti-dépréciation) |

## 14. Remaining PRODUCT_DECISION items

1. **Commande multi-producteurs** (§7) — commande unique ou une par producteur ? *(nouveau, P1)*
2. **`SALES_ACCEPT_CONTRACT`** (§4) — que signifie « accepter un contrat » ?
3. **Ledger STOCK** (§6) — le relier aux ventes, ou le laisser hors catalogue ?
4. **Anti-abus producteur** — faut-il une limite d'annulations côté producteur, symétrique de `MAX_CANCELLATIONS` ?

## 15. Final product capability status

```
PRODUCT STATUS — PHASE 5

Core transaction journeys:
  COMPLETE: 4   (achat catalogue/préorder, RFQ/enchère, cycle de vie
                 catalogue producteur, annulation des deux côtés)
  PARTIAL:  1   (commande multi-producteurs : fonctionne jusqu'à la
                 terminalisation, cf. P1)
  BLOCKED:  0

Open P0: 0
Open P1: 1   (terminalisation multi-producteurs — décision produit)
Open P2: 1   (SALES_ACCEPT_CONTRACT, sémantique indéterminée)

Product decisions closed:    3
Product decisions remaining: 4
New capabilities implemented: 1   (PRODUCER_CANCEL_ORDER)
Capabilities deprecated:      0   (les 21 de la Phase 4 restent en l'état)
Broken user-facing goals:     0
Runtime reachability:         PASS
Full regression:              4 échecs, exactement les 4 historiques
Historical failures:          4
```

```
READY:
  - Achat catalogue → préorder → confirmation → livraison + paiement → COMPLETED
  - RFQ → offres → sélection du gagnant → notifications → COMPLETED
  - Annulation à chaque étape, des DEUX côtés, avec notification de l'autre partie
  - Cycle de vie catalogue producteur : créer / modifier / retirer, sans résurrection
  - Dashboards acheteur et producteur (3 origines de commande, lignes scopées)
  - Catalogue d'intents : 39 exposés, 0 faux bouton, 2 contrats d'architecture verts

NOT READY:
  - Commande multi-producteurs : la terminalisation par un producteur
    engage la part de l'autre (P1, correction bloquée par une décision produit)

NEEDS PRODUCT DECISION:
  1. Commande unique multi-producteurs, ou une commande par producteur ?
  2. Que doit faire « accepter un contrat » (SALES_ACCEPT_CONTRACT) ?
  3. Le ledger STOCK doit-il être relié aux ventes et exposé ?
  4. Une limite d'annulations côté producteur est-elle souhaitée ?
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
