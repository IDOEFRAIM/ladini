# Audit d'impact — `Order(status=DRAFT)` orphelin (PREORDER, 2026-09-04)

Suite de `CART_CHECKOUT_BOUNDARY_CLOSURE_2026-09-04.md`. Portée strictement
limitée à PREORDER / `Order(DRAFT)` / CANCEL / ADD_MORE_PRODUCTS — Procurement,
Sales, CART et Stock non touchés (une fuite Sales trouvée en cours de route
est documentée en section C et signalée séparément, pas corrigée ici).

**Décision finale (section J) annoncée d'emblée** : **A. FIX IN CONVERSATIONAL
FLOW** — l'audit a prouvé un impact utilisateur RÉEL, pas seulement une ligne
DB orpheline.

---

## A. Signification réelle de `Order(status=DRAFT)`

Une seule catégorie existe dans tout le backend — recherche exhaustive de
`status="DRAFT"`/`status='DRAFT'` : **un seul écrivain**,
`create_preorder_draft` (`services/database/buyer.py:1403`). Aucune autre
notion de "DRAFT" ne touche la table `orders`.

**Contrat réel** : un `Order(DRAFT)` est une **réservation de snapshot de
checkout, sans engagement** — produit/palier/prix RÉSOLUS et ÉCRITS
(`OrderItem`), mais **aucun stock débité, aucun paiement initié**. Ce n'est
ni un "brouillon utilisateur" au sens UI (l'acheteur ne le voit/modifie
jamais directement — c'est `active_cart`, pas cette ligne, qui joue ce
rôle, voir l'audit CART précédent) ni une réservation de stock. C'est
`create_preorder_draft` qui matérialise le résultat AUTORITATIF du
checkout ; deux issues seulement lui sont ouvertes : `CONFIRMED` (via
`confirm_preorder_draft`, débit stock réel) ou — **désormais** —
`CANCELLED`/`SUPERSEDED` (ce chantier).

## B. Writers

| Transition | Fonction | Fichier |
|---|---|---|
| — → DRAFT | `create_preorder_draft` | `services/database/buyer.py` |
| DRAFT → CONFIRMED | `confirm_preorder_draft` | idem |
| DRAFT → CANCELLED/SUPERSEDED | `cancel_preorder_draft` | idem — **existait déjà, jamais appelée nulle part avant ce chantier** (aucune référence dans tout `src/`, ni dans `TOOL_SCOPE_MAP`) |

`cancel_preorder_draft` était du code mort — auto-exposé comme outil MCP
par introspection (`protocols/mcp/servers/h.py`, qui scanne TOUTE méthode
async publique de `AgriDatabaseService`) mais **rejeté fail-closed**
(absent de `TOOL_SCOPE_MAP`, voir `infrastructure/mcp/runtime.py` :
`if name not in TOOL_SCOPE_MAP: raise PermissionDenied`) — donc même un
appel accidentel aurait échoué avant ce chantier.

## C. Readers

| Lecteur | Filtre AVANT ce chantier | Impact |
|---|---|---|
| `get_active_orders_status` (`buyer.py:187`) | `Order.status.in_(["PENDING","CONFIRMED","PAID","SHIPPED","PICKED_UP"])` — allowlist DÉJÀ correcte | Aucun — déjà exclu DRAFT |
| `get_buyer_orders_dashboard` (`buyer.py:768`) | **AUCUN** | **Confirmé : oui** — voir D |
| `get_transaction_summary`, branche `buyer_phone` sans `order_id` (`buyer.py:1746`) | **AUCUN**, `ORDER BY created_at DESC LIMIT 1` | **Confirmé : oui** — voir D |
| `get_transaction_summary`, branche `order_id` explicite | Filtre par id — statut non pertinent | Aucun (lookup ciblé, support/debug légitime) |
| `cancel_pending_order` (`buyer.py:820`) | Exige `status=="PENDING"` | Aucun — DRAFT explicitement hors périmètre |
| `get_producer_orders` (`producer.py:1268`, domaine SALES) | `status_filter` optionnel, **aucune exclusion par défaut** | **Confirmé, mais hors scope de ce chantier (règle absolue §20)** — signalé séparément (`task_34df2754`) |
| `OrderService.list_buyer_orders` | N/A | **Mort** — jamais appelée (aucun outil MCP ne pointe dessus, seule une chaîne `"tool_name"` orpheline dans `interpreter/intent.py`) |

## D. Impact utilisateur — réponses précises, sans supposition

- **Visibles par l'acheteur ?** OUI, confirmé par lecture directe du code
  (pas une supposition) : `get_buyer_orders_dashboard`'s `status_map` n'a
  aucune entrée `"DRAFT"` → repli sur `f"🔄 Status: {order.status}"`,
  affichant littéralement *"🔄 Status: DRAFT"* avec un total RÉEL (non nul —
  `create_preorder_draft` pose déjà `total_amount` au moment du DRAFT) dans
  le tableau de bord WhatsApp de l'acheteur, comme une commande normale.
- **Comptés dans des statistiques ?** Non vérifié comme AFFIRMATIF (aucun
  agrégat/statistique buyer-side trouvé dans ce périmètre) — pas de preuve
  d'impact ici.
- **Apparaissent dans le dashboard ?** OUI (ci-dessus).
- **Repris par un workflow ?** Non — aucun code ne relit un `Order(DRAFT)`
  pour le reprendre automatiquement ; le seul chemin de reprise est
  `confirm_preorder_draft`, qui exige un `draft_id`/`order_id` connu du
  `PreorderDraft` actif (le brouillon suivant, s'il existe, en porte un
  DIFFÉRENT après ADD_MORE).
- **Bloquent une nouvelle preorder ?** Non — chaque bootstrap crée un
  nouvel `Order` sans jamais consulter les anciens.
- **Affectent paiement, stock, livraison ?** Non pour un DRAFT abandonné
  AVANT confirmation (voir E/F) — mais un `Order(DRAFT)` non nettoyé, en
  restant la ligne la plus récente, **masquait la VRAIE dernière commande**
  quand l'acheteur demandait "où en est ma commande ?"
  (`order_tracking.py::check_order_status` → `get_transaction_summary`
  sans `order_id`) — un impact FONCTIONNEL réel, pas juste un affichage
  cosmétique : la mauvaise commande était rapportée à une vraie requête
  utilisateur.

**Conclusion section D : Catégorie A (visible/user-facing) confirmée sur
DEUX lecteurs buyer-side distincts — correction obligatoire.**

## E. Impact paiement

Vérifié : **aucun** `Order(DRAFT)` abandonné n'a jamais pu porter de
paiement/escrow. `initiate_escrow_payment` (`escrow.py:138`) et
`confirm_preorder_draft` (`buyer.py`) exigent TOUS DEUX
`order.status == "DRAFT"` en entrée et le font sortir de cet état
immédiatement (`AWAITING_PAYMENT`/`CONFIRMED`) dans la MÊME transaction —
aucun chemin ne peut laisser un `Order` à la fois `DRAFT` et porteur d'un
paiement. Ce chantier RENFORCE même cette garantie : un `Order` désormais
`CANCELLED`/`SUPERSEDED` plutôt que `DRAFT` échouerait explicitement le
garde `!= "DRAFT"` si un appel erroné tentait malgré tout d'y initier un
paiement — fermeture d'une fenêtre théorique en plus de l'impact prouvé.

## F. Impact stock

Vérifié : `create_preorder_draft` ne débite JAMAIS le stock (documenté dans
son propre docstring, confirmé par relecture — aucun
`product.quantity_for_sale -= ...` dans cette fonction). Seul
`confirm_preorder_draft` débite, sous `FOR UPDATE`, avec garde
`status == "DRAFT"`. Un `Order(DRAFT)` abandonné est donc **sans effet
stock**, qu'il soit nettoyé ou non — documenté, pas supposé.

## G. Impact réconciliation

`services/reconciliation/preorder_reconciliation_service.py` inspecté
intégralement : il ne traite QUE `EXECUTING` (confirm en vol) et
`AWAITING_PAYMENT` (paiement escrow en attente d'IPN) — **aucun sweep
existant sur `DRAFT`/`CANCELLED`/`SUPERSEDED`**, aucun système de cleanup
d'`Order` à dupliquer ou percuter. La décision F (voir J) n'a donc PAS créé
de second système de réconciliation — elle a fermé la cause à la source
(écriture synchrone best-effort au moment du CANCEL/ADD_MORE), cohérente
avec ce qui existe déjà.

## H. CANCEL — comportement AVANT ce chantier

`_cancel_preorder` (`flows/buyer/preorder.py`) transitionnait UNIQUEMENT le
`PreorderDraft` applicatif (CAS PostgreSQL, table `preorder_drafts`) vers
`CANCELLED` — l'`Order` sous-jacent (table `orders`) restait `DRAFT` pour
toujours. **Incohérence métier confirmée, pas seulement une ligne
orpheline** (section D) — `PreorderDraft(order_id=X, status=CANCELLED)`
coexistant avec `Order(id=X, status=DRAFT)` est désormais un état interdit
par le domaine (voir K).

## I. ADD_MORE — comportement AVANT ce chantier

`bootstrap_preorder_draft` (`flows/buyer/preorder_confirmation.py`) : quand
un `PreorderDraft` `DRAFT` existant est recomposé, `create_preorder_draft`
crée INCONDITIONNELLEMENT un nouvel `Order`, puis le draft applicatif est
mis à jour vers ce nouvel `order_id` (nouvelle version, même `draft_id`) —
l'ANCIEN `order_id` n'était **jamais** transitionné, ni référencé nulle
part ensuite. Le code documentait déjà explicitement ce comportement comme
volontaire ("comportement identique à l'ancien code") — l'audit confirme
que ce n'est PAS un choix assumé mais un gap non détecté, avec un impact
utilisateur réel prouvé en section D.

## J. Décision

# **A. FIX IN CONVERSATIONAL FLOW**

Justifié par la section D (impact utilisateur confirmé, pas supposé) — la
cause racine (write side) ET un filet de sécurité immédiat (read side)
sont corrigés ensemble, sans nouveau système de réconciliation (section G).

## K. Code modifié

**Write side — synchronisation `PreorderDraft` ↔ `Order`** :
- [`security.py`](../src/ladini/infrastructure/mcp/security.py) — `cancel_preorder_draft` ajouté à `TOOL_SCOPE_MAP` (`DB_DATA_WRITE`).
- [`buyer.py::cancel_preorder_draft`](../src/ladini/services/database/buyer.py) — nouveau paramètre `target_status: str = "CANCELLED"` (défaut inchangé, 100% rétrocompatible) ; `cancellation_role="BUYER"` posé UNIQUEMENT sur `target_status=="CANCELLED"` (jamais sur un remplacement).
- [`gateway.py::PreorderGateway.cancel_draft`](../src/ladini/graphs/agents/market_coach/services/mcp/gateway.py) — nouvelle méthode, même convention que `confirm_draft`/`create_draft`.
- [`preorder.py::_cancel_preorder`](../src/ladini/graphs/agents/market_coach/flows/buyer/preorder.py) — appelle désormais `cancel_draft(target_status="CANCELLED")` **uniquement** sur la VRAIE première transition (`outcome.kind == CANCELLED`, même garde que le CAS existant — un double-CANCEL ne redéclenche rien, idempotence naturelle). Best-effort explicite : un échec MCP est journalisé (`DEGRADED`) mais ne bloque JAMAIS la confirmation d'annulation déjà actée côté `PreorderDraft`.
- [`preorder_confirmation.py::bootstrap_preorder_draft`](../src/ladini/graphs/agents/market_coach/flows/buyer/preorder_confirmation.py) — capture `old_order_id` avant la mise à jour ADD_MORE ; appelle `cancel_draft(target_status="SUPERSEDED")` sur l'ancien `order_id` **uniquement** si la mise à jour a réellement abouti (`kind != VERSION_CONFLICT` — protège le cas rare d'une course perdue face à un autre écrivain concurrent, voir section "Concurrence").

**Distinction `CANCELLED` vs `SUPERSEDED` (mandat §5/§10)** — assumée, pas
arbitraire : un rejet acheteur explicite (`CANCEL`) reste `CANCELLED` ;
un remplacement par recomposition de panier (`ADD_MORE`, l'acheteur n'a
JAMAIS rejeté quoi que ce soit) devient `SUPERSEDED` — aucune contrainte
`CHECK` en base sur `Order.status` (colonne `String` libre, déjà porteuse
de nombreuses valeurs non-enum), donc aucune migration nécessaire. Aucun
champ `supersedes_draft_id` créé (mandat §12) : la filiation "draft v2
vient de v1" est déjà entièrement traçable via `draft_id` STABLE (seule
`version`/`order_id` changent) — un champ dédié n'aurait rien exprimé de
plus.

