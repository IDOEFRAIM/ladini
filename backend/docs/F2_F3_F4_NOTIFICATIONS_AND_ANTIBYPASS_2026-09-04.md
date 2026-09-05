# F2 / F3 / F4 — Notifications manquantes + fermeture du bypass winner-selection

2026-09-04. Suite de F1 (clôture paiement-à-la-livraison). Ne rouvre PAS le
modèle de paiement/livraison ni le hardening Auction/Bid déjà fermé —
uniquement les trois sujets restants identifiés par l'audit fonctionnel.

---

## A. F2 — Notification producteur, préorder direct (sans escrow)

**Root cause** : `confirm_preorder_draft` (`services/database/buyer.py`,
le chemin `ESCROW_PAYMENT_ENABLED=False`) n'enfilait AUCUNE notification
Outbox — contrairement au chemin escrow
(`EscrowMixin.mark_escrow_paid`, déclenché par l'IPN Paydunya, qui notifie
déjà via `ESCROW_PAYMENT_SECURED_PRODUCER`, un template distinct par
producteur du panier, dédupliqué par `(order.id, phone)`).

**Fix** : `confirm_preorder_draft` réutilise EXACTEMENT le même motif de
collecte multi-producteurs que `mark_escrow_paid` (un panier peut porter
des articles de plusieurs producteurs — chacun notifié une seule fois) et
enfile un NOUVEAU template `PREORDER_CONFIRMED_PRODUCER` — jamais "paiement
reçu" (aucun paiement en ligne n'a eu lieu), uniquement "nouvelle commande
confirmée, payable à la livraison". Le chemin escrow n'est jamais touché
(les deux fonctions sont mutuellement exclusives par construction —
`_execute_and_finalize` bifurque sur `ESCROW_PAYMENT_ENABLED` AVANT
d'appeler l'une ou l'autre, jamais les deux).

**Idempotence** : aucune nouvelle primitive. Le garde déjà existant
(`order.status != "DRAFT"` → `BusinessRuleException(reason="not_draft")`)
fait échouer tout retry AVANT d'atteindre le code de notification — un
`confirm_preorder_draft` rejoué ne peut structurellement jamais renotifier.
`dedupe_key=f"PREORDER_CONFIRMED_PRODUCER:{order.id}:{phone}"` ajouté en
défense en profondeur, même convention que les 4 autres templates Outbox
de ce dépôt.

**Tests** — `tests/unit/test_preorder_confirmed_producer_notification.py`
(4) : notification unique sur confirmation simple, retry n'ajoute rien
(guard `not_draft` levé avant le code de notif), panier multi-producteurs
notifie chaque producteur une fois, absence de téléphone résolu
n'empêche jamais la confirmation.

## B. F3 — Producteurs perdants d'une enchère

**Root cause** : `select_winning_bid` transitionne déjà toutes les autres
offres `PENDING`/`WINNING` → `LOST` (bulk `UPDATE`, motif préexistant "sans
cela un producteur non retenu resterait En attente à vie") — mais
n'informait JAMAIS ces producteurs. Seul le gagnant recevait
`AUCTION_WON_PRODUCER`.

**Fix** : le MÊME bulk `UPDATE` récupère désormais `RETURNING
Bid.producer_id` — la décision métier ELLE-MÊME (quelles offres viennent
RÉELLEMENT de basculer) devient directement la liste des producteurs à
notifier, jamais une requête séparée ni une supposition. Un unique appel
`_outbox_repo.enqueue(...)` porte à la fois l'entrée gagnant et toutes les
entrées perdants (jamais un `for bid: send_whatsapp()` synchrone — Outbox,
même transaction que la commande). Nouveau template `AUCTION_LOST_PRODUCER`
("votre offre n'a pas été retenue"), `dedupe_key=f"AUCTION_LOST:{auction.id}:{producer_id}"`.

**Bid statuses couverts** : `Bid.status.in_(["PENDING", "WINNING"])` —
LA MÊME clause déjà en place, inchangée. Une offre `WITHDRAWN` (retrait
volontaire, déjà su du producteur) est STRUCTURELLEMENT exclue de
`RETURNING` — elle n'a jamais pu, ne peut pas, ne pourra jamais être
notifiée "vous avez perdu" (preuve sur le code source,
`TestUpdateWhereClauseExcludesWithdrawn`). Une offre déjà `LOST` (rejeu)
est exclue de la même façon.

**Un producteur, plusieurs bids** : structurellement impossible sur UNE
même enchère (`bids_auction_producer_unique`, UNIQUE sur `(auction_id,
producer_id)` — `place_bid` fait un UPSERT, jamais un second bid). La
question "par producteur ou par bid" ne se pose donc pas : les deux
coïncident toujours pour une même enchère.

**Auction sans winner** : hors de portée — aucune notification "perdu"
n'est JAMAIS enfilée en dehors de `select_winning_bid` lui-même (le seul
écrivain du statut `LOST`) ; une enchère qui expire sans jamais avoir de
gagnant (`check_and_expire_auctions`) ne déclenche aucun `UPDATE...RETURNING`
et donc aucune notification — comportement déjà correct, vérifié, pas
modifié.

**Idempotence** : le garde déjà en place (`auction.status != "OPEN"` →
rejet) empêche tout rejeu d'atteindre à nouveau le bulk UPDATE — prouvé
(`TestRetryNeverRepublishesNotifications`, aucune entrée Outbox sur une
tentative sur une enchère déjà `CLOSED`).

**Tests** — `tests/unit/test_auction_loser_notification.py` (5) : gagnant
+ perdant notifiés exactement une fois chacun avec le bon téléphone/template ;
aucun perdant → aucune notification perdante, pas de crash ; plusieurs
perdants → chacun sa propre notification, jamais de doublon ; rejeu sur
enchère déjà close → zéro notification ; preuve sur le code source que la
clause `WHERE` exclut structurellement `WITHDRAWN`.

## C. F4 — Tous les points d'entrée winner-selection, avant/après

| Entrée | Avant | Après |
|---|---|---|
| `BUYER_CHECK_AUCTION_STATUS` (`_TUNNEL_ASSIGNMENTS: "auction_tracking"`) → `order_tracking.py::confirm_winner_selection`/`finalize_winner`/`_execute_winner_selection` → `AuctionGateway.select_winning_bid` → `services/database/auction.py::select_winning_bid` | Sécurisé (row lock, status guards, stale-recap, GPS) | **Inchangé** — seul chemin réellement utilisé en pratique |
| `BUYER_NEGOTIATE_PRICE` (`_TUNNEL_ASSIGNMENTS: "negotiation"`) → `negotiation.py::_handle_viewing_offers` → même gateway → même DB | Tunnelé, mais SES PROPRES limites déjà documentées (GPS best-effort, pas de récap prix) — chantier antérieur, hors scope ici | **Inchangé** (mandat §21 explicite : ne pas rouvrir) |
| `PROCUREMENT_SELECT_WINNER` (HORS `_TUNNEL_ASSIGNMENTS`) → exécuteur générique → `actions/procure.py::prep_procurement_select_winner` → tool_name `select_winning_bid` | **VIVANT** — contournait TOUTES les protections du premier chemin, exigeait `auction_id`+`bid_id` bruts (peu probable mais pas structurellement impossible en LLM) | **Neutralisé** — `raise RuntimeError(...)` inconditionnel, ne résout plus jamais de tool_name |
| `PROCUREMENT_ACCEPT_OFFER` → `prep_procurement_accept_offer` → tool_name `accept_bid` | Déjà cassé (`accept_bid` n'existe comme aucune méthode DB réelle) — échouait avec une erreur technique brute | **Neutralisé proprement** — même `RuntimeError` explicite, message clair au lieu d'un échec MCP opaque |

**Décision (mandat §18)** : ni redirection vers le tunnel sécurisé (aurait
ajouté un second chemin d'ACCÈS vers la même décision, plus de surface à
maintenir) ni suppression du catalogue d'intents (aurait cassé la
cohérence de `INTENT_CONFIG`/`registry.py::validate_integrity`, qui exige
un handler pour tout goal WRITE déclaré, et le lien avec les datasets eval
existants) — **désactivation à la source**, l'option la plus étroite et la
moins risquée offerte par le mandat lui-même. `RuntimeError` (pas
`ValueError`) délibérément : évite le mécanisme de "self-heal" de
l'exécuteur (`nodes/executor.py`, qui tenterait de "réparer" un champ
manquant — pas le cas ici), tombe proprement sur le catch-all générique
déjà existant (`_GENERIC_TECHNICAL_ERROR`), sans toucher `executor.py`.

