# Commercial Pricing — Phase B1 : vertical slice `SALES_PUBLISH_PRODUCT`

Suite de `COMMERCIAL_QUANTITY_PRICING_MODEL.md` (Phase A). La Phase A a livré le modèle
(`domain/commercial_offer.py`, `validate_commercial_offer`) **en isolation**. La Phase B1 le branche dans le
VRAI flux WhatsApp de publication de vente, de bout en bout, pour que l'erreur du 2026-09-28 soit
impossible **par architecture** et non par détection du mot « sachet ».

> Incident : « 50 litres de lait » + « 500f le sachet » → récap « 500 FCFA/SAC … prix appliqué par LITRE »
> → exécuté à 500 FCFA/LITRE (le prix d'un sachet devenait le prix d'un litre).

## 1. Pipeline — avant / après

| Étape | Avant B1 | Après B1 |
|---|---|---|
| interpréteur | extrait `price`, `price_unit` (LLM) sans notion de base | inchangé, **mais** une réponse à une question commerciale (`routing.py` étape 0.45) est lue déterministiquement dans le contexte de la question |
| `memory_update` | fusionne, l'enrichissement texte lit « 0,5 litre » comme QUANTITÉ | garde `_commercial_reply_turn` : une réponse de question n'écrase jamais quantité/unité/prix ; « 600 le sachet » = prix, pas 600 sachets ; les alias périmés sont remis à `None` |
| `validator` | `reconcile_price_basis` (heuristique de conversion) | `evaluate_sales_publish_state` → `CommercialOffer` + `validate_offer` ; INCOMPLETE ⇒ `WAITING_INPUT` + **une** question précise |
| draft | `SalesPublishDraft` champs plats | + `commercial_offer` (JSON, versionné, CAS Postgres, **sans migration**) |
| confirmation | `confirmation_summary` du payload brut | `SalesPublishDraft.render_summary()` = projection de l'offre certifiée |
| exécution | payload brut normalisé au fil de l'eau | `execution_payload()` **dérivé de l'offre** ; l'exécuteur ne décide rien |

## 2. Où vit l'adaptateur (un seul)

`src/ladini/domain/commercial_offer_flow.py::build_commercial_offer_from_sales_state` — l'UNIQUE endroit
où l'état legacy (`transaction_payload`, texte, entités dites CE tour) devient une `CommercialOffer`.
`services/domain/commercial_gate.py` est le pont nœud→domaine (état LangGraph → appel domaine + journaux).
Aucun second système : `reconcile_price_basis` reste pour les goals PROCUREMENT/PRODUCTION (non migrés).

## 3. Provenance réelle

- montant/base dits **dans le texte** (`500f le sachet`, `à 300 fcfa le kg`) → `USER_EXPLICIT` ;
- nombre nu répondant à « Quel prix par tonne ? » → `QUESTION_CONTEXT_EXPLICIT` (base = celle demandée) ;
- `price_unit` présent dans les entités LLM **mais absent du texte** → `LLM_INFERRED` ⇒ `INCOMPLETE` (on redemande) ;
- rien de dit → base `UNKNOWN` ⇒ `INCOMPLETE`. Un inféré n'est jamais marqué explicite
  (`Provenance.is_execution_safe` reste le seul juge).

## 4. Contexte de question (`PendingInteraction`)

Deux champs STRUCTURED (`price_basis`, `package_size`) dans `core/field_registry.py` ;
`PendingInteraction.target` porte `requested_field`, `expected_basis_unit`, `package_type`, `content_unit`,
`candidate_amount`. Un message n'est « une réponse » que s'il est **pur** : `_is_pure_content_reply`
(nombre + unité + mots de liaison, aucun verbe/produit). Sous la question « Quelle quantité contient un
sachet ? », `je veux vendre 30 kg de tomates` est une NOUVELLE vente (bug trouvé et corrigé pendant B1 :
il devenait « sachet de 30 kg »).

## 5. Comportements verrouillés (`tests/integration/test_commercial_pricing_vertical_slice.py`)

| Scénario | Résultat |
|---|---|
| A. 50 L lait, `500f le sachet` | question « Quelle quantité contient un *sachet* ? » ; aucun draft ; `0,5 litre` = contenu, quantité intacte ; récap « 50 litres de lait à 500 FCFA par sachet de 0,5 litre » |
| B. 200 t maïs, `500000` après « par tonne ? » | 500 000 FCFA par tonne (QUESTION_CONTEXT_EXPLICIT) |
| C. `200 tonnes à 500000` | « par tonne ou pour l'ensemble des 200 tonnes ? » ; jamais de confirmation avant réponse |
| TOTAL_LOT | « pour 5 000 000 FCFA au total (lot entier) » |
| D/E | lait 500/L et bœufs 450 000/tête : aucune question en plus |
| F | LLM propose `TETE` pour du lait de vache → `LITRE` (la taxonomie/dérivé gagne sur le nom) |
| maïs 100 kg à 300 FCFA/kg | inchangé, aucune question |
| corrections | `finalement 600 le sachet` garde 0,5 L ; `finalement 1L le sachet` garde le prix ; **+1 version** de draft à chaque correction |
| invalidation | changement de produit ⇒ ni sachet ni ancienne quantité/prix ; changement d'unité (TONNE→KG) ⇒ base redemandée |
| cross-flow | après publication, annulation, expiration du pending, ou nouvelle vente de même intent : aucune fuite |