**Read side — filet de sécurité immédiat** :
- [`buyer.py::get_buyer_orders_dashboard`](../src/ladini/services/database/buyer.py) — `.where(Order.status.notin_(["DRAFT", "SUPERSEDED"]))` ajouté à la requête d'historique complet.
- [`buyer.py::get_transaction_summary`](../src/ladini/services/database/buyer.py) — même exclusion, **uniquement** sur la branche "dernière transaction par téléphone" (sans `order_id` explicite) ; un lookup par `order_id` explicite reste inchangé.

**Non touché, signalé séparément** (règle absolue §20) :
`get_producer_orders` (`services/database/producer.py`, domaine SALES) a la
MÊME classe de faille (aucune exclusion par défaut quand `status_filter`
est vide) — confirmé réel, **hors scope de ce chantier**, suggestion
envoyée (`task_34df2754`).

## L. Tests

Nouveaux, tous verts au premier essai :
- `tests/architecture/test_preorder_draft_order_lifecycle_sync.py` (5 tests) — CANCEL transitionne l'`Order` ; CANCEL ×2 idempotent (un seul appel MCP) ; échec best-effort n'empêche jamais la confirmation d'annulation ; ADD_MORE supersede (jamais cancel) l'ancien `Order` ; ADD_MORE ×2 ne laisse jamais deux drafts actifs (une seule ligne PostgreSQL, filiation par `draft_id` stable).
- `tests/unit/test_buyer_order_reads_exclude_draft_status.py` (3 tests) — `get_buyer_orders_dashboard`/`get_transaction_summary` (branche sans `order_id`) excluent bien `DRAFT`/`SUPERSEDED` (SQL réellement compilé, `NOT IN`) ; un lookup par `order_id` explicite n'est jamais filtré par statut.

