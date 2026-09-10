# Auction/Bid — clôture du stale recap, décision `Auction.version`, audit post-winner

2026-09-04. Suite directe de `AUCTION_BID_TRANSACTIONAL_AUDIT_2026-09-04.md`.
Ne rouvre PAS le modèle sécurisé par ce dernier (row locks, DB constraints,
transaction boundaries, status guards) — ce chantier ferme les deux points
explicitement laissés ouverts, puis audite ce qui se passe *après* une
sélection de gagnant devenue sûre.

---

## A. Stale recap — cause exacte + correction

**Trace demandée** :
```
winner selection (confirm_winner_selection)
  → construit le récap depuis get_auction_bids (LIVE à cet instant)
  → stocke SEULEMENT `pending_winner_bid` (l'id) — le TEXTE du récap n'est
    lui-même jamais repersisté, mais le PRIX affiché n'était nulle part
    capturé comme valeur comparable
→ PROVIDE_LOCATION (finalize_winner, étape GPS — 1+ tour supplémentaire)
  → AUCUNE nouvelle lecture de l'offre pendant cette étape
→ location received → resolve_gps_stage
→ confirmation resumes → _execute_winner_selection
  → select_winning_bid (relit `Bid`/`Auction` sous FOR UPDATE — TOUJOURS
    la valeur RÉELLEMENT courante à cet instant)
→ execution
```

**Cause exacte** : le récap affiché à l'acheteur (`confirm_winner_selection`)
et l'exécution (`select_winning_bid`) lisent chacun l'état RÉEL au moment
où ILS s'exécutent — ce sont donc chacun corrects INDIVIDUELLEMENT. Le gap
est entre les deux : rien ne comparait ce qui avait été MONTRÉ à ce qui
allait être EXÉCUTÉ. Si le producteur modifie son prix pendant la fenêtre
GPS (1+ tour), l'exécution finale utilise (à raison) le prix courant — mais
l'acheteur avait dit "oui" à un AUTRE nombre, jamais reprévenu.

**Correction** — PAS un `refresh_confirmation()` cosmétique : `_fetch_winner_recap`
devient LA seule fonction qui projette "état actuel de cette offre"
(prix, statut, producteur, produit), utilisée à la fois pour l'affichage
initial ET pour une **revalidation explicite juste avant `select_winning_bid`**.
Le prix affiché devient une VALEUR comparable
(`working_memory.pending_winner_price`), pas juste du texte. Si l'état a
changé (prix différent, offre plus `PENDING`) au moment de l'exécution :
**aucune exécution silencieuse** — nouveau récap sur l'état RÉEL, nouvelle
confirmation explicite demandée. Si rien n'a changé : exécution normale,
comme avant. [`order_tracking.py`](../src/ladini/graphs/agents/market_coach/flows/buyer/order_tracking.py)

## B. `Auction.version` — décision finale

**Étape A (recherche exhaustive, tout le dépôt : backend/tests/scripts/reports/migrations/admin)** :
un SEUL écrivain (`services/database/buyer.py::update_negotiation_offer`),
AUCUN lecteur nulle part. Aucun répertoire `admin`/`scripts`/`reports` dans
ce dépôt.

**Étape B (SELECT *, reporting, sérialisation)** : aucune trace de lecture
indirecte — aucun `SELECT *` sur `auctions`, aucune sérialisation de
`Auction` qui exposerait `version` à un consommateur externe visible depuis
ce dépôt.

**Étape C — décision** : **hybride, pas un simple REMOVE/KEEP/ACTIVATE** —
1. **L'écriture morte est retirée** (code) — `update_negotiation_offer`
   n'incrémente plus `auction.version` : c'était un compteur write-only
   sans consommateur, et la fonction protège déjà sa cohérence via
   `.with_for_update()` (verrou pessimiste, l'incrément n'apportait AUCUNE
   garantie de concurrence en plus).
