# Audit CART → CHECKOUT → PREORDER — 2026-09-04

Suite de
[SALES_POST_PUBLICATION_PRODUCER_JOURNEY_AUDIT_2026-09-04.md](SALES_POST_PUBLICATION_PRODUCER_JOURNEY_AUDIT_2026-09-04.md).
**Aucun `Draft`/version/CAS créé pour CART.** L'audit a trouvé la question
posée par le mandat ("le versioning/CAS résoudrait-il une incohérence
concrète ?") sans réponse positive pour le PANIER lui-même — mais a trouvé
**deux bugs réels, sérieux, déjà présents en production** exactement à la
frontière CHECKOUT, tous deux corrigés chirurgicalement, sans nouvelle
architecture.

---

## Réponse à la question centrale (mandat §5)

> Le panier est-il un état éditable, ou déjà une transaction métier ?

**Un état éditable.** `active_cart` n'a ni identifiant durable, ni
transaction SQL propre, ni notion de "version confirmée" — c'est une
LISTE en mémoire de conversation (state LangGraph), reconstruite/rendue à
chaque tour. La frontière transactionnelle RÉELLE est `checkout`
(`create_preorder_draft`), qui EST déjà une vraie transaction SQL, protégée
par les mécanismes adéquats (détaillés section C). **Aucune incohérence
concrète que CAS/version résoudrait n'a été trouvée pour le panier
lui-même** — les 2 bugs réels trouvés (sections D/K) ne sont PAS des bugs
de concurrence/versioning, ce sont des bugs de **calcul incorrect côté
serveur au moment du checkout**, corrigés à la source.

---

## A. Parcours réel CART

```
Recherche produit (SEARCH_PRODUCTS / cart_management)
    ↓
resolve_product_vendors (ProductGateway.search_products, LECTURE catalogue)
    ↓
[si palier] menu de paliers (tier_selection_context) → résolution palier
    ↓
Quantité (nombre de paquets si palier, sinon quantité en unité de base)
    ↓
add_to_cart_with_ref :
    - validate_stock_availability (LECTURE, informative — PAS une réservation)
    - validate_minimum_order_quantity (domain/order_policy.py)
    - construit la LIGNE panier (snapshot prix/palier au moment de l'ajout)
    ↓
active_cart (state LangGraph — AUCUNE table SQL dédiée)
    ↓
"précommander" (détecté par detect_cart_action)
    ↓
CHECKOUT — bootstrap_preorder_draft → PreorderGateway.create_draft
    → services/database/buyer.py::create_preorder_draft (TRANSACTION SQL RÉELLE)
        - relit Product.price/Product.pricing_tiers EN BASE (FRAIS, jamais le panier)
        - crée Order(status=DRAFT) + OrderItem(s)
    ↓
PreorderDraft (canonique, versionné, CAS — déjà migré, section C du rapport précédent)
    ↓
Confirmation acheteur (ConfirmationTarget, déjà en place)
    ↓
confirm_preorder_draft : SELECT FOR UPDATE sur Order + chaque Product,
    débit stock atomique, Order.status → CONFIRMED
    ↓
[si escrow] paiement Paydunya (déjà audité, phase PREORDER précédente)
```

---

## B. Source de vérité

| Concept | Ce qu'il EST réellement | Classification |
|---|---|---|
| `active_cart` | Liste en mémoire de conversation (state LangGraph, persistée par le checkpointer) — snapshot prix/palier figé au moment de CHAQUE ajout | **CANONICAL** pour la phase panier (rien d'autre ne fait autorité tant que le checkout n'a pas eu lieu) |
| `draft_payload` | Écho d'AFFICHAGE UNIQUEMENT ("✏️ En cours d'ajout : maïs — 50 kg") d'un item PAS ENCORE dans `active_cart` (produit/quantité en cours de saisie, avant résolution vendeur/palier) | **PROJECTION** — jamais lu comme source de calcul, seulement rendu (`format_pending_draft`) |
| `transaction_payload` | Canal générique per-tour (`merge_dict`) — porte `product`/`quantity`/`unit`/`selection_index`/`tier_id` etc. LE TEMPS d'un tour, consommé/vidé explicitement à chaque résolution | **MÉCANISME**, pas une donnée métier durable — sert TOUS les goals du repo, pas spécifique à CART |
| `Order(status=DRAFT)` + `OrderItem` (PostgreSQL) | Le VRAI panier une fois validé au checkout — prix/palier RE-RÉSOLUS serveur | **CANONICAL** à partir du checkout |
| `PreorderDraft` (déjà migré) | Projection VERSIONNÉE/CAS de CET `Order` DRAFT, côté conversationnel | **CANONICAL** pour le cycle confirmation post-checkout (inchangé, migration précédente) |

**Réponse explicite à la question du mandat** ("avons-nous `Cart` +
`active_cart` + `draft_payload` qui représentent la MÊME chose ?") :
**NON.** Il n'existe PAS de `Cart`/`CartItem` en base — la vérification a
confirmé qu'AUCUNE table SQL de ce nom n'existe (`domain/orders/models.py`,
`domain/catalog/models.py` audités intégralement, section B du rapport
précédent). `active_cart`/`draft_payload`/`transaction_payload` jouent 3
RÔLES DIFFÉRENTS (contenu confirmé / écho d'affichage transitoire /
canal de tour), pas 3 copies d'une même vérité — **aucune suppression
n'est justifiée** (mandat §19 : "chaque suppression doit avoir une preuve
ancien writer → nouveau writer → tous les readers migrés" — cette preuve
n'existe pas ici parce qu'il n'y a pas de duplication réelle).

---

## C. Checkout boundary — réponses aux 10 questions obligatoires

| # | Question | Réponse |
|---|---|---|
| 1 | Le checkout lit-il le panier une seule fois ? | Oui — `create_preorder`/`preorder.py` lit `state["active_cart"]` une fois par tour, transmis tel quel à `bootstrap_preorder_draft`. |
| 2 | Le panier peut-il changer PENDANT le checkout ? | Le panier LOCAL (state) ne change pas pendant l'appel MCP (synchrone, un seul tour) — mais le CATALOGUE (Product.price/pricing_tiers) PEUT changer entre l'ajout au panier et cet appel : **c'était le bug réel, voir section D**. |
| 3 | Les prix sont-ils relus depuis le catalogue ? | **Oui, côté SERVEUR** (`create_preorder_draft` relit `Product.price` FRAIS) — mais AVANT ce correctif, cette relecture n'était PAS répercutée dans ce que l'acheteur voyait confirmer (voir section D). **Corrigé.** |
| 4 | Le pricing tier est-il revalidé ? | **NON avant ce correctif** — `create_preorder_draft` ignorait `tier_id` et traitait tout article comme un produit à tarif unique. **Corrigé** (section D). |
| 5 | La quantité est-elle revalidée ? | Le nombre de paquets (`quantity`) est transmis tel quel (légitime — c'est un choix acheteur, pas une donnée catalogue) ; la quantité EN UNITÉ DE BASE (`base_unit_quantity`) est maintenant RECALCULÉE serveur via le palier RÉSOLU serveur (jamais transmise brute par le client). |
| 6 | Le stock est-il revalidé ? | Oui, à DEUX endroits : `validate_stock_availability` (informatif, à l'ajout panier) ET `confirm_preorder_draft` (`SELECT...FOR UPDATE` + débit atomique, AUTORITATIF) — c'est le second qui compte, déjà correct. |
| 7 | Le vendeur est-il toujours disponible ? | `create_preorder_draft` vérifie `Product` existe encore (sinon `unresolved_items`) — pas de vérification explicite "vendeur toujours actif" au-delà de ça (jugé suffisant : le produit EST la ressource réservée, pas le vendeur en tant que tel). |
| 8 | Un item supprimé peut-il encore être commandé ? | Non — `product = await session.scalar(select(Product)...)` retourne `None` si supprimé, l'item est placé dans `unresolved_items`, jamais silencieusement inclus. |
| 9 | Deux checkouts concurrents peuvent-ils créer 2 commandes ? | Non — `creation_key(phone, cart_fingerprint)` (idempotency_key MCP, déjà construit lors de la migration PREORDER) protège CE cas — déjà couvert par `TestInvariantJ_CreateRetryNeverCreatesADoubleDraft` (test existant). Vérifié toujours vert (section K). |
| 10 | Quelle est exactement la transaction DB ? | `create_preorder_draft` : UNE transaction (`INSERT Order` + `INSERT OrderItem`×N, FLUSH final) — pas de verrou de ligne nécessaire ici (aucun stock touché, `status=DRAFT`). `confirm_preorder_draft` : UNE transaction, `SELECT Order FOR UPDATE` puis `SELECT Product FOR UPDATE` par item (désormais triés par `product_id`, voir section F) puis débit + `status=CONFIRMED`. |

---

## D. Pricing — le bug réel trouvé et corrigé

### Le gap exact

`services/database/buyer.py::create_preorder_draft` (fonction appelée EN
PREMIER au checkout) ne savait RIEN des paliers de prix — elle calculait
`price_at_sale = product.price` et `line_total = product.price * quantity`
pour **tout** article, y compris un article dont `quantity` est en réalité
un **nombre de paquets** d'un palier (`tier_id` posé par le panier). Pour
"3 bidons de 10L à 5000 FCFA/bidon" (produit de base à 900 FCFA/L) :
- **Facturé (AVANT correctif)** : `900 × 3 = 2700 FCFA` (au lieu de 15000)
- **Stock débité (AVANT correctif)** : `3` unités (au lieu de 30 L)

C'est EXACTEMENT la classe de bug "confusion nombre de paquets / quantité
totale" déjà rencontrée et corrigée côté PANIER
([[pricing-tiers-litre-fastpath-bug-2026-08]]) — mais jamais fermée côté
CHECKOUT, où le chemin d'exécution RÉEL passe.

**Ironie révélatrice** : une AUTRE fonction du même fichier
(`finalize_multi_order`, ligne 488) implémente CETTE MÊME logique
correctement — `resolve_tier`/`compute_line`, `tier_id`, minimum de
commande re-validé — mais **`finalize_multi_order` n'est appelée nulle
part dans le code réel** (grep exhaustif : seulement dans sa propre
définition et `services/database/README.md`). Une implémentation correcte
existait déjà dans le dépôt, jamais branchée sur le chemin réellement
utilisé (`create_preorder_draft`/`confirm_preorder_draft`, via
`PreorderGateway`). Signalé — `finalize_multi_order` reste NON supprimée
(pourrait être un point d'entrée alternatif légitime pour un autre canal,
aucune certitude suffisante pour la retirer dans cet audit).

### Correctif appliqué

`create_preorder_draft` résout désormais le palier SERVEUR
(`resolve_tier`/`compute_line`, les MÊMES fonctions déjà éprouvées côté
panier — `tests/unit/test_pricing_tiers.py` — réutilisées, pas
redupliquées) à partir de `Product.pricing_tiers` FRAIS, jamais du prix
transmis par le client. `OrderItem.tier_id`/`base_unit_quantity`
(colonnes qui EXISTAIENT DÉJÀ, ajoutées 2026-08-30 précisément pour ça,
jamais peuplées jusqu'ici) sont désormais posées — ce qui active, sans
AUCUN changement côté aval, la logique `resolve_stock_debit` déjà correcte
dans `confirm_preorder_draft` (qui lisait déjà `base_unit_quantity` en
priorité, mais ne le trouvait jamais peuplé).

### Représentation finale (mandat §7, exigée explicitement)

Chaque item résolu porte désormais : `pricing_tier_id` (`tier_id`),
`package_count` (`quantity`, nombre de paquets), `base quantity`
(`base_unit_quantity`), `unit` (unité du CONTENU du palier), `effective
price` (`price`, prix PAR PAQUET — jamais par unité de base).

---

## E. Le second bug réel — affichage ≠ exécution au checkout

`bootstrap_preorder_draft` (flows/buyer/preorder_confirmation.py)
construisait `PreorderDraft.items`/`total_amount` à partir de `meta`/
`items_payload` — le panier LOCAL, figé aux instants de chaque ajout —
**au lieu de** la réponse RÉELLE de `create_preorder_draft`, qui contient
désormais (déjà, avant même le fix palier — juste jamais consommée) le
total et les prix RÉELLEMENT écrits en base (`OrderItem.price_at_sale`).

**Scénario concret** : acheteur ajoute des tomates à 250 FCFA/kg →
producteur augmente son prix à 300 FCFA/kg (via `SALES_UPDATE_PRODUCT`,
déjà migré) → acheteur checkoute 10 minutes plus tard → l'écran de
confirmation affichait "TOTAL : 12500 FCFA" (50kg×250, PÉRIMÉ) alors que
`Order.total_amount` venait d'être calculé à 15000 FCFA (50kg×300, le prix
RÉEL). L'acheteur confirmait un montant, la commande en portait un autre —
**exactement** l'invariant que ce chantier entier existe pour garantir
("ce que l'acheteur confirme = ce qui est exécuté").

**Correctif** : `bootstrap_preorder_draft` utilise désormais
`draft_res["items"]`/`draft_res["total_amount"]` (la réponse serveur,
AUTORITATIVE) comme SEULE source pour `PreorderDraft.items`/
`total_amount` — repli sur le panier local UNIQUEMENT si le serveur ne les
renvoie pas (rétro-compatibilité, journalisée). `render_summary()`
(`domain/preorder_draft.py`) a été étendu pour afficher un item à palier
avec le MÊME format que le panier (`N × conditionnement`, quantité totale
explicite) — jamais "Quantité : 3 L" pour 3 bidons.

---

## F. Concurrence — protections actuelles + gap fermé

| Question | Réponse |
|---|---|
| `confirm_preorder_draft` verrouille-t-il `Order` avant lecture ? | Oui, déjà (`SELECT...FOR UPDATE`, préexistant). |
| Verrouille-t-il chaque `Product` avant débit ? | Oui, déjà (`SELECT...FOR UPDATE` par item, préexistant). |
| Ordre de verrouillage déterministe (anti-deadlock) ? | **NON avant ce correctif** — les items étaient parcourus dans l'ordre `order.items` (non garanti stable/comparable entre 2 transactions concurrentes portant les MÊMES produits). `finalize_multi_order` (section D) démontrait DÉJÀ le motif correct (tri par `product_id` avant tout verrou) — jamais appliqué ici. **Corrigé** : `sorted(order.items or [], key=lambda it: str(it.product_id))` ajouté, aucune nouvelle primitive. |
| UPDATE quantité + CHECKOUT concurrent (mandat §8/§10) | Le panier (`active_cart`) n'a pas de verrou — mais AUCUNE conséquence financière n'est engagée avant le checkout (le stock check à l'ajout est purement informatif). Le checkout LIT le panier une fois (question C.1) — un "UPDATE" concurrent sur le MÊME state de conversation n'est de toute façon pas physiquement possible (un seul tour actif à la fois par conversation, garanti par le graphe LangGraph lui-même, pas par ce chantier). |
| Double checkout (mandat §9) | `creation_key`/`cart_fingerprint` (idempotency_key MCP) — déjà construit et testé lors de la migration PREORDER (`TestInvariantJ_CreateRetryNeverCreatesADoubleDraft`), réutilisé tel quel. |

---

## G. Paiement

Inchangé — entièrement audité/durci lors de la phase PREORDER précédente
(escrow/IPN, `PREORDER_ESCROW_IPN_RECONCILIATION_2026-09-03.md`). Aucun
nouveau constat à ce niveau : le checkout (`create_preorder_draft`) ne
touche jamais au paiement, qui reste une étape POST-confirmation
(`_execute_and_finalize`, déjà séparée et déjà idempotente).

---

## H. Idempotence

| Opération | Mécanisme | Statut |
|---|---|---|
| ADD (panier) | Aucune — état conversationnel simple, pas d'effet externe irréversible avant checkout | KEEP (rien à protéger) |
| CHECKOUT (`create_preorder_draft`) | `creation_key`/`cart_fingerprint` → idempotency_key MCP (`mcp_idempotency_store`, déjà construit) | KEEP, déjà correct |
| CONFIRM (`confirm_preorder_draft`) | `execution_key(draft)` + garde `status != DRAFT` côté serveur (double protection, déjà migrée) | KEEP, déjà correct |
| PAYMENT | Déjà audité phase précédente | KEEP |

---

## I. Concurrence — résultats des tests

Ce dépôt n'a AUCUNE infrastructure Postgres de test réelle (confirmé,
constat répété à travers TOUTES les phases de cet audit multi-mandat).
Les tests DB-level nouveaux de cette phase
(`test_create_preorder_draft_pricing_tiers.py`,
`test_bootstrap_preorder_draft_price_fidelity.py`) utilisent un faux
moteur MINIMAL (capture des objets construits, pas de simulation `FOR
UPDATE`) — même limite honnête que documentée pour l'audit AUCTION/BID
précédent. Le tri anti-deadlock (section F) n'a PAS de test de
concurrence réelle à threads multiples pour la même raison — sa preuve
est la RÉUTILISATION d'un motif déjà éprouvé ailleurs dans ce même
fichier (`finalize_multi_order`), pas une observation sous charge.

---

## J. Legacy

**Rien supprimé** — l'audit n'a trouvé AUCUNE duplication réelle entre
`active_cart`/`draft_payload`/`transaction_payload` (section B) ; chacun
joue un rôle distinct et nécessaire. `resolved_id`/`waiting_for_confirmation`/
`quantity_display`/`unit_display`/`expected_input` — recherchés
explicitement dans `cart.py`/`cart_service.py`/`preorder.py`/
`preorder_confirmation.py` : **absents** de tout le code touché par ce
chantier (grep ciblé, zéro occurrence hors commentaires expliquant
POURQUOI ils ont été évités).

---

## K. Décision

| Sous-système | Décision | Justification |
|---|---|---|
| `active_cart` (le panier lui-même) | **KEEP** | Aucune incohérence que versioning/CAS résoudrait — état éditable pré-transactionnel, sans effet externe engagé. |
| `create_preorder_draft` (résolution palier) | **CORRIGER CIBLÉMENT** | Bug réel, sérieux (sous-facturation + sous-débit de stock pour tout produit à palier). Fait. |
| `bootstrap_preorder_draft` (fidélité affichage) | **CORRIGER CIBLÉMENT** | Bug réel "affichage ≠ exécution" — le total confirmé par l'acheteur pouvait diverger du total réellement facturé. Fait. |
| `confirm_preorder_draft` (ordre de verrouillage) | **CORRIGER CIBLÉMENT** | Gap de robustesse (deadlock évitable, pas une corruption) — motif déjà prouvé ailleurs, appliqué ici. Fait. |
| `render_summary()` (affichage palier) | **CORRIGER CIBLÉMENT** | Conséquence directe du fix palier — sans lui, le total serait correct mais l'affichage par ligne resterait ambigu pour un item à palier. Fait. |
| `finalize_multi_order` (code mort) | **NE PAS TOUCHER** | Signalé, pas supprimé — aucune certitude suffisante que ce n'est pas un point d'entrée légitime pour un canal non audité ici. |
| Confirmation (`ConfirmationTarget`/`PendingInteraction`) | **KEEP** | Déjà en place depuis la migration PREORDER, jamais remise en cause par cet audit. |

**Résultat conforme à ce que le mandat annonçait comme acceptable** :
CART → pas de Draft nécessaire → transaction SQL actuelle (largement)
suffisante → un nombre limité de corrections ciblées, toutes à la
frontière CHECKOUT elle-même, jamais dans le panier.

---

## L. Tests

| Fichier | Tests | Contenu |
|---|---|---|
| `tests/unit/test_create_preorder_draft_pricing_tiers.py` (NOUVEAU) | 4 | Palier résolu serveur : facturation/débit corrects, `tier_id` périmé géré proprement, nombre de paquets non-entier rejeté, produit à tarif unique inchangé |
| `tests/architecture/test_bootstrap_preorder_draft_price_fidelity.py` (NOUVEAU) | 2 | Le draft reflète le total/prix SERVEUR (jamais le panier périmé), repli sûr si le serveur ne renvoie pas encore `items`/`total_amount` |
| Suite complète (`pytest tests/`) | — | Re-vérifiée verte après CHAQUE correctif (voir résultat annexé) — zéro nouvelle régression sur `tests/architecture`, `tests/nodes`, `tests/chaos`, `tests/unit`, `tests/integration` |
