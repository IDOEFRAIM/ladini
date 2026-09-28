# Buyer direct-purchase flow — audit et correction structurelle

Statut : 2026-09-28. Incident réel WhatsApp : recherche « boeufs » → `photo 3` → `3` →
`Je veux 461000` → « Stock insuffisant » → `20` → confirmation → appel d'offres.
Méthode : reproduire AVANT de corriger (tests sur le VRAI graphe compilé, vérifiés en échec sur
l'ancien code), tracer la chaîne exacte, corriger l'invariant violé, ne pas patcher le texte.

## Verdict

Voir la dernière section (« Verdict et périmètre »).

## Les quatre défauts, diagnostiqués séparément

| | Défaut | Invariant violé | Statut |
|---|---|---|---|
| A | identité d'offre : `3` → offre B au lieu de C | I1, I3, I4 | **corrigé** |
| B | `photo 3` | I2 | **innocent — prouvé** (aucune modification à apporter à son comportement) |
| C | `20` ne remplace pas `461000` | I5, I6 | **corrigé** |
| D | `oui`/confirmation → appel d'offres | I7, I8 | **corrigé** |

### A — Cause racine : le menu liste des OFFRES, la sélection résolvait un PRODUCTEUR

Chaîne exacte (fichier:rôle) :

1. `services/domain/cart_service.py::resolve_product_vendors` : une ligne par `product_id`
   (dédup sur `product_id:producer_id`). B et C sont deux lignes distinctes ✔.
2. `build_product_selection_menu` : `MenuOption.value = producer_id`.
3. `domain/selection_actions.py::build_selection_context` : `ProducerOption(producer_id, label)`
   — **aucun identifiant d'offre**. Le fast-path (`fast_path_action`) traduisait l'index affiché en
   `producer_id` (`« 3 » → P2`), et le micro-prompt LLM (`structured_action_contract`) faisait la
   même traduction.
4. `flows/buyer/cart.py::_execute_selection_action` (SELECT_PRODUCER) :
   `next(v for v in vendors if v.producer_id == action.producer_id)` → **la PREMIÈRE offre de P2 = B**.

Preuve : sans aucun appel LLM, `3` produisait « Vous avez choisi *Gilbert-prod* … prix : 450000.0 »
(`chosen_vendor.product_id == B`). Le log `selection_index=3` était correct ; c'est l'étape
`index → producer_id → première offre` qui perdait l'information. Le chemin heuristique
`selection_index` (`vendors_list[idx-1]`) était, lui, correct — seul le chemin d'action structurée
(prioritaire) était faux.

Aucun off-by-one : les index sont 1-basés partout et cohérents. **Mais** un second défaut latent du
même mécanisme : `build_selection_context` **écartait** les lignes sans `producer_id`, ce qui
décalait tous les index suivants par rapport au menu affiché (corrigé : plus aucun filtre,
`display_index` explicite).

**Conception avant** : index → `producer_id` → première correspondance.
**Conception après** :
- chaque ligne du menu est estampillée `offer_id` (= `product_id`, ou repli positionnel stable
  `producteur#rang` si le catalogue n'a renvoyé aucun id) et `display_index`, plus un `menu_id`
  figé dans `vendor_selection_context` (`BUYER_SEARCH_SNAPSHOT_CREATED`) ;
- `ProducerOption` porte `display_index` + `offer_id` ; fast-path et micro-prompt LLM émettent
  `offer_id` ; `SelectionAction.offer_id` est le seul identifiant que `cart.py` résout ;
- contrat historique (`producer_id` seul) : accepté **uniquement** si non ambigu dans le menu, sinon
  rejeté (« clarifier, jamais deviner-et-écrire ») ;
- `MenuOption.value = offer_id`.
Aucune classe nouvelle n'a été introduite hors ce qui existait (`ProducerOption`/`SelectionAction`).

### B — `photo 3` est innocent (prouvé, pas supposé)

- `photo N` est intercepté au niveau du webhook (`whatsapp_webhook.py` §4quater), **avant** le graphe :
  `send_search_result_photos_task` → `_send_search_result_photos`. Il ne lit que le cache Redis
  `last_search_results:{phone}` et n'écrit **rien** : ni `PendingInteraction`, ni
  `vendor_selection_context`, ni `expected_candidates`, ni panier, ni cache.
- Test `test_photo_3_shows_offer_C_photos_and_changes_no_graph_state` : l'empreinte de l'état de
  sélection est identique avant/après, le cache identique, les photos sont celles de C. **Ce test
  passe aussi sur l'ANCIEN code** : le bug de sélection existe sans aucune commande photo.
- État avant `photo 3` = état après `photo 3` (empreinte JSON identique sur
  `vendor_selection_context`, `pending_interaction` `SELECTION_MENU`, `current_goal`,
  `transaction_payload`, `active_cart`, `working_memory`).
- État après `3` (ancien code) : `chosen_vendor = B` (450000, stock 20). Après correctif : `C`
  (461000, stock 461000).
- Ajouts sans changement de comportement : `menu_id` + `offer_id` dans chaque entrée du cache photo,
  et log `BUYER_PHOTO_PREVIEW` (menu_id, option_index, product_id).
- Limite connue (non corrigée, documentée) : le cache photo est une clé unique par téléphone
  (TTL 30 min) réécrite à chaque construction de menu ; `photo N` se résout donc contre le DERNIER
  menu construit, pas contre le menu à l'écran si un autre menu a été construit entre-temps. La
  tâche Celery ne peut pas lire l'état du graphe pour comparer les `menu_id`. Aucun incident
  observé ; risque faible.

### Pagination : page 2 « vide »

Cause : `paginate_item_blocks` comptait les **pieds de page** (prompt « Répondez avec le numéro »,
astuce `photos <numéro>`, `post_hint` « appel ») comme des « éléments » (max 5). 3 offres + 2 pieds
= page 1 pleine ; le 3ᵉ pied atterrissait seul en « Page 2/2 ». Le découpage est purement
**d'affichage** (le numéro de chaque option est écrit dans son bloc `*N.*`) : il ne peut pas changer
un index. Correctif : `footer_blocks` attachés à la dernière page sans compter dans `max_items`.

### C — Pourquoi `20` ne remplaçait pas `461000`

Après la rupture, `cart_service` ne mémorisait que `payload_seed = {product, quantity=461000,
unit}` et 5 drapeaux épars de `working_memory`, et remettait `vendor_selection_context = None`.
Le tour `20` : l'interpréteur (état `CONFIRM_ACTION`) ne savait pas lire un nombre → `UNKNOWN` →
`cognitive_guard` → `response_strategy` **sans jamais passer par `buyer_request_resolver`** →
récap générique « Confirmez-vous … 461 000 TETE » construit depuis le `transaction_payload`
périmé. La quantité demandée restait autoritaire, et l'offre n'était plus mémorisée nulle part.

Champ canonique de quantité (Phase 9) : pour le flux acheteur, la seule quantité consommée par la
logique métier est `transaction_payload["quantity"]` (`flows/buyer/helpers.py::resolve_quantity`)
→ argument `qty` de `add_to_cart_with_ref` → ligne de panier `quantity` → `create_preorder_draft`.
Les alias (`quantity_for_sale`, `qty`, `quantite`, `volume`, `quantity_kg`, `action_quantity`…)
n'existent que dans la couche NLP (`core/slots.py`, `interpreter`, `services/mcp/schema_resolver.py`)
et ne pilotent aucune écriture acheteur. Pour la rupture : `purchase_quantity` est passé
explicitement (jamais relu depuis `stable_entities`), `original_requested_quantity` est historique.

### D — Pourquoi la confirmation devenait un appel d'offres

Le drapeau `buyer_request_waiting_choice=True` (posé par la rupture) restait actif ; tout tour
ultérieur était **re-déduit** : un `oui` (CONFIRM) était lu par `buyer_request_resolver` comme
« lancer l'appel d'offres » (`_escalate`) alors que l'utilisateur venait de choisir la prise
directe. `vendor_selection_context` valait `None` parce que `add_to_cart_with_ref` le remet à `None`
sur le chemin de rupture — l'offre choisie n'existait plus pour être reprise.

## Correctif structurel C/D : `StockShortageDecision` (`domain/stock_shortage.py`)

État explicite `working_memory["stock_shortage"]` posé au moment de la rupture :
offre exacte (snapshot), `original_requested_quantity` (HISTORIQUE), `available_quantity`, unité,
`menu_id`. La question est liée à la décision par `PendingInteraction.target =
{"kind": "STOCK_SHORTAGE", "product_id": …}` : un dict résiduel + une confirmation sans rapport ne
peuvent jamais capter un nombre nu (`shortage_awaiting_reply`).

- **Réponse numérique nue** (générique, aucune valeur en dur — testé avec 7, 2.5, 12, 40) : décidée
  **sans LLM** dans l'interpréteur (étape 0.4) → `TAKE_AVAILABLE`, `purchase_quantity = N`.
- `buyer_request_resolver` traite la décision AVANT toute autre lecture d'état :
  - `TAKE_AVAILABLE` : reprend l'OFFRE EXACTE du snapshot, appelle `add_to_cart_with_ref`
    (revalidation du stock : si le stock a de nouveau baissé, une nouvelle rupture est posée),
    efface la décision et l'interaction en attente. Le flux ne passe jamais par
    `PROCUREMENT_CREATE_REQUEST`.
  - `START_TENDER` (`oui`) : la quantité ORIGINALE est conservée pour l'appel d'offres, aucune
    ligne de panier.
  - `CANCEL` (`non`) : ni l'un ni l'autre, décision effacée.
- Un nombre différent du disponible (« 15 » sur 20) reste un achat direct de 15 (revalidé) ; un
  nombre supérieur re-déclenche une rupture.
- Périmètre : uniquement les produits SANS palier (avec palier, le disponible est en unité de base
  et non en nombre de paquets — comportement historique conservé, pas de prise directe
  automatique).

## « Commande certifiée » d'achat direct — pas de nouvelle classe

L'architecture de confirmation figée existe déjà pour l'achat : `PreorderDraft` (versionné, CAS
PostgreSQL) dont `items`/`total_amount` sont ceux que **`create_preorder_draft` renvoie côté
serveur** (relus en base, `bootstrap_preorder_draft`), avec clé d'idempotence stable. Introduire
une seconde classe `CertifiedDirectPurchase` dupliquerait ce mécanisme ; elle n'a donc pas été
créée. Le récap montre le brouillon, `confirm_preorder_draft` l'exécute (garde `status != DRAFT`
+ `SELECT … FOR UPDATE` côté serveur), un double OK n'exécute qu'une fois.

**Décision produit à trancher** : « achat direct » = panier → `précommander` → récap → GPS →
confirmation. Après `20`, l'utilisateur voit son panier (20 × 450000) et confirme via le
`PreorderDraft` ; il n'y a pas de raccourci `20 → récap immédiat`. Si un enchaînement plus court est
souhaité, c'est un changement de parcours, pas un correctif d'invariant.

Logs structurés ajoutés : `BUYER_SEARCH_SNAPSHOT_CREATED`, `BUYER_PHOTO_PREVIEW`,
`BUYER_OFFER_SELECTED`, `BUYER_STOCK_SHORTAGE`, `BUYER_SHORTAGE_DECISION`,
`BUYER_DIRECT_COMMAND_CERTIFIED`, `BUYER_DIRECT_EXECUTED` (identifiants techniques, jamais de
contenu sensible).

## Unité ↔ taxonomie (`domain/unit_taxonomy.py`)

Cause : `create_product` recopiait `unit.upper()` sans contrôle ; la taxonomie n'était qu'un conseil
de la couche conversationnelle (`resolve_product_unit`). Invariant désormais imposé par la couche
service, avant écriture : config admin `allowed_units` si elle existe (elle n'existe pas encore en
base — colonnes absentes), sinon repli taxonomique **générique** : élevage ⇒ famille de comptage
(`TETE`/`UNITE`) ou conditionnement ; `UNITE → TETE` canonicalisé (mapping explicite même
famille) ; `KG/TONNE/LITRE` **rejetés** (jamais de conversion silencieuse : 500 kg ≠ 500 têtes).
Pas de nom de produit ni de producteur codé en dur (liste de mots-clés d'élevage existante).

Recherche (Phase 20) : `search_products` écarte (et journalise `SEARCH_OFFER_DATA_QUALITY`) toute
offre au prix invalide (jamais achetable) ; une unité incompatible/générique est **signalée**
(`data_quality_flags`) sans retirer l'offre : retirer des annonces héritées est une décision du
propriétaire des données.

## Données existantes (Phase 19)

`backend/scripts/sql/audit_product_data_quality.sql` : requêtes **strictement lecture seule**
(`BEGIN READ ONLY … ROLLBACK`) produisant : produits suspects (unité incompatible, prix = quantité,
stock implausible…) avec lignes de commande et événements liés, producteurs à plusieurs offres,
candidats au nettoyage SANS dépendance (proposition, aucune action).

**Non exécuté.** `DATABASE_URL` pointe vers une base managée distante ; je n'y ouvre pas de
connexion sans accord explicite. De plus le script n'a pas pu être validé syntaxiquement (ni `psql`
ni Postgres local) et suppose la présence de `analytics.business_events` (à retirer si la migration
n'est pas appliquée). Recommandation de nettoyage : après lecture du rapport, **archiver**
(`is_available = false`) plutôt que supprimer les produits de la section 3 sans commande ni événement ;
toute mutation exige l'accord explicite du propriétaire.

## Ce qui n'a pas pu être prouvé ici

- **PostgreSQL réel** : indisponible dans cet environnement. Les assertions « DB » portent sur la
  frontière MCP (arguments de `create_preorder_draft` / `confirm_preorder_draft`) avec des doubles
  écrits par les tests, et sur le faux moteur SQL du `PreorderDraft`. Le débit de stock et la
  création d'`Order` restent verrouillés par les tests serveur existants, non ré-exécutés contre une
  vraie base.
- **LLM réel / WhatsApp réel** : non exécutés. Les réponses non numériques (« je prends les 20 »)
  dépendent de l'interprétation LLM ; seule la réponse numérique nue est déterministe.
- Le script d'audit SQL (voir ci-dessus).

## Verdict et périmètre

**AGENT BUYER DIRECT PURCHASE FLOW READY FOR RESTRICTED PILOT — sous les conditions ci-dessous.**

Gates (Windows, `PYTHONUTF8=1`, Redis injoignable) :

| Gate | Résultat |
|---|---|
| Nouveaux tests `test_buyer_direct_purchase_flow.py` (36) + `test_unit_taxonomy_invariant.py` (28) | verts ; **28 des 36 échouent sur l'ancien code** (les 8 qui passent dans les deux cas sont les tests d’innocence de `photo N` et les cas sans rapport avec le défaut) |
| Durcissements précédents rejoués (cross-flow, idempotence MCP, ownership, 500.000, confirmation figée, webhook enqueue, réconciliation, dédup…) + buyer/shortage/checkout | 250 passed |
| `tests/integration` / `nodes` / `interpreter` / `architecture` / `chaos` | 417 / 899 / 385 / 1132 / 171 passed |
| **Suite backend complète** | **5233 passed, 2 failed, 363 skipped, 5 xfailed** |
| Les 2 échecs | `test_turn_policy_classification.py` — séparateurs de chemin Windows, préexistants (identiques sur `origin/main`) |
| Ruff `src` + `tests` | 711 = baseline (0 nouvelle) |
| mypy (14 sources modifiées/ajoutées) | 106 erreurs avant, 106 après (0 nouvelle) |

Conditions du pilote restreint :
1. Lancer le test manuel WhatsApp de la section ci-dessous après déploiement.
2. Exécuter (ou faire exécuter) `audit_product_data_quality.sql` en lecture seule et traiter les
   offres héritées signalées (`boeufs / UNITE`) — l'invariant protège les créations futures, pas les
   lignes existantes.
3. Décision produit sur le parcours « achat direct » (raccourci récap après `20` ?).

Risques résiduels : cache photo à clé unique par téléphone (voir B) ; réponses non numériques à la
question de rupture dépendantes du LLM ; produits à palier hors `TAKE_AVAILABLE` ; PostgreSQL et LLM
réels non exercés ; script SQL non validé.

## Test manuel WhatsApp à exécuter après déploiement

Compte acheteur de test, avec au moins 3 offres « boeufs » dont 2 du même producteur (stocks 1, 20,
grand) :

1. `boeufs` → un seul message de menu (pas de « Page 2/2 » vide), 3 lignes numérotées.
2. `photo 3` → photos de la 3ᵉ offre + « répondre *3* ».
3. `3` → « Vous avez choisi *…* » avec le **prix et le stock de la ligne 3** (pas de la 2).
4. `Je veux <plus que le stock de la ligne 2>` après avoir choisi la 2 → message de rupture proposant
   « répondez *N* ».
5. `N` → panier avec **N × prix de la ligne 2**, aucun message d'appel d'offres.
6. `précommander` → récap avec la quantité N, puis GPS, `oui` → une seule commande (renvoyer `ok`
   ne doit pas en créer une seconde).
7. Variante : à l'étape 4, répondre `oui` → appel d'offres avec la quantité d'origine ; `non` → rien.