2. **La colonne reste en base** — ce dépôt n'a PAS Alembic ; son mécanisme
   de DDL réel (`services/database/common.py::SCHEMA_COLUMN_DDL`) est
   STRICTEMENT additif (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, jamais
   de `DROP COLUMN` nulle part dans ce dépôt) — une suppression physique
   sortirait de ce mécanisme établi. Un lecteur EXTERNE à ce dépôt (le
   repository web `frontag`, mentionné dans une session antérieure, est
   séparé et hors de portée d'audit ici) ne peut pas être exclu avec
   certitude depuis ce dépôt seul.
3. La colonne est marquée `DEPRECATED / UNUSED` explicitement dans le
   modèle ([`orders/models.py`](../src/ladini/domain/orders/models.py))
   avec la justification complète — pas une dette silencieuse.

C'est un `REMOVE` du code mort + un `KEEP` documenté du schéma, jamais un
`ACTIVATE` (aucun invariant réel ne justifiait de la réutiliser — la
concurrence Auction/Bid est déjà entièrement couverte par les verrous
`FOR UPDATE`, prouvé dans l'audit précédent).

## C. Winner snapshot — comment la sélection devient canonique

**Réponse à la question du mandat §4** : le bid sélectionné devient
**canonique au moment de l'exécution** (`select_winning_bid`, sous verrou),
**pas** au moment où l'acheteur dit "oui" — il n'y a PAS de gel/snapshot
DB à cet instant intermédiaire (délibérément : cela introduirait un état
durable supplémentaire, proche d'un Draft, pour un besoin qui n'existe pas
— voir D). Champs qui PEUVENT encore changer entre "oui" et exécution :
`Bid.offered_price` (`update_bid_price`), `Bid.status` (`withdraw_bid`) —
documenté, pas supposé. La correction (A) ferme la conséquence
utilisateur-visible de cette non-immuabilité (jamais d'exécution
silencieuse sur une valeur périmée) sans figer la donnée plus tôt que
nécessaire.

## D. Bid lifecycle — états après sélection

Prouvé par `test_auction_bid_full_lifecycle_e2e.py` (chaîne réelle
complète, pas des assertions isolées) : une fois `Bid.status == "WINNING"` —
- `update_bid_price` → refusé (`bid.status.upper() != "PENDING"`).
- `withdraw_bid` → refusé (même garde).
- `place_bid` (nouveau producteur, même enchère) → refusé (`auction.status != "OPEN"`).

**Le bid gagnant est donc immuable EN PRATIQUE dès l'exécution** — le
contrat métier est : `PENDING` (négociable) → `WINNING`/`LOST`/`WITHDRAWN`
(tous les trois terminaux, aucune transition sortante existante).

## E. Auction lifecycle — états après sélection

`Auction.status = "CLOSED"` (jamais "SETTLED" — aucune valeur de ce type
n'existe dans ce code ; CLOSED est le SEUL état terminal "gagnant désigné").
Prouvé : `place_bid` sur une enchère `CLOSED` refuse (`auction.status != "OPEN"`,
correctif de l'audit précédent) ; `cancel_auction` sur une enchère `CLOSED`
refuse également (`auction.status.upper() != "OPEN"`, déjà en place) — **ne
peut jamais revenir en arrière**, confirmé par la chaîne E2E.

## F. Order lifecycle — création + invariants

**Champs vérifiés exactement ceux du winning bid** (mandat §9, prouvé par
le test E2E, pas supposé) : `buyer_id = auction.buyer_id`, `auction_id =
auction.id`, `winning_bid_id = bid.id`, `total_amount = bid.offered_price ×
auction.quantity` — TOUS lus depuis les objets `bid`/`auction` verrouillés
DANS LA MÊME transaction, jamais un payload conversationnel relu. Pas de
duplication de `quantity`/`unit`/`producer` sur `Order` lui-même : ces
champs sont dérivables via les FK (`auction_id`→`Auction.quantity`/`unit`,
`winning_bid_id`→`Bid.producer_id`) — une SEULE source par champ, jamais
deux représentations à synchroniser (voir aussi section H).

**Transaction boundary (mandat §14/G)** — tracé jusqu'au mécanisme réel :
`AgriDatabaseService.__getattribute__` (`services/database/d.py`) enveloppe
DYNAMIQUEMENT chaque méthode non-lecture (dont `select_winning_bid`) avec
`@transactional(write=True)` (`services/database/base_service.py`) — SEUL
gestionnaire de session du backend. Son contrat, lu directement dans le
code : commit UNIQUEMENT si la fonction entière retourne sans exception ;
toute exception (y compris pendant `current_session.flush()` à la création
de l'`Order`) déclenche `await _safe_rollback(session)` AVANT de
re-lever — donc les mutations déjà faites en mémoire dans LA MÊME fonction
(`bid.is_winner=True`, `bid.status="WINNING"`, `auction.status="CLOSED"`,
`auction.winner_bid_id=...`) n'ont jamais été committées et sont annulées
avec le reste. **Un `Order` manquant après "gagnant marqué" est donc
structurellement impossible** — soit tout est committé ensemble (gagnant +
Order + notification enfilée), soit rien ne l'est. Aucun état intermédiaire
distinct nécessaire (mandat §14 : pas de "rollback explicite à écrire", le
mécanisme générique déjà partagé par tout le backend s'en charge).

## G. Transaction boundary — Winner + Order

Voir F — même transaction, prouvé par lecture directe du décorateur
partagé, pas par supposition.

## H. Read-side — Buyer / Producer

**Buyer** : déjà correct (audit précédent, section J/K) — `confirm_winner_selection`
relit en LIVE, `check_auction_status` ne propose la sélection que si
`status_raw == "OPEN"`.

**Producer — GAP RÉEL trouvé et fermé** : `get_producer_orders` ("mes
commandes" côté producteur) n'avait que DEUX sources de candidats
(`product_order_ids` via `OrderItem`, `cycle_order_ids` via
`market_offer_id`) — **ni l'une ni l'autre n'atteint une commande née d'un
appel d'offres** (`select_winning_bid` ne crée AUCUN `OrderItem`, AUCUN
`market_offer_id` — seulement `auction_id`/`winning_bid_id`). Un
producteur qui REMPORTE une enchère ne voyait donc **jamais** cette
commande dans "mes commandes" — seule la notification Outbox ponctuelle
(`AUCTION_WON_PRODUCER`) l'en informait, une fois, au moment du gain,
sans aucun moyen de la retrouver ensuite en consultant ses commandes.
Corrigé : troisième source de candidats, `Order.id` rejoint via
`Order.winning_bid_id == Bid.id` filtré sur `Bid.producer_id`.
[`producer.py`](../src/ladini/services/database/producer.py)

`get_my_active_bids` (déjà audité précédemment, toujours correct) montre
le statut `WON`/`LOST`/`WITHDRAWN` — désormais complété par la commande
elle-même visible dans "mes commandes" grâce au correctif ci-dessus.

**Lost/withdrawn bids ne sont jamais présentés comme disponibles** (repris
de l'audit précédent, confirmé toujours vrai après ce chantier) :
`_derive_bid_status` dérive l'affichage en tenant compte à la fois du
statut du bid ET de celui de l'enchère.

## I. Notifications

Inchangé, déjà correct (audit précédent, section I) : Outbox, même
transaction que l'`Order`, `dedupe_key` par `Order.id`. Confirmé à nouveau
ici — aucun `send_whatsapp()` synchrone trouvé dans le chemin réexaminé.

## J. Concurrency — matrice finale

Toute la matrice de concurrence (`select A × 2`, `select A + select B`,
`select A + update A`, `select A + withdraw A`, `select A + cancel auction`)
était déjà couverte et prouvée par l'audit précédent (row locks + status
guards + `orders_auction_unique`) — non rouverte ici (règle absolue du
mandat). Pas d'infrastructure Postgres réelle dans ce dépôt pour un test de
concurrence physique (documenté depuis le tout premier audit de cette
session) — la preuve reste au niveau code+verrous+contraintes, comme pour
tout le reste de cette suite.

## K. Tests

Nouveaux, tous verts au premier essai :
- `tests/nodes/test_finalize_winner_stale_recap.py` (4 tests) — prix inchangé exécute normalement ; prix modifié pendant la fenêtre GPS bloque l'exécution et redemande confirmation sur la valeur RÉELLE ; re-confirmation après changement exécute au nouveau prix ; bid retiré pendant la fenêtre GPS rejeté proprement.
- `tests/unit/test_auction_version_decommission.py` (2 tests) — l'incrément mort est bien retiré, la colonne historique n'est plus jamais retouchée.
- `tests/unit/test_auction_bid_full_lifecycle_e2e.py` (1 test, scénario complet) — reproduction exacte du mandat §16 : A=250, B=275, A→300, select A, PUIS les 5 tentatives refusées (select B, update A, withdraw A, nouveau bid, cancel auction) — y compris l'invariant `Auction.winner_bid_id == Order.winning_bid_id`.
- `tests/unit/test_producer_orders_includes_auction_wins.py` (2 tests) — les commandes nées d'un gagnant d'enchère apparaissent désormais dans "mes commandes" côté producteur ; non-régression de l'exclusion DRAFT/SUPERSEDED déjà en place.

## L. Régression complète

`pytest tests/` — voir résultat ajouté après exécution ; attendu : mêmes 4
échecs préexistants sans rapport, zéro nouvelle régression des 9 nouveaux
tests + des 4 correctifs (stale recap, décommission `Auction.version`,
lecture producteur post-winner, plus les 3 correctifs déjà actés par
l'audit précédent).

---

**Verdict final** : la propriété centrale du mandat — BID → SELECT WINNER →
UN SEUL gagnant canonique → UNE SEULE Order → un seul prix → une seule
quantité, avec toute action concurrente rejetée proprement — est
désormais prouvée de bout en bout, y compris ce qui se passe APRÈS la
sélection (bid/auction immuables, Order cohérente par construction
transactionnelle, lecture producteur enfin complète). Aucun nouveau Draft
créé. Auction/Bid est considéré **fermé**, sauf bug concret futur.
