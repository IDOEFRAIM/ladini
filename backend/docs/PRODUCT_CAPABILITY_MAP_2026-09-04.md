# PRODUCT CAPABILITY MAP — 2026-09-04

Cartographie des capacités RÉELLES d'Ladini, construite en remontant
la chaîne complète pour chaque capacité :

```
Goal / intent → Tunnel → Handler → Service DB → Entité → Transition de statut
              → Notification → Dashboard → État terminal
```

**Règle appliquée partout** : une capacité n'est jamais déclarée COMPLETE
parce qu'une fonction porte son nom. Chaque ligne `COMPLETE` a été vérifiée
jusqu'à un état terminal réellement atteignable par un utilisateur réel.

Statuts : **COMPLETE** (toutes les étapes existent, l'état final est
atteignable) · **PARTIAL** (le début marche, une étape essentielle manque) ·
**BROKEN** (semble complet, un chemin réel échoue) · **DEAD** (le code
existe, aucun chemin utilisateur ne l'atteint) · **MISSING** (attendue pour
fermer le journey, n'existe pas).

---

## 1. BUYER CAPABILITIES

| Capability | Intent | Tunnel | Handler | Mutation DB | Notification | Dashboard | État terminal | Status |
|---|---|---|---|---|---|---|---|---|
| Search products | `BUYER_REQUEST`/`SEARCH_PRODUCTS` | `handled_by_flow` | `flows/buyer/flow.py` | — (lecture) | — | — | résultats affichés | **COMPLETE** |
| View product / pricing tiers | via recherche | cart | `flows/buyer/cart.py` | — | — | — | paliers affichés | **COMPLETE** |
| Choose quantity | cart | cart | `_execute_selection_action` | `active_cart` | — | panier | ligne calculée | **COMPLETE** |
| Choose producer | cart | cart | idem | `active_cart` | — | panier | vendeur retenu | **COMPLETE** |
| Add to cart | `BUYER_ADD_TO_CART` | cart | `cart.py` | `active_cart` (état conversationnel, jamais DB) | — | panier | article ajouté | **COMPLETE** |
| Edit / remove cart item | `BUYER_VIEW_CART` + actions de sélection | cart | `cart.py` | `active_cart` | — | panier | panier à jour | **COMPLETE** |
| Checkout (draft) | `BUYER_PREORDER_INIT` | preorder | `preorder_confirmation.py` | `Order(DRAFT)` + `PreorderDraft` (CAS Postgres) | — | exclu des dashboards | `DRAFT` | **COMPLETE** |
| Confirm order | `BUYER_PREORDER_CONFIRM` | preorder | `confirm_preorder_draft` | `DRAFT→CONFIRMED`, débit stock | `PREORDER_CONFIRMED_PRODUCER` (F2) | oui | `CONFIRMED` | **COMPLETE** |
| Cancel draft | `_cancel_preorder` | preorder | `cancel_preorder_draft` | `DRAFT→CANCELLED` | — | exclu | `CANCELLED` | **COMPLETE** |
| Cancel confirmed order | `BUYER_CANCEL_ORDER` | order_tracking | `cancel_pending_order` | `PENDING`/`CONFIRMED→CANCELLED`, recrédit stock, limite anti-abus | `ORDER_CANCELLED_BY_BUYER_PRODUCER` | oui | `CANCELLED` | **COMPLETE** *(Phase 1 : était BROKEN — garde `PENDING` inatteignable)* |
| Track order | `BUYER_CHECK_ORDER_STATUS` | order_tracking | `check_order_status` | — | — | oui | statut affiché | **COMPLETE** |
| View order history | `BUYER_LIST_ORDERS` | order_tracking | `get_buyer_orders_dashboard` | — | — | oui (hors `DRAFT`/`SUPERSEDED`) | liste | **COMPLETE** |
| Receive delivery + payment confirmation | — (passif) | — | `confirm_delivery_and_payment` (producteur) | `CONFIRMED→COMPLETED` | `ORDER_COMPLETED_AT_DELIVERY_BUYER` (F1) | oui | `COMPLETED` | **COMPLETE** |
| Receive producer rejection/cancellation | — | — | — | — | — | — | — | **MISSING** (dépend de P1-2) |
| RFQ (create) | `PROCUREMENT_CREATE_REQUEST` | générique | `create_auction` | `Auction(OPEN)` | `AUCTION_INVITE_PRODUCER` | `BUYER_LIST_AUCTIONS` | `OPEN` | **COMPLETE** |
| View / compare bids | `BUYER_CHECK_AUCTION_STATUS` | auction_tracking | `get_auction_bids` | — | — | oui | offres listées | **COMPLETE** |
| Negotiate | `BUYER_NEGOTIATE_PRICE` | negotiation | `negotiation.py` | `NegotiationSession` | — | — | prix accepté/refusé | **COMPLETE** |
| Select winner | `BUYER_CHECK_AUCTION_STATUS` → `confirm_winner_selection`/`finalize_winner` | auction_tracking | `select_winning_bid` | `Auction→CLOSED`, `Bid→WINNING/LOST`, `Order(CONFIRMED)` | `AUCTION_WON_PRODUCER` + `AUCTION_LOST_PRODUCER` (F3) | oui | `CONFIRMED` | **COMPLETE** |
| Cancel RFQ | (via `cancel_auction`) | auction_tracking | `cancel_auction` | `OPEN→CANCELLED` (verrou FOR UPDATE) | — | oui | `CANCELLED` | **COMPLETE** |
| Reopen interrupted transaction | `RESUME_TUNNEL` + `pending_interaction` (DURABLE) | tous | `core/pending_interaction.py` | checkpointer | — | — | reprise au point exact | **COMPLETE** |
| `PROCUREMENT_SELECT_WINNER` (voie parallèle) | déclaré | aucun | `RuntimeError` explicite (F4) | — | — | — | — | **DEAD (volontairement neutralisé)** |
| `PROCUREMENT_ACCEPT_OFFER` | déclaré | aucun | `RuntimeError` (F4) — `accept_bid` n'existe pas | — | — | — | — | **DEAD (volontairement neutralisé)** |

## 2. PRODUCER CAPABILITIES

| Capability | Intent | Tunnel | Handler | Mutation DB | Notification | Dashboard | État terminal | Status |
|---|---|---|---|---|---|---|---|---|
| Create / publish product | `SALES_PUBLISH_PRODUCT` | générique | `create_product` | `Product` | `NEW_PRODUCT_ALERT_BUYER` | catalogue | publié | **COMPLETE** |
| Edit product (prix/qté/nom/unité/paliers) | `SALES_UPDATE_PRODUCT` | `producer_update` | `update_product_price_and_qty` | `Product` (FOR UPDATE, paliers revalidés contre l'unité finale) | — | catalogue | à jour | **COMPLETE** |
| **Unpublish / retirer un produit** | **`SALES_UNPUBLISH_PRODUCT`** | résolveur dédié | **`delete_product`** | archivage doux (`is_available=False`) ou suppression physique, refus si commandes actives | — | disparaît du catalogue ET de la recherche acheteur | retiré | **COMPLETE** *(Phase 2 : était DEAD — méthode DB complète, zéro chemin utilisateur)* |
| Set pricing tiers | `SALES_PUBLISH_PRODUCT`/`SALES_UPDATE_PRODUCT` | idem | `validate_pricing_tiers` | `Product.pricing_tiers` | — | catalogue | paliers actifs | **COMPLETE** |
| Set minimum order | — (politique PLATEFORME par `SubCategory`) | — | `domain/order_policy.py` | — | — | — | appliqué au checkout | **COMPLETE (par conception — pas une capacité producteur)** |
| Declare future production | `DECLARE_CROP_CYCLE`/`SALES_UPDATE_PRODUCTION` | `producer_update` | `declare_future_production`/`update_production_fields` | `MarketOffer` | `PREORDER_RESERVED_PRODUCER` | productions | déclarée | **COMPLETE** |
| Publish/unpublish une production future | — | — | `update_production_visibility` | `MarketOffer.is_public`/`preorder_enabled` | — | — | — | **DEAD** (méthode complète, aucun intent) |
| Receive buyer order | — (passif) | — | Outbox | — | `PREORDER_CONFIRMED_PRODUCER` (F2) | `SALES_LIST_ORDERS` | notifié | **COMPLETE** |
| Accept / confirm buyer order | — | — | — | *(par conception : la commande naît `CONFIRMED` du geste acheteur, il n'y a pas d'étape d'acceptation producteur)* | — | — | — | **MISSING — décision produit (P1-2)** |
| Reject buyer order / cancel confirmed order | — | — | — | — | — | — | — | **MISSING — décision produit (P1-2)** |
| Deliver + receive payment | `PRODUCER_CONFIRM_DELIVERY_PAYMENT` | résolveur dédié | `confirm_delivery_and_payment` | `CONFIRMED→COMPLETED`, `payment=PAID`, `delivery=DELIVERED`, 3 `OrderStatusHistory` | `ORDER_COMPLETED_AT_DELIVERY_BUYER` | oui | `COMPLETED` | **COMPLETE** *(Phase 2 : était BROKEN en pratique — impasse validateur, voir P1-3)* |
| Deliver (escrow, code OTP) | `PRODUCER_CONFIRM_DELIVERY_OTP` | `producer_escrow` | `verify_delivery_otp` | `ESCROWED→PAID_OUT` | — | oui | `PAID_OUT` | **COMPLETE** (commandes escrow avec `OrderItem` uniquement) |
| Record direct sale (cash) | `SALES_RECORD_DIRECT` | générique | `record_sale` | `Order(COMPLETED)` direct | — | oui | `COMPLETED` | **COMPLETE** |
| View order history | `SALES_LIST_ORDERS` | générique | `get_producer_orders` | — | — | catalogue + préorder + **RFQ** | liste | **COMPLETE** |
| Place bid | `SALES_PLACE_BID` | `producer_auction` | `place_bid` | `Bid(PENDING)` | — | `MARKET_GET_MY_PROPOSALS` | `PENDING` | **COMPLETE** |
| Update bid | (même tunnel) | `producer_auction` | `update_bid_price` | `Bid.offered_price` | — | oui | à jour | **COMPLETE** |
| Withdraw bid | (même tunnel) | `producer_auction` | `withdraw_bid` | `PENDING→WITHDRAWN` (FOR UPDATE) | — | oui | `WITHDRAWN` | **COMPLETE** |
| Know whether bid won / lost | — (passif) | — | `select_winning_bid` | — | `AUCTION_WON_PRODUCER` / `AUCTION_LOST_PRODUCER` (F3) | oui | notifié | **COMPLETE** |
| Track winning order | `SALES_LIST_ORDERS` | générique | `get_producer_orders` (source RFQ incluse) | — | — | oui | visible | **COMPLETE** |
| Accept contract | `SALES_ACCEPT_CONTRACT` | résolveur `_resolve_bid` | `commit_staged_transaction` | **aucune — la méthode DB n'existe pas** | — | — | — | **BROKEN** |
| Stock management | `STOCK_ADJUST`/`STOCK_DELETE`/`STOCK_REMOVE_PARTIAL`/`STOCK_RECORD_MOVEMENT` | résolveur `_resolve_stock` | `*_by_id` — **aucune méthode DB de ce nom** | — | — | — | — | **BROKEN** (déprioritisé par décision produit antérieure) |
| Report anomaly | `SYSTEM_REPORT_ANOMALY` | générique | `report_anomaly` — **aucune méthode DB** | — | — | — | — | **BROKEN** |

## 3. Capacités transverses non listées au mandat mais trouvées dans le code

| Capability | Constat | Status |
|---|---|---|
| `OrderService` (`create_order`, `advance_order_status`, `record_payment`, `schedule_reminder`…) | zéro appelant, jamais monté dans les mixins | **DEAD** |
| `DeliveryMixin` (`create_delivery`, `claim_delivery`, OTP coursier) | zéro appelant ; modélise un réseau de livreurs tiers qui ne correspond pas au modèle métier réel (le producteur livre lui-même) | **DEAD** |
| `product_service.py::search_products` | zéro appelant (la vraie recherche est `buyer.py::search_products`) | **DEAD** |
| Escrow Paydunya (IPN, TTL, remboursement) | complet et testé, mais désactivé par décision produit (`PAIEMENT = À LA LIVRAISON`) | **COMPLETE mais hors périmètre produit actuel** |
| Modération / anti-abus (blocage compte, limites d'annulation, produits interdits) | actif, dérivé de la DB | **COMPLETE** |
| Photos produit / enchère / offre (WhatsApp → Supabase) | actif | **COMPLETE** |
| Workers proactifs (Beat + Outbox : sollicitations enchères, proximité, réconciliation preorder) | actifs | **COMPLETE** |

---

## 4. Synthèse par statut

- **COMPLETE : 38** capacités vérifiées jusqu'à un état terminal réel.
- **BROKEN : 3 familles** — `SALES_ACCEPT_CONTRACT`/`SYSTEM_COMMIT_TRANSACTION` (`commit_staged_transaction` inexistant), `STOCK_*` (`*_by_id` inexistants, déjà déprioritisé), `SYSTEM_REPORT_ANOMALY` (`report_anomaly` inexistant).
- **DEAD : 6** — `PROCUREMENT_SELECT_WINNER`/`PROCUREMENT_ACCEPT_OFFER` (neutralisation volontaire F4), `update_production_visibility`, `OrderService`, `DeliveryMixin`, `product_service.py`.
- **MISSING : 2** — rejet/annulation producteur d'une commande `CONFIRMED`, et la notification acheteur correspondante (une seule et même décision produit).

Les corrections apportées et le détail de chaque gap sont dans
[PRODUCT_COMPLETENESS_PHASE2_2026-09-04.md](PRODUCT_COMPLETENESS_PHASE2_2026-09-04.md).
