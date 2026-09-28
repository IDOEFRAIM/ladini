# Commercial Quantity & Pricing Domain Model

Statut : mission architecturale "Commercial Quantity & Pricing Domain
Hardening", 2026-09-28, **Phase A** (audit + fondations + 2 correctifs P0
réels et confirmés). Compagnon de `AGENT_RELIABILITY_MATRIX.md`/
`AGENT_PRODUCTION_READINESS.md` (fiabilité transactionnelle — sujet
distinct, non retouché par cette mission sauf mention explicite).

## Pourquoi ce document existe

Une offre commerciale ("50 litres de lait à 500 F le sachet", "200 tonnes
de maïs à 500 000 F la tonne") était pensée, dans tout le pipeline, comme
un triplet plat `(quantity, unit, price)`. Ce triplet ne peut PAS
représenter "500 F est un prix PAR SACHET, pas par litre" — la seule façon
de le faire était de fourrer "SACHET" dans le champ `unit` de la quantité,
écrasant l'unité physique réelle et perdant "combien de litres contient un
sachet ?" entièrement.

Un audit à 4 volets, en parallèle, sur l'ensemble du pipeline (extraction
NLP, registre de drafts, exécution MCP/DB, flux acheteur/enchères/besoin
récurrent) a confirmé — **par lecture directe du code, pas par hypothèse**
— que ce n'était pas un cas isolé mais un défaut architectural traversant
tout le système. Deux bugs P0 réels et LIVE en ont découlé directement,
tous deux corrigés cette session (voir §Ce qui est fait). Le reste de ce
document couvre le modèle conceptuel complet demandé par le mandat,
l'inventaire exact de ce qui reste à câbler, et pourquoi une partie
substantielle est délibérément différée plutôt que précipitée.

## Principe central (rappel du mandat)

> Une valeur commerciale critique ne peut être exécutée si sa sémantique
> n'est pas explicite ou dérivée par une règle déterministe certifiée.

En particulier :
- `price_amount` seul n'est **jamais** exécutable — il lui faut
  `price_basis`.
- `PER_PACKAGE` sans définition de contenu du conditionnement n'est
  **jamais** exécutable.
- Une conversion entre unités incompatibles (ex: SAC ↔ KG) est
  **interdite** — un conditionnement n'est pas une unité physique.
- Une normalisation interne ne doit **jamais** supprimer la représentation
  commerciale originale.
- **When in doubt → ASK. Never guess and write.**

---

## 1. Architecture actuelle — où la sémantique était perdue

Diagramme du pipeline réel (confirmé par audit) :

```
USER TEXT
  │
  ▼
RAW EXTRACTION (interpreter/routing.py, micro-prompts new_task_contract.py)
  │  price_unit EXISTE, distinct de unit (introduit après un incident réel
  │  2026-09-xx) — mais AUCUN champ pour "combien contient un package"
  ▼
MEMORY UPDATE (nodes/memory.py)
  │  ⚠ PERTE #1 : si pricing_tiers existe, quantity/price/price_unit sont
  │  dérivés en scalaires (1er tier, somme des quantités) — le multi-
  │  conditionnement est jeté pour tout lecteur en aval.
  │  ⚠ PERTE #2 (P0, CORRIGÉE cette session) : `_normalize_quantity_to_kg`
  │  (utils.py) convertit quantity/unit (TONNE/G→KG) EN PLACE — jamais le
  │  price associé.
  ▼
VALIDATION (nodes/validation.py)
  │  ⚠ PERTE #3 : required_fields ne liste JAMAIS price_unit/pricing_tiers
  │  pour aucun des 4 goals génériques concernés (SALES_PUBLISH_PRODUCT,
  │  SALES_RECORD_DIRECT, STOCK_REGISTER_HARVEST, PRODUCTION_DECLARE_FUTURE)
  │  — seuls price>0/quantity>0 sont vérifiés.
  ▼
DRAFT (domain/sales_publish_draft.py et 3 siblings)
  │  Fields plats price/quantity/unit — AUCUNE notion de package/basis.
  │  render_summary() interpole naïvement "{price} FCFA/{unit}".
  ▼
CONFIRMATION (confirmation_gate.py, services/ui/confirmation_summary.py)
  │  Le seul template basis-aware est _format_pricing_tiers (actif QUAND
  │  pricing_tiers existe, absent pour PRODUCTION_DECLARE_FUTURE/
  │  SALES_RECORD_DIRECT) — le cas courant (prix unique, pas de tiers) est
  │  toujours "{price} FCFA/{unit}" ou "{price} FCFA" (total), jamais
  │  "{price} FCFA par {package} de {contenu} {unit}".
  ▼
MCP (protocols/mcp/servers/h.py)
  │  Le "schéma MCP" EST la signature Python de la méthode DB — aucun
  │  contrat séparé, aucune notion de package/basis à ce niveau.
  ▼
DB WRITE (services/database/{producer,marketplace,product}.py)
  │  ⚠ PERTE #4 (P0, CORRIGÉE cette session) : `actions/common.py::
  │  _UNIT_TO_KG` assimilait SAC/PANIER/CHARRETTE à des unités de masse à
  │  poids FIXE deviné (SAC=100kg...) — "3 sacs" devenait "300 KG" quel que
  │  soit le poids réel.
  ▼
DB (Product.price/unit/quantity_for_sale plats ; Product.pricing_tiers
  JSONB, bolt-on, non intégré aux champs plats ; MarketOffer AUCUN champ
  package/tier du tout)
```

