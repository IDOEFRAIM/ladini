# PHASE 8 — MCP ALLOW-LIST HARDENING (2026-09-05)

## 1. Problem

L'exposition MCP reposait sur l'introspection de `AgriDatabaseService` :

```
méthode async publique  →  devient un outil MCP  →  TOOL_SCOPE_MAP décide
                                                     s'il est appelable
```

Modèle **opt-out / deny-by-omission** : ajouter une méthode créait un
outil, et un scope posé « par cohérence » ouvrait un accès. Quatre failles
en sont issues, toutes de la même racine :

| Faille | Ce qu'elle permettait | Fermée en |
|---|---|---|
| `update_order_status` | marquer n'importe quelle commande `PAID`/`COMPLETED`/`CANCELLED`, sans propriétaire | Phase 6B |
| `mark_escrow_paid` | déclarer un paiement reçu sans le fournisseur de paiement | Phase 7 |
| `expire_pending_payments` | annuler en masse les commandes en attente, sans acteur | Phase 7 |
| `cancel_preorder_draft(target_status)` | faire passer un brouillon à `COMPLETED` sans paiement ni stock | Phase 7 |

Chacune avait été fermée individuellement. **Cette phase supprime la
classe entière.**

## 2. Current exposure model (avant)

```python
def _compute_exposed_methods() -> list[str]:
    return sorted({
        name for name, m in inspect.getmembers(DatabaseService)
        if not name.startswith("_") and inspect.iscoroutinefunction(m)
    })
```

→ **113 outils exposés**, dont 82 sans aucun goal utilisateur.

## 3. New exposure model (après)

```
MCP_EXPOSED_TOOLS          →   TOOL_SCOPE_MAP        →   runtime
infrastructure/mcp/            infrastructure/mcp/       call_tool()
exposure.py                    security.py

« Cet outil existe-t-il ? »    « Qui a le droit ? »      « fail-closed »
```

`_compute_exposed_methods()` renvoie désormais
`introspection ∩ MCP_EXPOSED_TOOLS`. L'intersection (plutôt que la liste
brute) garantit qu'un nom mal orthographié ou une méthode supprimée ne
crée jamais un outil fantôme — la dérive est journalisée **et** testée.

`TOOL_SCOPE_MAP` **conserve** son rôle : autorisation runtime. Il n'est
simplement plus ce qui décide qu'une méthode *devient* un outil.

Politique complète : [MCP_TOOL_EXPOSURE_POLICY.md](MCP_TOOL_EXPOSURE_POLICY.md).

## 4. Complete tool inventory

Méthode d'établissement de l'allow-list — **mesurée, pas devinée** :

1. `tool_name` de tous les goals de `INTENT_CONFIG` qui résolvent vers une méthode réelle ;
2. **tout nom littéral** passé à un helper d'invocation, extrait par AST sur l'ensemble du paquet : `_call(...)`, `call_db(...)`, `call_tool(...)`, `invoke_tool(...)` ;
3. plus une décision explicite pour les outils photo (§6).

Deux itérations ont été nécessaires — et c'est le point important :

* la 1ʳᵉ détection (ligne à ligne) manquait les appels `_call(` **multi-lignes** : `cancel_pending_order`, `confirm_preorder_draft`, `create_preorder_draft` seraient tombés hors de la liste ;
* la 2ᵉ manquait `call_db(...)`, un **second helper d'invocation** utilisé par l'onboarding : `create_user_profile` serait tombé hors de la liste.

Sans le test d'équivalence, ces deux erreurs auraient cassé le checkout et
l'onboarding en production.

## 5. User-facing tools

**63 outils** exposés, groupés par domaine dans `exposure.py` : identité,
exploitation, catalogue, stock (lecture + récolte), production future,
panier/précommande, commandes, enchères/RFQ, négociation, escrow,
marché/référentiel, modération, finance, photos.

Tous ont un scope déclaré (vérifié par test dans les deux sens).

## 6. System-only tools

Non exposés — leurs appelants réels passent **directement** par le
service :

| Outil | Appelant réel |
|---|---|
| `mark_escrow_paid` | tâche IPN Paydunya |
| `expire_pending_payments` | cron `order_expiry` |
| `ensure_performance_indexes` | `api/tasks.py` au démarrage du worker |
| `check_and_expire_auctions` | balayage système |

