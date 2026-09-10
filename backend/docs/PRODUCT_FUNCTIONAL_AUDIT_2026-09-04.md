# Audit fonctionnel global — Produit Ladini (2026-09-04)

Après stabilisation transactionnelle de PROCUREMENT/PREORDER/SALES_PUBLISH_PRODUCT/
CART→CHECKOUT/AUCTION-BID, cet audit répond à une seule question :
**qu'est-ce qui empêche réellement un parcours producteur/acheteur complet
et exploitable de bout en bout ?** — pas "que reste-t-il à refactorer".

Méthode : lecture directe du catalogue d'intents réel
(`interpreter/intent.py`, ~65 goals), croisée avec le routage réel
(`_TUNNEL_ASSIGNMENTS`), les implémentations DB réelles
(`services/database/*.py`), et le catalogue de notifications réel
(`workers/outbox/templates.py`) — jamais une roadmap théorique.

---

## A. Cartographie produit

### PRODUCTEUR
```
compte/profil (get_or_create_farm, update_farm, profil géo)
  → publication produit (SALES_PUBLISH_PRODUCT — LIVE, migré, testé)
  → stock (add_stock/remove_stock/adjust_stock — LIVE ; 4 goals conversationnels BROKEN, voir D)
  → réception de demandes RFQ (MARKET_BROWSE_REQUESTS — LIVE)
  → bid (SALES_PLACE_BID/place_bid — LIVE, sécurisé)
  → mise à jour bid (update_bid_price — LIVE, sécurisé)
  → gain d'enchère (select_winning_bid — LIVE, sécurisé) OU vente directe préorder (confirm_preorder_draft — LIVE)
  → [GAP P0] paiement/livraison de la commande — voir E1
  → historique ("mes commandes" — get_producer_orders, LIVE et désormais complet, voir G)
  → notifications (AUCTION_WON_PRODUCER LIVE ; préorder direct SILENCIEUX, voir E2)
```

### ACHETEUR
```
recherche (search_products — LIVE)
  → sélection produit + palier de prix (pricing_tiers — LIVE, testé)
  → panier (active_cart — LIVE, audité)
  → checkout (create_preorder_draft/confirm_preorder_draft — LIVE, sécurisé)
      OU RFQ (create_auction → place_bid × N → select_winning_bid — LIVE, sécurisé)
  → paiement (escrow Paydunya si ESCROW_PAYMENT_ENABLED=True [DÉFAUT] — LIVE ;
    sinon aucun suivi de paiement numérique, cash implicite)
  → commande (Order — LIVE)
  → suivi ("où en est ma commande" — check_order_status/get_transaction_summary, LIVE)
  → livraison (OTP escrow — LIVE pour PREORDER escrowé UNIQUEMENT ; [GAP P0] absent pour AUCTION, voir E1)
  → historique ("mes commandes" — get_buyer_orders_dashboard, LIVE et déjà nettoyé DRAFT/SUPERSEDED)
```

## B. Fonctionnalités LIVE (fonctionnent réellement, testées)