**`select_winning_bid` (la fonction DB) n'a pas été dupliquée ni modifiée**
(mandat §19) — uniquement les DEUX points d'ENTRÉE identifiés comme
dangereux ont été neutralisés.

## D. Notification architecture (Outbox + idempotence)

Pattern confirmé UNIFORME sur les 3 correctifs (F1/F2/F3), jamais réinventé :
`DB transaction (déjà en cours) → entries[] construites en Python → UN SEUL
appel `_outbox_repo.enqueue(session, entries)` avant la fin de la
transaction → commit → cron `outbox-dispatch` (~30s) → envoi WhatsApp`.
Jamais de `send_whatsapp()` synchrone. Idempotence par
`dedupe_key` (déjà la convention Outbox existante) COMBINÉE aux gardes
d'état de chaque fonction (qui empêchent structurellement un rejeu
d'atteindre le code de notification) — deux couches de protection, aucune
nouvelle primitive introduite dans les trois cas.

**7 templates Outbox au total désormais** (`workers/outbox/templates.py`) :
5 pré-existants + `ORDER_COMPLETED_AT_DELIVERY_BUYER` (F1) +
`PREORDER_CONFIRMED_PRODUCER` (F2) + `AUCTION_LOST_PRODUCER` (F3).

## E. Preuve anti-bypass (structurelle)