**Le seul sous-système déjà discipliné** : `domain/pricing_tiers.py`
(`PricingTier`, partagé producteur ET acheteur) — sépare déjà `unit`
(mesure physique) de `packaging` (conditionnement, texte libre), refuse les
familles incompatibles, et a un classifieur dédié
(`classify_pack_count_unit`, incident réel documenté "3 bidons ≠ 30L") pour
éviter qu'un nombre de paquets soit confondu avec une quantité globale.
**Ce module N'A PAS été dupliqué** — les nouveaux concepts (§2 ci-dessous)
le réutilisent explicitement (`domain/commercial_offer.py` importe
`unit_family`/`unit_factor` de `pricing_tiers.py`, ne redéfinit rien).

## 2. Nouveau modèle domaine (`src/ladini/domain/commercial_offer.py`)

Module créé cette session. Huit concepts, tel que demandé par le mandat :

| # | Concept | Statut |
|---|---|---|
| 1 | PRODUCT IDENTITY | pré-existant (`domain/product_identity.py`, dédup textuel) + `sub_category_id`, non retouché |
| 2 | INVENTORY QUANTITY | `InventoryQuantity(amount, unit, source)` — implémenté |
| 3 | COMMERCIAL QUANTITY | `CommercialQuantity(amount, unit, source)` — implémenté |
| 4 | PRICING | `Pricing(amount, basis, currency, source, basis_source)` — implémenté |
| 5 | PACKAGE / CONDITIONING | `PackageDefinition(package_type, content_amount, content_unit, status, source)` — implémenté |
| 6 | NORMALIZED REPRESENTATION | `NormalizedRepresentation` — implémenté (calcul interne uniquement) |
| 7 | PROVENANCE | `Provenance` (7 valeurs, `is_execution_safe`) — implémenté |
| 8 | CERTIFIED BUSINESS COMMAND | **non créé séparément** — voir §7, décision : étendre `SalesPublishDraft` plutôt qu'une 2e source de vérité |

### 2.1 `PriceBasis`

```python
PER_BASE_UNIT  # par l'unité physique du produit (kg, litre, tête...)
PER_PACKAGE    # par exemplaire d'un conditionnement (sac, sachet, bidon...)
TOTAL_LOT      # pour la totalité de la quantité annoncée
```