**Cas nuancé — les outils photo.** Mon inventaire les avait classés
« système » parce que `workers/media/product_photo_task.py` appelle le
service en direct. La régression a fait échouer
`test_mcp_hardening.py::TestProductPhotoTool`, qui verrouille leur
exposition depuis le chantier « photo produit par WhatsApp ». J'ai donc
**restauré** `add_product_photo`/`add_bid_photo`/`add_auction_photo` : une
décision délibérée d'un chantier antérieur l'emporte sur une inférence
tirée du mode d'appel actuel. Le mode d'appel est un détail
d'implémentation ; la capacité produit, non.

## 7. Internal methods

Non exposées : `get_buyer_profile`, `get_producer_profile` (résolution
d'identité utilisée *dans* la couche service), `guess_category`,
`resolve_sub_category`, `normalize_unit`, ainsi que les lectures publiques
jamais câblées et le code mort (`finalize_multi_order`,
`update_production_visibility`, `toggle_product_availability`…).

**« Public » ≠ « outil MCP »** : ces méthodes restent appelables depuis le
code, ce qui est le mode d'appel normal des workers.

## 8. Security invariants

| Invariant | Où | Testé |
|---|---|---|
| Une méthode publique n'est pas un outil sans déclaration explicite | `exposure.py` + `h.py` | ✅ `test_a_new_public_method_is_not_exposed` |
| Tout outil exposé a un scope | cohérence des 2 listes | ✅ |
| Aucun nom déclaré sans méthode | intersection + log | ✅ |
| Outil inconnu → refusé | `runtime.py::call_tool` fail-closed | ✅ (préexistant) |
| Les 4 outils dangereux restent invisibles | allow-list | ✅ |
| Aucune capacité produit perdue | équivalence goals + appels littéraux | ✅ |

## 9. Migration

Une seule étape, sans période de compatibilité : l'introspection reste la
source des **méthodes candidates**, l'allow-list décide de l'exposition.
Aucun `tool_name` renommé. Aucune logique métier touchée : ni Order, ni
Bid, ni Auction, ni Product, ni Drafts, ni verrous, ni idempotence, ni
Outbox, ni ResponsePlan, ni PendingInteraction, ni LLM Gateway.

Aucun repli sur l'ancien comportement n'existe : si un outil requis
manquait dans la liste, l'appel échoue (fail-closed) et les tests
d'équivalence échouent — jamais un retour silencieux à « tout exposer ».

## 10. Tests

**Nouveau fichier** : [test_mcp_exposure_allowlist.py](../tests/architecture/test_mcp_exposure_allowlist.py) — 41 contrôles.

Validation par **mutation** (§26 du mandat) : `select_winning_bid` retiré
temporairement de l'allow-list → **3 gardes indépendants** échouent
(équivalence des goals, équivalence des appels littéraux, liste des
parcours critiques). Restauré → tout repasse. L'allow-list est donc
réellement porteuse, pas décorative.

## 11. Before / after

```
AVANT
  113 méthodes introspectées → 113 outils exposés
   39 scope WRITE · 32 scope READ · 41 sans scope (fail-closed)
   31 référencés par un goal · 82 sans aucun goal

APRÈS
   63 outils exposés (allow-list explicite)
   50 méthodes publiques rendues invisibles à MCP
    0 dérive (aucun nom déclaré sans méthode)
    0 outil exposé sans scope
    0 capacité produit perdue (équivalence prouvée)
```

Réduction de la surface d'attaque : **−44 %**.

## 12. Regression

`pytest tests/` → **4 échecs, exactement les 4 historiques** de
`test_create_auction_catalog_gate.py` (date codée en dur, antérieurs,
jamais modifiés). Aucune nouvelle défaillance.

Le 5ᵉ échec apparu en cours de phase (`TestProductPhotoTool`) était un
**vrai signal**, pas un faux positif : il a corrigé une décision d'exposition
erronée de ma part (§6).

`test_write_capability_reachability.py` et `test_no_broken_user_goals.py`
restent verts.

## 13. Remaining risks

| Risque | Sév. | État |
|---|---|---|
| Appels d'outils par nom **dynamique** (15 sites : exécuteur générique, `tool_provider`, dispatcher) | P3 | par construction, ils ne peuvent invoquer qu'un `tool_name` de goal — tous couverts par l'équivalence |
| L'allow-list doit être maintenue à la main | P3 | c'est le but : l'ajout devient une décision. Deux tests détectent la dérive dans les deux sens |
| `TOOL_SCOPE_MAP` contient encore des noms d'outils inexistants (goals cassés : `accept_bid`, `report_anomaly`, `*_by_id`) | P3 | sans effet : ces méthodes n'existent pas, donc l'intersection les élimine |

**Non entrepris volontairement** (§31) : le décorateur d'autorisation et
le déplacement du domaine partagé restent des chantiers distincts.