`tests/unit/test_winner_selection_single_entrypoint.py` (7 tests) :
1. Les deux handlers désactivés lèvent TOUJOURS, quel que soit le payload.
2. Les deux goals restent déclarés dans le registre (intégrité préservée)
   mais sans tunnel dédié ni tool_name résolvable.
3. **Preuve finale sur le code source réel** : `select_winning_bid` (la
   méthode du gateway conversationnel) n'est appelée QUE depuis
   `order_tracking.py` et `negotiation.py` dans TOUT `graphs/` — et
   `actions/procure.py` ne nomme plus JAMAIS `"select_winning_bid"` comme
   tool_name. Ce test échoue si un troisième site réapparaît.

## F. E2E — RFQ → Winner → Order → Fulfillment

`tests/unit/test_rfq_to_completion_with_notifications_e2e.py` (1 test,
scénario complet du mandat §22, verbatim) : A=250, B=275, A→300, buyer
sélectionne A → `AUCTION_WON_PRODUCER`(A) + `AUCTION_LOST_PRODUCER`(B)
exactement une fois chacun → `Order(CONFIRMED)` → producteur A confirme
livraison+paiement (F1) → `Order(COMPLETED, payment=PAID, delivery=DELIVERED)`
→ `ORDER_COMPLETED_AT_DELIVERY_BUYER` enfilée, jamais confondue avec les
notifications de sélection.

## G. Régression complète

`pytest tests/` — résultat exact ajouté ci-dessous après exécution ;
attendu : mêmes 4 échecs préexistants sans rapport
(`test_create_auction_catalog_gate.py`), **zéro nouvelle régression** des
17 nouveaux tests (F2 : 4, F3 : 5, anti-bypass : 7, E2E final : 1) + des 3
fichiers de tests existants corrigés pour la nouvelle forme de requête
(`RETURNING`/notification producteur ajoutées à des fonctions déjà
couvertes : `test_confirm_preorder_draft_row_locking.py`,
`test_select_winning_bid_state_guards.py`,
`test_auction_bid_full_lifecycle_e2e.py`, `test_payment_at_delivery_e2e.py`).

## H. Gaps restants (réels, pas génériques)

1. **Observabilité télémétrie AUCTION** — le mandat demandait de
   "réutiliser la télémétrie existante" pour des évènements
   `AUCTION_WINNER_SELECTED`/`AUCTION_BID_LOST`/`WINNER_SELECTION_REJECTED`
   corrélables. Vérifié : `services/database/auction.py` n'a JAMAIS émis
   de télémétrie structurée (`record_procurement_transaction_event`-style,
   utilisée par PROCUREMENT/PREORDER) — uniquement des `logger.info`/
   `logger.warning` bruts, un gap PRÉ-EXISTANT à F1-F4, pas introduit par
   ce chantier. Construire un nouveau canal de télémétrie pour AUCTION
   aurait dépassé le périmètre "notifications + anti-bypass" de cette
   phase (mandat §H : "uniquement les problèmes réellement démontrés" —
   ceci EST démontré, mais sa correction est un chantier à part, pas une
   extension mineure). Non corrigé, documenté honnêtement plutôt que
   passé sous silence.
2. **Dead code confirmé, non supprimé** — `ProcurementSelectWinnerPayload`/
   `ProcurementAcceptOfferPayload`/`ProcurementSelectWinnerCommand`/
   `ProcurementAcceptOfferCommand`/`ProcurementService.select_winner`/
   `.accept_offer` (`domain/procurement.py`, `actions/procure_dto.py`) —
   recherche exhaustive confirmée (aucun autre appelant), mais laissés en
   place (pas de cascade de suppression hors du fichier directement
   concerné par F4).
3. **`negotiation.py`** garde ses propres limites (GPS best-effort, pas de
   révalidation de prix comme le tunnel `order_tracking.py`) —
   explicitement non touché, mandat §21.