## 6. Certification / confirmation / exécution

Le draft porte `draft_id`, `version`, `commercial_offer`, ainsi que l'offre normalisée. La confirmation lit
`render_summary()` ; à `CONFIRM`, la clé d'idempotence est `sales_publish:{draft_id}:{version}`
(version après la transition CONFIRM→EXECUTING). Muter l'état brut après confirmation ne change rien à
l'exécution (test dédié : quantité 999 / prix 1 injectés, l'exécution utilise 50 L / 500 FCFA-sachet).
Un second « oui » ne republie pas (1 seul `create_product`).

## 7. Mapping base de données (aucune migration)

`offer_execution_payload` :

- `quantity`/`unit` = stock en unité de base ; `price` = prix par unité de base (`500 / 0,5 L = 1000 FCFA/L`) ;
- prix **par conditionnement** ⇒ un palier dans le JSONB existant `products.pricing_tiers`
  `{"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"}`.

Écart connu et assumé (à traiter en B2) : `TOTAL_LOT` est stocké comme un prix unitaire dérivé
(`5 000 000 / 200 000 kg = 25 FCFA/kg`) car `products` n'a pas de colonne « prix du lot » ; un total non
divisible perdrait de la précision. Pas de colonne `price_basis` : la base est implicite dans `pricing_tiers`.

## 8. Taxonomie pilote

Le code lit déjà `governance.sub_categories.priority_unit/allowed_units` (`resolve_product_unit`) et la taxonomie
**gagne** sur l'heuristique de nom (`ANIMAL_DERIVED_PRODUCT_MARKERS` : lait/œufs/fromage… jamais TETE).
Les données, elles, sont un chantier séparé (aucune production modifiée) :

- `docs/domain/sql/taxonomy_pilot_audit_READONLY.sql` — audit lecture seule ;
- `docs/domain/sql/taxonomy_pilot_seed_PROPOSED_NOT_APPLIED.sql` — patch proposé, **terminé par `ROLLBACK`**,
  n'écrit que là où rien n'est configuré, motifs en mots entiers.

L'audit SQL n'a **pas** été exécuté (base distante gérée, pas d'approbation) : l'état réel des sous-catégories
est inconnu — inconnu ≠ vide.

## 9. Observabilité

Événements journalisés (jamais le texte de l'utilisateur ; `flow_id` = identifiant de conversation masqué) :
`COMMERCIAL_OFFER_PARSED`, `COMMERCIAL_OFFER_INCOMPLETE`, `PACKAGE_REQUIRED`, `PACKAGE_RESOLVED`,
`PRICE_BASIS_RESOLVED`, `PRICE_BASIS_AMBIGUOUS`, `COMMERCIAL_OFFER_CERTIFIED`, `COMMERCIAL_OFFER_EXECUTED`
(`draft_id`, `version`, `flow_id`, `idempotency_key`).

## 10. Défauts préexistants trouvés et corrigés en route

1. **Alias périmés ressuscités** (`memory.py::_null_stale_aliases`) : `normalize_slot_keys` replie `quantite`,
   `qty`, `prix`, `montant`… sur le canonique, mais ces alias restaient dans le canal `merge_dict` avec leur
   ancienne valeur ; dès qu'une cascade (changement de produit/unité) vidait le canonique, le validateur
   relisait l'ancien alias. « finalement je vends des boeufs » gardait 50 et 500 FCFA du lait. Concerne tous
   les goals qui vident une quantité/un prix, pas seulement la vente.
2. Enrichissement texte : « 600 le sachet » lu comme 600 sachets.
3. `l'ensemble` lu comme `l` = LITRE.

## 11. Ce qui reste — Phase B2 « BID / ORDER ITEM / MARKET OFFER SCHEMA HARDENING »

Non touché (mandat) :

- `marketplace.bids.offered_price` : nombre nu sans unité ni base — **migration requise** (`offered_price_basis`,
  `offered_price_unit`) ; le parseur producteur (`auctions.py::_first_number`) jette encore les mots d'unité.
- `marketplace.order_items` : ni unité ni snapshot de conditionnement (`unit`, `packaging_snapshot jsonb`).
- `marketplace.market_offers` : aucune colonne palier/conditionnement/base.
- `products` : colonne de prix du lot (`TOTAL_LOT`) ou `price_basis` explicite.
- Contrat LLM (`new_task_contract.py`) : pas de champ `package_size`/`price_basis` typé (B1 lit le texte, pas un champ LLM).
- Buyer flow (`cart_service`, `_guess_display_unit`) : affichage encore basé sur `unit`.
- Goals PROCUREMENT/PRODUCTION/PREORDER : toujours sur `reconcile_price_basis`.

## 12. Risques résiduels

- Les regex de langue (`_UNIT_AFTER_PRICE_RE`, filler de `_is_pure_content_reply`) couvrent le français courant ;
  un dialecte/orthographe SMS inattendu retombe sur la **question** (jamais sur une devinette) — coût : un tour.
- Un vrai LLM peut classer une correction de prix au confirm autrement que le double de test (`ANSWER` au lieu de
  `NEW_TASK`) ; le comportement à la confirmation pour `ANSWER` n'est pas modifié par B1.
- Les tests d'intégration utilisent un faux moteur SQL fidèle pour le draft (pas un vrai Postgres) — même limite
  documentée que `test_sales_publish_draft_persistence.py`.