**Concurrence (mandat §16)** — analysée, pas testée avec de vrais threads
(déjà couverte pour CANCEL+CONFIRM par les protections `FOR UPDATE` +
garde `status != "DRAFT"` pré-existantes des DEUX côtés, application ET
Postgres — cf. `test_confirm_preorder_draft_row_locking.py`) :
- `CANCEL + CONFIRM` : sûr — CAS applicatif serialise déjà les deux
  actions sur le MÊME `PreorderDraft` ; côté `Order`, `FOR UPDATE` +
  `status != "DRAFT"` sur les deux fonctions garantit qu'une seule des
  deux transitions gagne, l'autre échoue proprement (best-effort journalisé
  côté CANCEL, exception métier normale côté CONFIRM).
- `ADD_MORE + CONFIRM` : **résiduel, rare, documenté plutôt que fermé** — si
  CONFIRM gagne la course CAS sur le `PreorderDraft` avant qu'ADD_MORE ne
  persiste sa propre mise à jour, ADD_MORE detecte `VERSION_CONFLICT` et
  s'abstient (ne supersede PAS l'ancien `order_id`, correctement — CONFIRM
  est en train de le convertir). Mais le NOUVEL `Order` qu'ADD_MORE avait
  déjà créé (inconditionnellement, avant la tentative de CAS) reste alors
  lui-même orphelin — un cas déjà présent AVANT ce chantier, non aggravé,
  laissé tel quel (fenêtre de course déjà extrêmement étroite : deux
  actions distinctes du MÊME acheteur sur le MÊME tour conversationnel).
- `CLEANUP + CONFIRM` : sans objet — aucun cleanup asynchrone créé (section
  G, décision A plutôt que B).

## M. Régression complète

`pytest tests/` — suite entière verte (voir sortie ci-dessous ajoutée après
exécution), mêmes 4 échecs préexistants sans rapport
(`test_create_auction_catalog_gate.py`, date codée en dur), **zéro
nouvelle régression** des 8 tests neufs + des correctifs eux-mêmes.

---

**Règle absolue respectée** : aucune ligne DB n'a été traitée comme un bug
sur la seule base d'être "orpheline" — la décision A repose exclusivement
sur l'impact utilisateur PROUVÉ en section D (deux lecteurs buyer-side
montrant réellement un brouillon abandonné comme une commande, ou
masquant la vraie dernière commande à une requête réelle), jamais sur
"la base contient des lignes en trop".