- Recherche produit, pricing tiers, panier, checkout PREORDER (avec/sans escrow).
- RFQ complet : `create_auction` → `place_bid`/`update_bid_price`/`withdraw_bid` → `select_winning_bid` (row locks, status guards, stale-recap fix — 5 chantiers de cette session).
- Publication catalogue producteur (`SALES_PUBLISH_PRODUCT`).
- Paiement escrow Paydunya (initiation, IPN, OTP de déblocage) — pour PREORDER escrowé uniquement.
- Négociation acheteur↔producteur unique (`initiate_negotiation_session`/`update_negotiation_offer`).
- Notifications gagnant d'enchère + réservation production future + paiement escrow (Outbox, transactionnel, correct).
- Lecture "mes commandes" (buyer + producer) — assainie cette session (DRAFT/SUPERSEDED exclus, commandes d'enchère désormais visibles côté producteur).
- Vente directe rétroactive (`record_sale`, bookkeeping cash déjà conclu).

## C. Fonctionnalités PARTIAL (existent, incomplètes)

- **Paiement PROCUREMENT/AUCTION** : `Order.payment_status` reste `"PENDING"` indéfiniment — aucun mécanisme numérique de collecte (implicitement cash/hors-app, jamais explicité à l'utilisateur).
- **Livraison, toutes filières confondues sauf PREORDER-escrow** : voir E1 — un seul mécanisme de clôture existe dans TOUT le produit, et il ne couvre qu'une partie des commandes.
- **Notifications producteur** : couvre le gain d'enchère et le paiement escrow, mais PAS la confirmation directe d'un préorder non-escrowé (voir E2) ni la perte d'une enchère (le producteur perdant n'est jamais notifié, seulement visible en tirant lui-même `get_my_active_bids`).
- **Gestion de stock conversationnelle** : la RÉSOLUTION (quel article) fonctionne, l'EXÉCUTION finale échoue (tool_name inexistant) — voir D.

## D. Dead code / goals cassés

| Élément | Constat | Classification |
|---|---|---|
| `STOCK_RECORD_MOVEMENT`/`STOCK_ADJUST`/`STOCK_REMOVE_PARTIAL`/`STOCK_DELETE` | `intent.py` déclare `tool_name` `*_by_id` (`adjust_stock_by_id`, etc.) — AUCUNE méthode de ce nom n'existe (les vraies sont `adjust_stock`, `remove_stock`, `add_stock_movement`, `delete_stock`, sans suffixe). `flows/producer/flow.py` résout correctement QUEL stock est visé, puis retombe sur l'exécuteur générique qui utilise le `tool_name` cassé. | **BROKEN** — confirmé réel, mais explicitement hors scope de correction ce chantier (STOCK admin déjà dépriorisé par décision antérieure). Ne PAS migrer, juste documenté. |
| `accept_bid` (tool derrière `SALES_ACCEPT_CONTRACT`/`PROCUREMENT_ACCEPT_OFFER`) | Aucune méthode `accept_bid` nulle part dans `services/database/`. Le mécanisme RÉEL et sécurisé (`select_winning_bid`) existe et fonctionne, mais SOUS UN AUTRE GOAL (`BUYER_CHECK_AUCTION_STATUS`, tunnel `auction_tracking`). | **NEEDS DECISION** — référencé par 1 dataset eval + 1 test node (pas juste orphelin en apparence), mais structurellement cassé (`accept_bid` n'existe pas) et redondant avec un mécanisme équivalent déjà sécurisé sous un autre nom. Ne pas supprimer sans vérifier ces références d'abord (fait : voir ci-dessous). |
| `PROCUREMENT_SELECT_WINNER` (tool `select_winning_bid`, hors tunnel) | Existe en dehors de `_TUNNEL_ASSIGNMENTS` — si jamais classifié par le LLM, appellerait `select_winning_bid` via l'exécuteur GÉNÉRIQUE, contournant tout le tunnel sécurisé (`confirm_winner_selection`/GPS/stale-recap) audité cette session. Exige `auction_id`+`bid_id` en `required` — des UUID qu'un message naturel ne fournit jamais spontanément, ce qui limite la probabilité réelle d'atteinte, sans l'exclure structurellement. | **NEEDS DECISION** — risque théorique réel (contournement du tunnel sécurisé), probabilité d'occurrence faible mais non nulle. À rediriger vers le tunnel `auction_tracking` ou retirer, PAS à corriger dans ce chantier (mandat : audit, pas nouveau chantier technique). |
| `services/database/order_service.py` (`OrderService`, classe entière : `create_order`, `advance_order_status`, `advance_delivery_status`, `record_payment`, `list_buyer_orders`, `schedule_reminder`, `due_reminders`) | ZÉRO appelant nulle part dans `src/ladini` (recherche exhaustive) — n'est même pas incluse dans les mixins d'`AgriDatabaseService`. Bien construite (transitions + `OrderStatusHistory` journalisées), mais totalement débranchée. | **SAFE DELETE candidat** — mais NEEDS DECISION avant suppression réelle : c'est la SEULE implémentation de ce dépôt qui gérerait proprement `advance_delivery_status`/`record_payment`, potentiellement récupérable pour combler le gap E1 plutôt que jetée. |
| `services/database/delivery.py` (`DeliveryMixin` — `create_delivery`, `claim_delivery`, `start_delivery_transit`, `confirm_delivery_with_otp`, `get_delivery_status_tracking`) | ZÉRO appelant conversationnel (recherche exhaustive dans `graphs/`). Modèle `Delivery`/`DeliveryAgent` complet (assignation, transit GPS, OTP) mais jamais instancié — `create_delivery` n'est jamais appelée, donc AUCUNE ligne `Delivery` n'existe jamais en pratique. | **NEEDS DECISION** — même remarque que ci-dessus : candidat naturel pour combler E1, pas du code à supprimer sans réflexion. |
| `Auction.version` | Déjà tranché (chantier précédent) : écriture retirée, colonne conservée et documentée DEPRECATED/UNUSED. | **CLOS** — pas rouvert ici (mandat §12 : aucune dépendance réelle trouvée dans cet audit non plus). |

## E. Gaps P0 — bloquants critiques

### E1 — Aucune clôture de livraison/paiement pour les commandes d'appel d'offres (et les préorders non-escrowés)

**Utilisateur** : PRODUCTEUR (ne peut jamais "clore" une vente RFQ) ET ACHETEUR (aucune commande d'enchère ne sort jamais de "en attente").

**Problème** : `select_winning_bid` crée une `Order(status="CONFIRMED", payment_status="PENDING", delivery_status="PENDING")` — **aucun mécanisme, nulle part dans le produit conversationnel, ne peut faire avancer ces deux derniers statuts** pour ce type de commande. Le SEUL mécanisme de clôture qui existe (`verify_delivery_otp`, escrow) exige structurellement un `OrderItem` (INNER JOIN) — une commande d'enchère n'en a jamais.

**Impact business** : toute transaction RFQ (l'un des 3 piliers transactionnels du produit) reste éternellement "PENDING" dans l'historique des DEUX parties, sans preuve de livraison ni de paiement, sans clôture possible. Un préorder confirmé SANS escrow (`ESCROW_PAYMENT_ENABLED=False`, un mode réellement supporté et testé) a exactement le même problème.

**Preuve** (code, pas supposition) : `tests/unit/test_auction_orders_have_no_fulfillment_closure.py` — prouve sur le VRAI code source (1) que `verify_delivery_otp` utilise un INNER JOIN sur `OrderItem`/`Product` (jamais `outerjoin`), (2) que `select_winning_bid` ne crée jamais d'`OrderItem`, (3) qu'aucune autre entrée de `_TUNNEL_ASSIGNMENTS` ne couvre la livraison/le paiement.

### E2 — Producteur jamais notifié d'une vente préorder directe (mode non-escrow)

**Utilisateur** : PRODUCTEUR.

**Problème** : `confirm_preorder_draft` (le chemin de confirmation immédiate, utilisé quand `ESCROW_PAYMENT_ENABLED=False`) n'enfile AUCUNE notification Outbox — confirmé sur le code source (`"outbox" not in inspect.getsource(...)`). Le producteur ne découvre la vente qu'en consultant PROACTIVEMENT "mes commandes".

**Impact business** : conditionnel — le mode par défaut (`ESCROW_PAYMENT_ENABLED=True`) notifie bien via `ESCROW_PAYMENT_SECURED_PRODUCER`. Le gap ne se matérialise QUE si l'escrow est désactivé en déploiement réel — non vérifiable depuis ce dépôt seul (`.env.example` ne fixe pas cette valeur). Classé P0 conditionnel / P1 par défaut de configuration — voir priorisation.

## F. Gaps P1 — prochains chantiers à forte valeur

- **F1** — Réutiliser/adapter `OrderService`/`DeliveryMixin` (ou une version simplifiée) pour combler E1 — chantier naturel suivant.
- **F2** — Notification producteur inconditionnelle sur toute confirmation préorder (indépendamment du mode escrow) — petit correctif, même pattern Outbox déjà 3 fois éprouvé cette session.
- **F3** — Producteur jamais notifié quand il PERD une enchère (`select_winning_bid` ne notifie que le gagnant) — amélioration UX, pas un blocage.
- **F4** — `PROCUREMENT_SELECT_WINNER`/`PROCUREMENT_ACCEPT_OFFER` : rediriger vers le tunnel sécurisé ou retirer (risque de contournement du hardening déjà fait, probabilité faible).

## G. Gaps P2 — améliorations

- STOCK_* goals cassés (tool_name) — déjà dépriorisé explicitement, laissé tel quel.
- `order_service.py` : décision de suppression ou de récupération à prendre lors du chantier F1, pas isolément.
- Web ↔ Agent (cohérence panier/commande) : **NOT VERIFIABLE FROM THIS REPOSITORY** — le dépôt web (`frontag`) n'est pas accessible depuis ici (confirmé lors du chantier CART→CHECKOUT).

## H. Parcours E2E — ce qui s'exécute réellement de bout en bout aujourd'hui

| Parcours | Jusqu'où va-t-il ? |
|---|---|
| Acheteur : recherche → tier → panier → checkout → preorder → **paiement escrow → livraison OTP → PAID_OUT/DELIVERED** | **Complet** (le SEUL parcours entièrement bouclé du produit) |
| Acheteur : recherche → tier → panier → checkout → preorder confirmé **sans escrow** | S'arrête à `CONFIRMED/PENDING/PENDING` — jamais clôturé (E1) |
| RFQ : demande acheteur → bids producteurs → mise à jour → **gagnant sélectionné → Order créée** | S'arrête à `CONFIRMED/PENDING/PENDING` — jamais clôturé (E1) |
| Producteur : publication → réception demande → bid → gain | Complet jusqu'à la vente ; rien après (E1) |

## I. Tests manquants (uniquement ceux couvrant un vrai risque fonctionnel)

- ✅ Ajouté : `tests/unit/test_auction_orders_have_no_fulfillment_closure.py` — preuve du gap E1 (structure de requête + absence de tunnel).
- Manquant, à écrire lors du chantier F1 (pas ici — pas de code sans décision de conception d'abord) : un test de PARCOURS complet "commande RFQ confirmée → [nouveau mécanisme] → payment_status=PAID/delivery_status=DELIVERED" une fois E1 résolu.
- Pas de test manquant identifié pour E2 au-delà de ce qui existe déjà (le gap est une ABSENCE d'appel, pas un comportement erroné à piéger).

## J. Recommandation de prochain chantier

# **F1 — Clôture paiement/livraison pour les commandes non-escrowées (RFQ + préorder cash)**

- **Utilisateur** : les DEUX — producteur (ne peut jamais "finir" une vente RFQ dans l'app) et acheteur (commande éternellement "en attente" dans son historique).
- **Impact** : P0 — touche un pilier transactionnel ENTIER (100% des ventes RFQ, plus tout préorder sans escrow) ; sans ce chantier, le produit ne peut pas honnêtement prétendre gérer une transaction de bout en bout dès qu'elle sort du chemin escrow Paydunya.
- **Preuve** : `tests/unit/test_auction_orders_have_no_fulfillment_closure.py` (code réel, pas supposition) + section E1/H ci-dessus.
- **Effort estimé** : MOYEN — la brique DB existe déjà à l'état de code MORT mais BIEN CONÇU des deux côtés (`order_service.py::advance_delivery_status`/`record_payment`, `delivery.py::DeliveryMixin`) ; le travail réel est de choisir/adapter LEQUEL réutiliser (probablement une version simplifiée, pas le modèle `DeliveryAgent` complet — à valider avec le produit), l'exposer comme 1-2 nouveaux goals conversationnels (ex: "PRODUCER_CONFIRM_SALE_COMPLETE"), et réutiliser l'Outbox déjà éprouvé pour notifier.
- **Dépendances** : décision produit préalable — le modèle métier veut-il un simple "marqué livré par le producteur" (déclaratif, comme `verify_delivery_otp` mais sans OTP puisqu'il n'y a pas d'argent séquestré à débloquer), ou un flux avec confirmation acheteur également ? Cette décision n'est PAS technique — elle conditionne l'implémentation et n'a pas été tranchée dans cet audit (volontairement, conformément à la règle finale du mandat : ne pas inventer de features, seulement identifier le manque réel).

---

**Ce que cet audit n'a PAS fait** (délibérément, conformément au mandat) :
aucune migration de STOCK, aucune réouverture d'Auction/Bid/CART/PREORDER,
aucune suppression de code sans décision explicite, aucune fonctionnalité
inventée au-delà de ce que le code référence déjà (E1/E2/F1-F4 sont tous
des trous entre des briques EXISTANTES, jamais une idée nouvelle).
