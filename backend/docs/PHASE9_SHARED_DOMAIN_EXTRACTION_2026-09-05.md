# PHASE 9 — EXTRACTION DU DOMAINE PARTAGÉ (2026-09-05)

## 1. Problem

Des règles métier utilisées par la couche transactionnelle vivaient dans
le paquet de l'orchestration conversationnelle :

```
services/database/{buyer,producer,product,escrow}.py
services/reconciliation/*, agents/forms.py
        ↓   INVERSION
graphs/agents/market_coach/domain/…
graphs/agents/market_coach/services/domain/…
```

Rien de cassé fonctionnellement — mais la persistance dépendait de
l'orchestration, ce qui rendait `services/` inintelligible sans le paquet
agent et entretenait le cycle `graphs ↔ services`.

## 2. Modules discovered

Inventaire **par AST** (pas par grep) de tous les imports des couches
basses vers `graphs/**/domain` :

| Module | Imports | Symboles | Consommateurs |
|---|---:|---|---|
| `pricing_tiers` | 4 | `PricingTierError`, `validate_pricing_tiers`, `tiers_to_dicts`, `resolve_tier`, `compute_line`, `resolve_stock_debit` | `buyer.py`, `producer.py`, `product.py`, `escrow.py` |
| `order_policy` | 1 | `validate_minimum_order_quantity` | `buyer.py` |
| `quantity_unit` | 1 | `parse_compound_quantity`, `normalize_unit`, `default_unit_for_product` | `agents/forms.py` |
| `preorder_draft` | 2 | modèle + statuts + `build_response_plan`, `adapt_mcp_result` | store CAS, réconciliation |
| `procurement_draft` | 2 | idem | store CAS, réconciliation |
| `sales_publish_draft` | 2 | idem | store CAS, réconciliation |

## 3. Classification

| Module | Pur ? | Dépend de LangGraph / ResponsePlan ? | Classement |
|---|:--:|:--:|---|
| `quantity_unit` (509 l.) | ✅ stdlib seul | non | **SHARED_DOMAIN** |
| `order_policy` (116 l.) | ✅ + `quantity_unit` | non | **SHARED_DOMAIN** |
| `pricing_tiers` (398 l.) | ⚠️ **une** fonction impure | oui, pour `pending_pack_count_tier` | **SHARED_DOMAIN** après extraction de cette fonction |
| `preorder_draft` (1172 l.) | non | `ResponsePlan` + `confirmation_target` + `utils` | **CONVERSATIONAL_DOMAIN** |
| `procurement_draft` (989 l.) | non | idem | **CONVERSATIONAL_DOMAIN** |
| `sales_publish_draft` (778 l.) | non | idem | **CONVERSATIONAL_DOMAIN** |

### Le piège évité

Ma première vérification de pureté ne lisait que les imports en tête de
fichier : `pricing_tiers` semblait pur. Le contrat de dépendance a révélé
un **import paresseux à l'intérieur d'une fonction** :

```python
def pending_pack_count_tier(state):          # lit pending_interaction
    from …core.pending_interaction import InteractionKind, get_pending_interaction
```

Cette fonction est de l'**orchestration** (elle interroge l'état
conversationnel pour savoir si un nombre de paquets est attendu), pas une
règle de tarification. Déplacer le module tel quel aurait fait entrer
LangGraph dans le domaine partagé — exactement ce que la règle absolue du
mandat interdit.

Elle a donc été extraite dans
`graphs/agents/market_coach/domain/tier_interaction.py`, à côté de ses
deux seuls consommateurs (`interpreter/routing.py` et un test de contrat).

## 4. Modules moved

```
graphs/agents/market_coach/domain/pricing_tiers.py         → ladini/domain/pricing_tiers.py
graphs/agents/market_coach/domain/order_policy.py          → ladini/domain/order_policy.py
graphs/agents/market_coach/services/domain/quantity_unit.py → ladini/domain/quantity_unit.py
```

Déplacements via `git mv` (historique conservé), imports réécrits
mécaniquement dans **31 fichiers** (`src/` + `tests/`).

**Aucun shim de compatibilité** : les anciens chemins n'existent plus, il
n'y a donc qu'une seule implémentation et aucun chemin d'import
ambigu — vérifié par `test_no_duplicate_implementation_remains_under_graphs`.

Cible retenue : `ladini.domain`, le paquet déjà dédié au métier
(modèles ORM + DTO). Son `__init__` documente désormais la règle de
dépendance. Aucun nouveau paquet « shared/common » n'a été inventé.

## 5. Modules deliberately not moved

Les trois `*_draft` **restent** dans `graphs`. Ce ne sont pas des règles
partagées mais des **machines à états conversationnelles** :

* chacune définit son propre `*ResponsePlan` et sa fonction
  `build_response_plan` (présentation) ;
* chacune importe `core/confirmation_target` et `graphs/utils`
  (orchestration) ;
* `adapt_mcp_result` traduit un résultat d'outil en résultat de domaine.

Les déplacer aurait entraîné l'orchestration dans le domaine partagé, ou
exigé de scinder trois modules de 800 à 1 200 lignes sur le chemin CAS
durci — ce que le mandat interdit explicitement (§8, §23).

