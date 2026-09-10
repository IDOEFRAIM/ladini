# Clôture de la frontière CART → CHECKOUT → PREORDER (2026-09-04)

Suite de l'audit `CART_CHECKOUT_PREORDER_AUDIT_2026-09-04.md`. Ce chantier
ne remet PAS en cause son verdict (`active_cart` = état éditable
pré-transactionnel, pas de `CartDraft`) — il ferme les gaps réels restants à
la frontière CART → CHECKOUT et verrouille le parcours complet par des tests
qui traversent les VRAIS nœuds, pas des appels isolés à `apply_domain_action`.

**Décision finale (section M) annoncée d'emblée** : `CART = KEEP`. Deux
correctifs ciblés au `CHECKOUT` (pas au panier). Zéro nouvelle abstraction.

---

## A. Architecture CART

`active_cart` reste ce que l'audit précédent a établi : un champ d'état
LangGraph (`Annotated[List[Dict], replace_list]`), jamais une entité SQL
dédiée (aucun modèle `Cart`/`CartItem` dans `domain/models.py`). Cycle de
vie réel, tracé nœud par nœud dans `flows/buyer/cart.py` :

| Opération | Où | Mécanisme |
|---|---|---|
| ADD | `cart_service.py::add_to_cart_with_ref` | Remplace la ligne existante du MÊME `product_id` (`cart = [c for c in cart if c.get("product_id") not in (...)]; cart.append(line)`) — pas d'append pur |
| UPDATE (changer quantité/palier d'un produit déjà au panier) | idem | Implicite via ADD — reparler du même produit RÉÉCRIT sa ligne, il n'existe PAS de goal `BUYER_UPDATE_CART` séparé |
| REMOVE (un seul article) | — | **N'existe pas** comme opération dédiée — confirmé par recherche exhaustive (`grep BUYER_REMOVE\|REMOVE_FROM_CART` → aucun résultat). Le seul retrait possible est le reset complet |
| CLEAR / VIEW | `cart_management` (`BUYER_VIEW_CART`) / `_cancel_preorder` (`BUYER_CART_RESET`) | Rendu pur / reset complet à `[]` |
| CHECKOUT | `flows/buyer/preorder.py::create_preorder` | Lit `active_cart` **une seule fois**, projette en `items_payload`, jamais relu après |

Aucune opération CART n'écrit dans une table SQL avant le checkout — c'est
la propriété centrale qui rend `CART = KEEP` correct : il n'y a rien à
verser sous CAS/version tant que rien n'est persisté.

## B. Frontière CHECKOUT — le snapshot exact créé

`create_preorder` (`preorder.py`) lit `active_cart` UNE fois, construit
`items_payload` (avec `tier_id` — correctif du 2026-09-04 précédent), et
appelle `bootstrap_preorder_draft` → `PreorderGateway.create_draft` (MCP,
protégé par `idempotency_key=creation_key(phone, cart_fingerprint(items))`)
→ `services/database/buyer.py::create_preorder_draft` (serveur) qui :

1. Relit `Product.price`/`Product.pricing_tiers` FRAIS en base (jamais le
   panier tel quel) — résolution prix/palier AUTORITATIVE.
2. **Désormais** (ce chantier) revalide le seuil minimum de commande contre
   `SubCategory` (voir section E) — écarte l'article si non atteint,
   jamais silencieusement.
3. Écrit `Order(status=DRAFT)` + `OrderItem` — AUCUN débit de stock à ce
   stade (documenté, volontaire : le brouillon n'engage rien).
4. Renvoie `items`/`total_amount` RÉELLEMENT écrits — c'est ce que
   `bootstrap_preorder_draft` utilise pour construire `PreorderDraft`
   (correctif du 2026-09-04 précédent), jamais `items_payload`/`meta` locaux
   sauf repli dégradé explicite.

**Ce que le serveur résout AVANT le snapshot** (mandat §6) : produit ✅,
palier ✅, prix ✅, quantité/unité ✅, seuil minimum ✅ (ce chantier). **Ce
qu'il NE résout PAS à cette étape** : stock, vendeur actif — **décision
assumée**, voir section F.

Après le snapshot, `PreorderDraft.items` devient la SEULE source pour tout
le reste du cycle (`preorder_confirmation.py::apply_response_plan`,
commentaire explicite : *"`active_cart` n'est PAS réécrit ici —
`plan.draft.items` reste la source canonique"*) — `active_cart` ne peut
plus, structurellement, faire dériver un draft déjà créé.

## C. Pricing tiers — résolution et propagation

Un seul modèle canonique, jamais recalculé divergemment :
`domain/pricing_tiers.py::resolve_tier`/`compute_line`/`resolve_stock_debit`.
Chaîne de propagation vérifiée de bout en bout par le nouveau test
`test_cart_to_checkout_full_journey.py` :

```
cart_service.add_to_cart_with_ref (résolution palier CLIENT, affichage)
  → cart line { tier_id, quantity=package_count, tier_quantity, base_unit_quantity, price, line_total }
  → preorder.py::items_payload (tier_id propagé)
  → create_preorder_draft (résolution palier SERVEUR, AUTORITATIVE — resolve_tier/compute_line réutilisés)
  → OrderItem.tier_id / base_unit_quantity / price_at_sale
  → confirm_preorder_draft::resolve_stock_debit(item) (débit RÉEL = base_unit_quantity, jamais package_count)
```

`package_count` (nombre de paquets) et `base_unit_quantity`/`effective
quantity` (quantité réelle) ne sont JAMAIS confondus à aucune étape — déjà
verrouillé par `tests/integration/test_tier_pack_count_separation.py`
(8 scénarios : héritage toxique de quantité, refus de "tetris" automatique,
re-sélection de palier, validation stock cohérente) et reconfirmé end-to-end
par le nouveau test de parcours complet.

## D. Quantité / conditionnement — représentation canonique

`quantity` = nombre de paquets (affichage), `base_unit_quantity` = quantité
réelle en unité de base (stock/paiement/politique). Ces deux axes ne se
substituent JAMAIS l'un à l'autre — `domain/order_policy.py` et
`resolve_stock_debit` lisent tous les deux explicitement
`base_unit_quantity` en priorité, `quantity` seulement en repli (produit
sans palier). Aucune divergence trouvée.

## E. Seuil minimum de commande — source et enforcement

Source canonique unique confirmée : `SubCategory.minimum_order_quantity` /
`minimum_order_unit` (`governance.sub_categories`), lue via LA MÊME fonction
pure `domain/order_policy.py::validate_minimum_order_quantity` à chaque
site — aucune règle concurrente trouvée côté agent.

**Gap réel trouvé et fermé** : cette revalidation n'existait, avant ce
chantier, qu'à l'AJOUT au panier (`cart_service.py::add_to_cart_with_ref`)
— jamais relue au moment du snapshot serveur faisant foi
(`create_preorder_draft`, le VRAI chemin de checkout). La seule
implémentation qui la refaisait correctement (`finalize_multi_order`) est du
code MORT, jamais appelée depuis le vrai chemin MCP (déjà identifié par
l'audit précédent). Un panier composé avant qu'un admin ne relève le seuil
pouvait ainsi produire une précommande en dessous du minimum EN VIGUEUR.

**Correctif** ([buyer.py](../src/ladini/services/database/buyer.py))
— `create_preorder_draft` revalide désormais chaque article contre
`SubCategory` (même requête, même fonction pure que la référence morte) ;
un article qui échoue est écarté (jamais sous-facturé) et journalisé dans
`unresolved_items`. Comme ce champ n'était surfacé nulle part côté
conversation (gap pré-existant plus large, voir section K),
`bootstrap_preorder_draft` a été corrigé en même temps pour préfixer un
avertissement explicite au récapitulatif dès que `unresolved_items` n'est
pas vide — sans quoi l'acheteur aurait vu un panier de 3 articles devenir
silencieusement une précommande de 2, exactement la classe de divergence
affichage≠exécution que ce chantier ferme.

## F. Stock — transaction et concurrence

Décision assumée : le débit de stock a lieu **exclusivement** à
`confirm_preorder_draft` (le VRAI point d'engagement), jamais au brouillon
— comportement documenté et volontaire (*"Les stocks ne sont PAS
décrémentés tant que la précommande n'est pas convertie"*). C'est
correct : le brouillon est annulable sans effet, la seule garantie qui
compte est qu'AUCUNE survente ne peut se produire au moment de la
conversion réelle.

`confirm_preorder_draft` :
- Verrouille l'`Order` via `SELECT ... FOR UPDATE`.
- Garde `status == "DRAFT"` (rejette tout doublon — voir section G).
- Verrouille chaque `Product` via `SELECT ... FOR UPDATE`, **trié par
  `product_id`** (correctif anti-deadlock déjà posé le 2026-09-04 précédent,
  RÉUTILISÉ tel quel de `finalize_multi_order`) — nouvellement **prouvé**
  par `tests/unit/test_confirm_preorder_draft_row_locking.py` (SQL
  réellement compilé, ordre d'acquisition vérifié différent de l'ordre du
  panier).
- Refuse la conversion entière (`insufficient_stock`) si UN SEUL article
  manque de stock — jamais de commande partielle silencieuse.

Compromis assumé et documenté : un brouillon peut légitimement afficher un
récapitulatif pour un article devenu hors-stock entre temps — l'acheteur ne
le découvre qu'au CONFIRM (rejet explicite, jamais une survente). C'est un
choix UX (early-warning absent au DRAFT), pas une faille de correction : la
propriété "jamais de survente" tient dans tous les cas.

## G. Paiement — idempotence et IPN

Deux mécanismes déjà réels et déjà audités (mandat PREORDER précédent,
`test_preorder_payment_state_machine.py`, `test_preorder_reconciliation_service.py`,
`test_preorder_payment_anti_legacy.py`, `chaos/test_preorder_payment_recovery_chaos.py`) :

1. **CAS applicatif** — `PreorderDraft` transite `DRAFT → EXECUTING` via
   `compare_and_swap` optimiste ; un second CONFIRM concurrent sur la même
   version échoue (`VERSION_CONFLICT`), relit l'état RÉEL, ne ré-exécute
   jamais (`TestInvariantH_DoubleConfirmExecutesExactlyOnce`,
   `TestInvariantI_ConcurrentUpdateConfirmNeverExecutesAStaleVersion` — ce
   dernier avec de VRAIS threads).
2. **Idempotence MCP générique** — `infrastructure/mcp/runtime.py`
   (`mcp_idempotency_store`) : `create_preorder_draft` protégé par
   `creation_key(phone, cart_fingerprint)` (retry avec le MÊME panier →
   REPLAY, pas un second `Order`), `confirm_preorder_draft`/
   `initiate_escrow_payment` protégés par `execution_key(draft)` — même clé
   pour tout retry d'exécution de la MÊME transition EXECUTING.

Aucun nouveau mécanisme créé — vérifié suffisant.

## H. Order/Preorder — passage de propriété des données

Avant le snapshot : `active_cart` est la seule source. Après
`bootstrap_preorder_draft` : `PreorderDraft.items`/`total_amount` deviennent
la seule source pour tout le reste du cycle — `active_cart` n'est plus lu
par `apply_response_plan`. Vérifié par `TestInvariantC_ActiveCartCannotDivergeFromDraftItems`
(existant) ET par le nouveau test de parcours complet (section I).

## I. User journey — test réel complet

Nouveau : `tests/integration/test_cart_to_checkout_full_journey.py` —
traverse les VRAIS nœuds (`cart_management`, `DomainRouter.decide`,
`create_preorder`, `bootstrap_preorder_draft`, `resolve_preorder_confirmation`,
`apply_domain_action`, CAS PostgreSQL simulé) sur le scénario exact de
l'incident historique (mandat §19) :

```
"je veux 30 L de lait" → menu paliers
  → "2" → palier 10L résolu, quantité pré-palier PURGÉE
  → "3" → panier : 3 bidons × 900 FCFA = 2700 FCFA, 30L au total
  → "confirmer" → checkout RÉEL (create_preorder_draft, tier_id transmis — vérifié)
  → GPS demandé (aucune localisation connue)
  → position partagée → confirm_preorder_draft → EXECUTED
```

Assertion finale : `tier_id`/`base_unit_quantity`/`price`/`line_total`
identiques à CHAQUE étape (ligne panier, snapshot checkout, draft confirmé
persisté) — zéro divergence.

## J. Web ↔ Agent

Ce dépôt (`Ladini/`) ne contient PAS l'application web (`frontend/`
local n'est qu'un prototype Gradio Python + un schéma Drizzle, aucun code
panier). L'app web réelle (`frontag`, référencée par la mémoire de session
précédente pour `orderPolicy.ts`) est un dépôt SÉPARÉ, hors de portée de
cet audit. Conséquence honnête : **il n'existe aucun panier partagé
Web↔Agent à auditer depuis ce dépôt** — la seule surface réellement
partagée est la base Postgres elle-même (`Order`/`OrderItem`/`Product`/
`SubCategory`), déjà canonique des deux côtés par construction (une seule
base). Un test de cohérence croisée nécessiterait le dépôt web — hors
périmètre technique de cette session, comme déjà noté pour la table de
conversion d'unités dans un chantier antérieur.

## K. Legacy

`active_cart` / `draft_payload` / `transaction_payload` — reconfirmés
(`helpers.py::capture_cart_draft`, `format_pending_draft`) comme trois rôles
distincts, jamais des doublons :

| Champ | Rôle | Justification |
|---|---|---|
| `active_cart` | **CANONICAL** | Seule source lue par `create_preorder`, jamais recalculée ailleurs |
| `draft_payload` | **DISPLAY PROJECTION** | Écho pur pour "en cours d'ajout" (`format_pending_draft`), jamais relu comme panier |
| `transaction_payload` | **EXECUTION ADAPTER** | Entrée brute du tour courant (`merge_dict`), consommée puis explicitement remise à `{"__reset__": True}` après un ADD réussi |

Rien supprimé — aucune duplication réelle prouvée, exactement le verdict de
l'audit précédent, reconfirmé sous cet angle de classification explicite.

**Gap identifié mais volontairement NON corrigé dans ce chantier** (hors
scope, faible sévérité, signalé séparément) : `_cancel_preorder`
(`preorder.py`) transitionne `PreorderDraft` vers `CANCELLED` mais n'appelle
jamais le tool MCP `cancel_preorder_draft` — l'`Order(status=DRAFT)` sous-
jacent reste orphelin en base indéfiniment (même chose à chaque cycle
"ajouter d'autres produits", déjà documenté comme comportement identique à
l'ancien code). Aucun risque de survente/double-facturation (DRAFT ne débite
jamais de stock) — pollution de données uniquement. Suggestion envoyée
séparément (`task_e6fd522f`).

`unresolved_items` (renvoyé par `create_preorder_draft` depuis le correctif
palier du 2026-09-04 précédent) n'était surfacé NULLE PART côté conversation
— fermé par ce chantier (section E).

## L. Tests

**Nouveaux, tous verts au premier essai** :
- `tests/unit/test_create_preorder_draft_minimum_order.py` (6 tests) — revalidation serveur du seuil minimum, palier ET tarif unique, panier mixte (un seul article écarté).
- `tests/unit/test_confirm_preorder_draft_row_locking.py` (2 tests) — `FOR UPDATE` réel sur `Order`/`Product`, ordre d'acquisition lexicographique prouvé différent de l'ordre du panier.
- `tests/architecture/test_bootstrap_preorder_draft_price_fidelity.py` (+2 tests) — avertissement explicite sur article écarté par le serveur, silence préservé si rien n'est écarté.
- `tests/integration/test_cart_to_checkout_full_journey.py` (1 test, parcours complet 5 tours) — reproduction directe du scénario historique, valeurs identiques à chaque étape.

**Coverage pré-existante réutilisée (non dupliquée)**, déjà suffisante pour :
- Double confirm / update+confirm concurrent (vrais threads) / retry idempotent création : `tests/architecture/test_preorder_transactional_contract.py`.
- Séparation quantité/palier/paquet (8 scénarios) : `tests/integration/test_tier_pack_count_separation.py`.
- Non-régression du bug historique "celui de 10 l" (bypass confirmation) : `tests/integration/test_pending_interaction_bug_scenario.py`.
- Paiement/IPN/escrow (état, anti-legacy, chaos récupération) : `tests/architecture/test_preorder_payment_state_machine.py`, `test_preorder_payment_anti_legacy.py`, `tests/chaos/test_preorder_payment_recovery_chaos.py`.

**Régression complète** : `pytest tests/ -q` — suite entière verte, mêmes 4
échecs préexistants (date codée en dur, `test_create_auction_catalog_gate.py`,
sans rapport avec ce chantier), **zéro nouvelle régression** introduite par
les 2 correctifs de ce chantier.

## M. Décision finale

| Composant | Décision |
|---|---|
| `active_cart` (le panier lui-même) | **KEEP** — état éditable pré-transactionnel confirmé, aucune transaction à protéger avant checkout |
| `draft_payload` / `transaction_payload` | **KEEP** — rôles distincts prouvés, aucune duplication |
| `create_preorder_draft` (snapshot checkout) | **CORRIGER CIBLÉMENT** — seuil minimum désormais revalidé serveur (section E) |
| `bootstrap_preorder_draft` (récapitulatif affiché) | **CORRIGER CIBLÉMENT** — articles écartés désormais explicitement signalés (section E) |
| `confirm_preorder_draft` (débit stock) | **KEEP**, robustesse prouvée par test (verrouillage + ordre anti-deadlock, déjà corrigé le 2026-09-04 précédent) |
| Paiement/IPN | **KEEP** — idempotence déjà réelle, suffisante |
| Web ↔ Agent | **N/A** — aucun panier partagé dans ce dépôt |

Aucun `CartDraft`/`CartVersion`/`CartConfirmationTarget` créé. Le vrai
objet transactionnel reste au `CHECKOUT`, comme le mandat l'exigeait : le
snapshot serveur y est désormais complet (produit, palier, prix, quantité,
**et** seuil minimum), et toute divergence entre ce snapshot et ce que
l'acheteur voit est désormais structurellement impossible à laisser
silencieuse.