Décision de scope (mandat Phase 3, "PER_BASE_UNIT + unit vs enum par
unité ?") : **`PER_BASE_UNIT` générique + `unit` séparé**, pas un enum par
unité physique (`PER_KG`/`PER_TONNE`/`PER_LITRE`...) — l'unité physique
elle-même vit déjà dans `InventoryQuantity.unit`/`CommercialQuantity.unit`,
la dupliquer dans l'enum de `PriceBasis` aurait recréé exactement la classe
de désynchronisation que ce mandat corrige (deux endroits pour dire "quelle
unité ?", qui peuvent diverger).

### 2.2 `Provenance`

```python
USER_EXPLICIT              QUESTION_CONTEXT_EXPLICIT
DOMAIN_DERIVED              UNIT_CONVERSION
DATABASE_VERIFIED           LLM_INFERRED
UNKNOWN
```

`Provenance.is_execution_safe` est `False` pour `LLM_INFERRED`/`UNKNOWN`
uniquement — implémentation directe de la règle centrale du mandat
("LLM_INFERRED ou UNKNOWN sur une donnée financière critique → NO BUSINESS
WRITE").

### 2.3 `PackageDefinition`

```python
package_type: Optional[str]       # "SACHET", "SAC", "BIDON", "CAISSE"...
content_amount: Optional[float]   # None = inconnu
content_unit: Optional[str]       # unité physique du CONTENU
status: KNOWN | UNKNOWN | NOT_REQUIRED
source: Provenance
```

`is_content_known` est la seule question qui compte pour la validation :
`status == KNOWN` ET `content_amount`/`content_unit` renseignés.

### 2.4 Conversions déterministes — `convert_commercial_quantity_to_base_unit`

**Ne réimplémente aucune table de conversion.** Réutilise
`domain/pricing_tiers.py::unit_family`/`unit_factor` (la table MASS/VOLUME
la plus complète du dépôt, déjà partagée avec `domain/analytics/units.py`).
Une conversion n'est autorisée QUE si les deux unités sont dans la MÊME
famille convertible (MASS ou VOLUME) — SAC/PANIER/CHARRETTE/TETE/UNITE
restent des "familles singleton" : aucune conversion, jamais un facteur
deviné. Retourne `(montant_converti, facteur)` — le facteur sert à
re-baser un prix dans la MÊME proportion (voir §4).

### 2.5 `validate_commercial_offer` — le validateur central (Phase 13)

```python
def validate_commercial_offer(
    *, inventory_quantity, pricing, package=None,
) -> CommercialOfferValidation:  # VALID | INCOMPLETE | INVALID
```

Règles exactes :
- `pricing is None` ou `inventory_quantity is None` → `INCOMPLETE`.
- `pricing.amount <= 0` ou `inventory_quantity.amount <= 0` → `INVALID`
  (conflit, prioritaire sur un simple manque).
- `pricing.basis is None` OU `not pricing.is_basis_known` (provenance
  `LLM_INFERRED`/`UNKNOWN`) → `INCOMPLETE`, `missing_fields=["price_basis"]`.
- `pricing.basis == PER_PACKAGE` ET (`package is None` OU
  `not package.is_content_known`) → `INCOMPLETE`,
  `missing_fields=["package_content_amount"]`.
- Sinon → `VALID`.

Testé contre la matrice complète Phase 25 (scénarios 1-13 du mandat) dans
`tests/unit/test_commercial_offer_validation.py` (16 tests, tous verts).

**Pas encore câblé dans le pipeline live** — voir §7.

## 3. `PriceBasis` — décisions Phase 3

Voir §2.1. Consolidation demandée par le mandat ("évaluer PER_BASE_UNIT +
unit vs enum par unité") : tranchée en faveur de `PER_BASE_UNIT + unit`,
raison donnée ci-dessus.

## 4. Provenance — décisions Phase 4/5

Voir §2.2. Phase 5 (contexte de question) : `QUESTION_CONTEXT_EXPLICIT`
existe dans l'enum mais **n'est pas encore câblé** à un point de détection
réel (ça suppose de savoir, au moment où le validateur tourne, QUELLE
question précédente a été posée — c'est une information du pipeline de
conversation, pas du domaine pur). Documenté comme dépendance de câblage
(§7), pas un manque de modèle.

## 5. Normalisation non destructive — Phase 6/7, ce qui est FAIT

Les 2 bugs P0 fermés cette session sont EXACTEMENT des violations de "la
normalisation ne doit jamais désynchroniser le commercial de l'interne" :

1. **`actions/common.py::normalize_quantity_to_kg`** — SAC/PANIER/
   CHARRETTE (et toute unité non reconnue) **ne sont plus jamais
   convertis** avec un facteur deviné ; ils traversent inchangés, comme
   TETE/UNITE/LITRE le faisaient déjà. `domain/quantity_unit.py::
   UNIT_SYNONYMS` mappe encore "sachet" vers le même code "SAC" qu'un sac
   de 50kg — **résidu documenté**, pas fermé (un sachet et un sac restent
   le MÊME code unité aujourd'hui ; les distinguer vraiment demande une
   révision de `UNIT_SYNONYMS`, hors scope de cette session car cela
   touche l'extraction NLP directement).
2. **3 sites** (`domain/sales.py::publish_product`,
   `domain/agro.py::declare_crop_cycle`,
   `domain/procurement.py::create_request`) — un prix documenté "par
   `unit`" est maintenant re-basé par le MÊME facteur déterministe que la
   quantité (`convert_commercial_quantity_to_base_unit`), jamais laissé
   désynchronisé. Testé en isolation avec repro AVANT correctif (git
   stash), voir `tests/unit/test_commercial_offer_price_basis_hardening.py`.

Ce qui N'EST PAS fait (Phase 6 "conserver toujours l'original") : les
fonctions ci-dessus continuent de CONVERTIR (TONNE→KG) plutôt que de
préserver "200 TONNE" jusqu'à la DB et ne calculer le KG que pour un
usage interne. Ce choix a été délibéré : préserver l'original jusqu'à la
DB demanderait que `Product.unit`/`MarketOffer.unit` acceptent n'importe
quelle unité SANS que le reste du pipeline (recherche, agrégation
analytics, `domain/pricing_tiers.py`) suppose une base commune — un
changement de comportement plus large que "ne pas perdre le prix", qu'on
préfère traiter comme un chantier séparé (Phase B, §8) plutôt que
mélanger avec le correctif P0 de cette session.

## 6. Conversions déterministes / packages — Phase 7/8

`convertible_measurement_family`/`convert_commercial_quantity_to_base_unit`
(§2.4) implémentent exactement la règle demandée : MASS↔MASS et
VOLUME↔VOLUME autorisés (table `pricing_tiers.py`), tout le reste refusé.
`PackageDefinition` (§2.3) sépare structurellement un conditionnement d'une
unité physique — mais n'est, à ce stade, PAS encore alimenté par
l'extraction LLM (§7, Phase 27) ni persisté sur aucun draft (§7, Phase 15).

## 7. Ce qui reste — inventaire exact, Phase B

Le tableau ci-dessous liste, phase par phase du mandat, ce qui est
FAIT (F), PARTIEL (P) ou DIFFÉRÉ (D) — et pourquoi pour chaque D.

| Phase mandat | Sujet | Statut | Pourquoi différé (si D) |
|---|---|---|---|
| 1-2 | Audit + inventaire concepts | **F** | 4 audits parallèles complets, ce document |
| 3 | PriceBasis | **F** | §2.1 |
| 4 | Provenance | **F** | §2.2 |
| 5 | Question context | **P** | Enum existe, pas câblé à la détection réelle (dépend du pipeline conversationnel) |
| 6 | Normalisation non destructive | **P** | 2 bugs P0 fermés ; conversion silencieuse encore active (choix délibéré, §5) |
| 7 | Conversions déterministes | **F** | §2.4/§6, réutilise `pricing_tiers.py` |
| 8 | PackageDefinition | **F** (modèle) / **D** (persistance) | Le type existe ; rien ne le peuple ni ne le stocke encore |
| 9-10 | Scénario "500 F le sachet" | **P** | `validate_commercial_offer` le gère EN ISOLATION (testé) ; pas câblé à un vrai tour de conversation |
| 11 | Taxonomy livestock | **P** | Bug substring (`buyer.py`) fermé ; mécanisme taxonomy déjà câblé de bout en bout (colonnes DB confirmées existantes) mais AUCUNE sous-catégorie n'a `allowed_units` peuplé — **donnée admin manquante, pas du code** |
| 12 | Fallback texte produit | **D** | Non touché — reste la dernière protection avant écriture pour les 10 goals génériques (P1-A de la mission fiabilité protège la CONFIRMATION, pas la RÉSOLUTION du produit) |
| 13 | Domain validator central | **F** | §2.5 |
| 14 | Pas de confirmation sur INCOMPLETE | **D** | Nécessite le câblage §9-10 |
| 15 | CertifiedCommercialOffer | **D** (décision prise, pas exécutée) | Étendre `SalesPublishDraft` (pas une 2e source de vérité) — champs à ajouter : `price_basis`, `package` (dataclass ou JSON), `provenance` par champ critique. Mécaniquement simple (dataclass extensible, CAS déjà en place) mais implique de réécrire `render_summary()` — un changement visible sur CHAQUE confirmation de vente, à tester à part |
| 16 | Confirmation depuis l'objet certifié | **D** | Dépend de 15 |
| 17 | Executor ne reconstruit pas la sémantique | **F** (déjà vrai) | Confirmé par audit : `mcp_tool_executor` ne fait AUCUN calcul de prix/unité — il transmet ce que le dispatcher construit. Les 2 bugs P0 étaient dans les dispatchers (`domain/*.py`), pas l'executor lui-même — déjà conforme à cette règle, corrigé au bon endroit |
| 18 | DB capability | **F** (audit) | §8 ci-dessous |
| 19 | Compatibilité produits simples | **F** (déjà vrai) | `package=NONE`/`PriceBasis.PER_BASE_UNIT` implicite reste le comportement par défaut ; rien cassé (139 tests golden Phase 1 + suite complète toujours verts) |
| 20 | Compatibilité pricing tiers | **F** (déjà vrai) | `pricing_tiers.py` non touché, réutilisé tel quel |
| 21 | Buyer flow | **D** | Non câblé — `cart_service.py`/`buyer.py` affichent toujours le prix brut stocké, correct pour `unit` explicite mais toujours vulnérable si `unit` est resté au défaut KG (`_guess_display_unit`, corrigé pour le faux positif substring, pas pour le fond) |
| 22 | Order item snapshot | **D** | Confirmé par audit : `OrderItem` ne stocke ni unit ni packaging, seulement `tier_id`+`base_unit_quantity` — un changement de schéma serait nécessaire pour fermer entièrement (voir §8) |
| 23 | Bid pricing | **D** | Confirmé par audit comme LE point le plus faible : `Bid.offered_price` est un float nu, sans unité ; le parseur producteur (`_first_number`) jette les mots d'unité ; `recap_bid` réétiquette le nombre brut avec l'unité de l'ENCHÈRE, pas ce que le producteur a dit — risque d'inversion de prix confirmé mais NON corrigé cette session (portée déjà large) |
| 24 | Recurring pricing | **P** | `max_price_per_unit` est correctement unit-gated PAR OCCURRENCE (comparaison bloquée si unités différentes) mais PARTAGÉ comme un seul plafond entre plusieurs produits d'unités différentes dans un même draft multi-items — ambiguïté structurelle documentée, non corrigée |
| 25 | Test matrix | **F** (partiel, en isolation) | 16 tests sur `validate_commercial_offer`, pas encore de test bout-en-bout via une vraie conversation |
| 26 | Price without basis | **F** (modèle) / **D** (câblage question-context) | §2.5 gère le cas ; le cas "contexte de question" (Phase 5) n'est pas câblé |
| 27 | LLM contract | **D** | Le contrat actuel (`new_task_contract.py`) n'a pas de champ `package_size`/`price_basis` typé — l'ajouter change ce que CHAQUE conversation envoie au LLM, hors scope Phase A |
| 28 | Deterministic resolution | **F** (déjà vrai) | Toutes les conversions de ce module sont Python pur, aucune décision LLM |
| 29-33 | Provenance/cross-flow/correction/change-unit/change-product tests | **D** | Dépendent tous du câblage §9-10/15-16 |
| 34 | Architecture invariants | **D** | Vacants tant que rien n'est câblé dans le pipeline live — écrire des tests d'architecture sur un mécanisme non branché serait trompeur |
| 35 | Observability | **D** | Logs `COMMERCIAL_OFFER_*` non ajoutés — rien ne les déclenche encore (le validateur n'est appelé que par les tests) |
| 36 | Data audit | **D** | Nécessite un accès DB réel en lecture ; non exécuté cette session (pas de Postgres connecté) — recommandé comme premier chantier Phase B |
| 37 | Documentation | **F** | Ce document |
| 38 | Migration strategy | **F** | §8 |
| 39 | Legacy compatibility | **F** (analyse) | §5, §7 ligne 12 |
| 40 | Golden E2E | **D** | Dépend du câblage §9-10/15-16 |
| 41 | Revert-check | **F** | Tous les tests écrits cette session vérifiés en échec sur le code pré-correctif (git stash), voir chaque fichier de test |
| 42 | Full gates | **F** | Voir rapport final |
| 43 | Pas de merge automatique | **F** | Une seule branche désignée (contrainte de session, déjà documentée dans `AGENT_PRODUCTION_READINESS.md` §Recommandation de séquencement) — aucune PR créée |

## 8. DB capability — ce qui existe, ce qui manque, migration

**Confirmé par lecture directe des modèles + `schema_contract/migrations/`
(la source de vérité du schéma réel — un miroir en LECTURE SEULE d'un
dépôt frontend Drizzle séparé, ce backend ne peut PAS émettre de DDL, voir
`AGENT_PRODUCTION_READINESS.md` pour la même contrainte déjà documentée
côté fiabilité transactionnelle) :**

| Table | Colonnes prix/quantité/package existantes | Ce qui manque pour le modèle complet |
|---|---|---|
| `marketplace.products` | `price`, `unit`, `quantity_for_sale`, `pricing_tiers` (JSONB, non typé au niveau DB — chaque tier `{quantity,unit,price,packaging}`), `packaging_type` (Text, un seul par PRODUIT, pas par tier, déconnecté du JSONB) | Pas de `price_basis` colonne (implicite dans le sens du prix) ; `pricing_tiers` JSONB non contraint par un schéma DB |
| `marketplace.market_offers` | `unit`, `price_per_unit` UNIQUEMENT | **Aucune colonne tier/package/basis du tout** — `declare_future_production` n'a jamais eu de tarification par palier |
| `governance.sub_categories` | `priority_unit` (Text), `allowed_units` (Text[]) — **existent déjà**, non peuplés | `allowed_pricing_bases`, `measurement_family` : colonnes réellement absentes |
| `marketplace.order_items` | `quantity`, `price_at_sale`, `tier_id` (FK nu), `base_unit_quantity` | Aucun snapshot d'unité/packaging — dépend d'un JOIN live vers `Product`, risque de staleness confirmé (une commande historique peut se retrouver mal étiquetée si le producteur change `Product.unit`/`pricing_tiers` après coup) |
| `marketplace.bids` | `offered_price` UNIQUEMENT | **Aucune colonne unité** — le point le plus faible de tout le modèle actuel |

**Recommandation de migration (Phase 38 du mandat, staged, PAS big-bang) :**

- **Phase A (cette session)** : modèle domaine + 2 correctifs P0 (adaptateur
  de compatibilité — aucune écriture DB nouvelle, les fonctions existantes
  écrivent toujours dans le même schéma, juste avec des valeurs correctes).
- **Phase B (migration additive, Drizzle, dépôt frontend)** :
  1. `marketplace.bids.offered_price_basis text` + `offered_price_unit
     text` (ferme le point le plus faible, §7 ligne "Bid pricing").
  2. `marketplace.order_items.unit text`, `packaging_snapshot jsonb`
     (ferme le staleness risk sur les commandes historiques).
  3. `governance.sub_categories.allowed_pricing_bases text[]`,
     `measurement_family text` (complète ce qui existe déjà pour
     `priority_unit`/`allowed_units`).
  4. `marketplace.market_offers` : ajouter `pricing_tiers jsonb` (parité
     avec `Product`, aujourd'hui absent).
- **Phase C (backfill/audit, read-only d'abord)** : peupler
  `allowed_units`/`priority_unit` pour les sous-catégories à risque
  ("Lait" → `[LITRE, ML]`, catégories bétail → `[TETE]`...) — un chantier
  de DONNÉE, pas de code, déjà exécutable AUJOURD'HUI sans attendre Phase B
  (les colonnes existent). Auditer les offres existantes pour des
  incohérences unit/prix suspectes (ex: `unit=SAC` avec un `price` qui
  semble être un prix/kg) — non exécuté cette session, pas d'accès DB réel
  connecté.
- **Phase D (retrait du legacy)** : une fois Phase B/C en place et
  vérifiées en production, retirer `Product.packaging_type` (dupliqué avec
  le concept `PackageDefinition`) et les heuristiques keyword restantes
  (`LIVESTOCK_PRODUCT_KEYWORDS` peut devenir un PUR filet de secours,
  jamais la source primaire, une fois `allowed_units` peuplé partout où
  c'est pertinent).

## 9. Taxonomy — suppression/dépréciation de l'heuristique livestock

**Ne PAS supprimer `LIVESTOCK_PRODUCT_KEYWORDS`/`is_livestock_product`**
cette session — c'est le SEUL filet pour les sous-catégories non encore
configurées, et le retirer sans Phase C (peuplement des données) casserait
purement et simplement le classement pour tout produit non configuré. Le
correctif de cette session (§5, buyer.py) réduit le RISQUE de cette
heuristique (plus de faux positif substring), sans la retirer. Une fois
Phase C exécutée pour les sous-catégories à risque réel (laitiers, viandes
dérivées), l'heuristique keyword devient un filet résiduel pour les
produits non catalogués — décision produit, pas un correctif technique
unilatéral (même position que le fallback legacy de l'interpréteur, déjà
documentée dans `AGENT_PRODUCTION_READINESS.md`).

## 10-14. Handling détaillé des scénarios nommés

Tous couverts par `validate_commercial_offer` EN ISOLATION (testé), pas
encore par un vrai tour de conversation (§7) :

- **"500 F le sachet"** (§9-10 mandat) : `Pricing(500, PER_PACKAGE)` +
  `PackageDefinition(status=UNKNOWN)` → `INCOMPLETE`,
  `clarification_question="Quelle quantité contient un exemplaire de ce
  conditionnement ?"`. Une fois "sachet = 0,5L" fourni :
  `PackageDefinition(content_amount=0.5, content_unit=LITRE,
  status=KNOWN)` → `VALID`.
- **"500000/tonne"** (§21-22 mandat) : `Pricing(500000, PER_BASE_UNIT)` sur
  `CommercialQuantity(200, TONNE)` → `VALID`, et
  `convert_commercial_quantity_to_base_unit` donne `(200000.0, 1000.0)` —
  le facteur EXACT que les 3 correctifs P0 appliquent désormais aussi au
  prix (500 F/KG en interne, jamais 500 000 F/KG).
- **Price without basis** (§26 mandat) : `Pricing(500000, basis=None)` →
  `INCOMPLETE`. `basis_source=LLM_INFERRED` → également `INCOMPLETE`, même
  avec une valeur de `basis` présente (règle centrale, jamais contournable).
- **TOTAL_LOT** (§23 mandat) : `PriceBasis.TOTAL_LOT` avec provenance
  explicite → `VALID`, sans exiger de `PackageDefinition`.
- **Bid pricing** (§24 mandat) : **non fermé** — voir §7 ligne 23, le point
  le plus faible confirmé du modèle actuel, nécessite une migration DB
  (§8) pour être fermé proprement (`Bid` n'a pas de colonne unité).
- **Recurring pricing** (§25 mandat) : **partiellement fermé** — voir §7
  ligne 24, unit-gated par occurrence mais partagé entre produits
  d'unités différentes dans un même draft multi-items.

## Résiduels exacts (chemins qui dépendent encore d'une heuristique)

- `domain/quantity_unit.py::LIVESTOCK_PRODUCT_KEYWORDS`/
  `is_livestock_product` — mot entier, toujours actif tant que la
  taxonomy n'est pas peuplée (§9).
- `domain/quantity_unit.py::UNIT_SYNONYMS` — "sachet"/"sachets" restent
  mappés au même code "SAC" qu'un sac de 50kg (§5).
- `interpreter/routing.py`/micro-prompts — aucun champ `package_size`
  structuré (§7 ligne 27).
- `flows/producer/auctions.py::_first_number` — extrait le premier nombre
  d'un message de bid SANS lire l'unité qui l'accompagne (§7 ligne 23).
- `services/database/buyer.py::_guess_display_unit` — filet residuel pour
  `unit` non renseigné, toujours basé sur `is_livestock_product`.

## Verdict Phase A

**COMMERCIAL QUANTITY & PRICING DOMAIN : NOT READY** (le modèle et 2 bugs
P0 confirmés sont fermés ; le câblage dans le pipeline de conversation live
— Phases 9-10, 14-16, 21-24, 27, 34-35, 40 du mandat — reste à faire,
délibérément différé plutôt que précipité sur un chantier dont l'ampleur
réelle, confirmée par audit à 4 volets, dépasse ce qu'une session peut
livrer avec le niveau de rigueur (repro-avant-correctif, gates complets)
appliqué aux 2 correctifs qui ONT été livrés). Voir le rapport final pour
le détail complet et les blockers précis.