L'inversion résiduelle est donc **assumée et documentée**, et un test la
fige : `TestConversationalDraftsDeliberatelyStay` échoue si quelqu'un les
déplace sans traiter leur couplage.

## 6. Before dependency graph

```
services/database/{buyer,producer,product,escrow}
services/reconciliation/*, agents/forms
                    ↓
    graphs/agents/market_coach/domain/{pricing_tiers, order_policy,
                                       *_draft}
    graphs/agents/market_coach/services/domain/quantity_unit
```

## 7. After dependency graph

```
                 ladini.domain
        (quantity_unit · order_policy · pricing_tiers)
                ↙                     ↘
         services/                  graphs/
        (persistance)            (orchestration)

reste, volontairement :
    services/{*_draft_store, reconciliation}
                    ↓
    graphs/…/domain/{preorder,procurement,sales_publish}_draft
```

`ladini.domain` n'importe **ni** `graphs`, **ni** `api`, **ni**
`workers`, **ni** `infrastructure`, **ni** `protocols` — contrat testé.

## 8. Import-cycle impact

| Mesure | Avant | Après |
|---|---:|---:|
| Modules `graphs/**/domain` importés par les couches basses | 6 | **3** |
| Imports correspondants | 12 | **6** |
| Cycles entre couches | 11 | **10** |

Cycle supprimé : `domain_agent ↔ services_agent`. Les cycles restants
(`graphs ↔ services`, `services ↔ workers`, …) proviennent des drafts et
de l'Outbox transactionnel : ils étaient hors périmètre (§14 : ne pas
chercher à tous les supprimer).

## 9. Tests

| Test | Rôle |
|---|---|
| [test_service_does_not_depend_on_graph_domain.py](../tests/architecture/test_service_does_not_depend_on_graph_domain.py) | **AST**, pas grep : aucune couche basse n'importe une règle partagée depuis `graphs` ; les 3 modules vivent dans le paquet neutre ; **aucune implémentation dupliquée** ne subsiste sous `graphs` |
| `TestSharedDomainStaysIndependent` | le domaine partagé n'importe aucune couche technique — **c'est ce test qui a détecté l'import paresseux** de `pending_interaction` |
| `TestConversationalDraftsDeliberatelyStay` | fige la décision de non-déplacement des drafts (preuve du couplage : `ResponsePlan` + `confirmation_target`) |

Tests métier existants (pricing, minimum de commande, paliers, checkout,
preorder, procurement, sales publish) : **inchangés**, seuls leurs chemins
d'import ont suivi le déplacement des fichiers.

## 10. Regression

**Comportement identique, vérifié par instantané avant/après** sur :
validation de paliers, `compute_line` (10 L × 3 → 22 500 / base 30 ;
5 L × 2 → 8 000 / base 10), seuil minimum (50 KG accepté, 10 KG
`BELOW_MINIMUM`), parsing « 3 bidons de 10 litres » → 10 LITRE,
`normalize_unit`, `convert_quantity` (2 TONNE → 2 000 KG),
`default_unit_for_product`.

Seul écart : `tier_id`, généré par `uuid.uuid4().hex[:12]` quand il n'est
pas fourni — **nondéterministe par construction**, vérifié en exécutant
deux fois le même code (ids différents). Ce n'est donc pas une régression.

`pytest tests/` → **4 échecs, exactement les 4 historiques** de
`test_create_auction_catalog_gate.py`. Aucune nouvelle défaillance.
`test_write_capability_reachability.py` et `test_no_broken_user_goals.py`
restent verts.

## 11. Remaining architectural debt

| Dette | Sév. | Commentaire |
|---|---|---|
| `services/{*_draft_store, reconciliation} → graphs/…/*_draft` | P2 | assumée : les drafts sont conversationnels ; les scinder toucherait le chemin CAS durci |
| Cycles restants (10) | P2 | dominés par l'Outbox transactionnel (`services ↔ workers`) et les gateways (`graphs ↔ services`) — coût de lisibilité, pas de risque |
| Le paquet `ladini.domain` mélange désormais modèles ORM/DTO et règles pures | P3 | acceptable et documenté ; un sous-paquet `domain/rules/` serait cosmétique |

---

```
SHARED DOMAIN STATUS

Pricing:                      SHARED
Order policy:                 SHARED
Quantity/unit:                SHARED
Draft models:                 NOT SHARED (conversationnels, décision documentée)

services → graphs.domain:     3 modules / 6 imports   (drafts uniquement ; 6/12 avant)
Shared domain → graphs:       0
Behavior changes:             0
Tests:                        PASS
Regression:                   4 échecs historiques, aucun nouveau
```

```
ARCHITECTURAL EFFECT

Dependency inversion removed:   YES (pour les règles métier partagées)
Duplicate business logic:       NO  (déplacement, aucun shim, aucune copie)
Remaining cycles:               10 (11 avant)
New risks:                      aucun — déplacement purement structurel ;
                                aucune transaction, aucun verrou, aucune
                                idempotence, aucun Outbox, aucun MCP,
                                aucun ResponsePlan, aucun PendingInteraction
                                modifié
```
