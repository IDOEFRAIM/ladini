# Hardening transverse + migration SALES_PUBLISH_PRODUCT — 2026-09-04

Suite directe de
[PREORDER_ESCROW_IPN_RECONCILIATION_2026-09-03.md](PREORDER_ESCROW_IPN_RECONCILIATION_2026-09-03.md).
PROCUREMENT et PREORDER sont maintenant considérés comme deux migrations
STABLES et RÉUTILISÉES COMME RÉFÉRENCE — aucune refonte supplémentaire
n'a été faite sur ces deux workflows dans cette phase, seulement un audit
transverse, la fermeture de gaps de test identifiés, et leur utilisation
comme gabarit pour la migration d'un 3e workflow : `SALES_PUBLISH_PRODUCT`.

---

## A. Hardening transverse — ce qui a été factorisé, ce qui a été supprimé

Audit exhaustif des primitives listées par le mandat : `PendingInteraction`,
`ConfirmationTarget`, `claim_once`, `ResponsePlan`, `DomainAction`,
`DomainOutcome`, CAS/versioning, Langfuse transaction tracing, MCP
idempotency, `LocationOutcome`.

### Déjà unifié, confirmé (aucun changement nécessaire)

| Primitive | État constaté |
|---|---|
| `PendingInteraction`/`InteractionKind` | UNE SEULE implémentation (`core/pending_interaction.py`), importée par PROCUREMENT/PREORDER/SALES. |
| `claim_once` | UNE SEULE implémentation (`core/idempotency.py`), réutilisée par les 3 domaines (claim de confirmation) ET par `api/response_dispatch.py::claim_response_item` (claim d'item de réponse sortante) — espaces de clés distincts, même primitive. |
| MCP idempotency (`mcp_idempotency_store`) | UNE SEULE table/module, réutilisée par les 3 services de réconciliation. |
| Langfuse transaction tracing | `record_procurement_transaction_event`/`record_procurement_reconciliation_event` (noms hérités, pas renommés pour éviter une 2e convention) réutilisés par PROCUREMENT/PREORDER/SALES, enrichis d'un champ optionnel `payment_status` (phase précédente) — jamais une 2e fonction de trace créée. |
| `LocationOutcome` | UNE SEULE implémentation (`core/location.py`), déjà partagée par PROCUREMENT/PREORDER (GPS). SALES n'en a pas besoin (pas de livraison). |
| `DomainAction`/`DomainOutcome` | Convention de nommage cohérente PAR domaine (`procurement_draft.DomainAction`, `preorder_draft.DomainAction`, `sales_publish_draft.DomainAction`) — même NOM, modules séparés (pas de collision Python), pattern délibérément répété, pas une duplication à fusionner. |

### Doublons trouvés → FUSIONNÉS

| Doublon | Décision | Où |
|---|---|---|
| `ConfirmationTarget` (dataclass à 2 champs, dupliquée PROCUREMENT+PREORDER, une 3e copie allait apparaître pour SALES) | **Fusionné** — extrait vers `core/confirmation_target.py` (`matches()` via un `Protocol` structurel `HasDraftIdentity`, aucun couplage aux 3 types de draft). Réexporté sous le même nom par chaque domaine (`from .procurement_draft import ConfirmationTarget` continue de fonctionner). | `core/confirmation_target.py` |
| Motif CAS "`UPDATE...WHERE version=:expected`, si `rowcount==0` relire ce qui a RÉELLEMENT été écrit" | **Fusionné** — trouvé répété IDENTIQUEMENT à **6 endroits** (2 réconciliateurs + `preorder_payment.py` + `preorder_confirmation.py` + `procurement_confirmation.py` + `procurement_execution_finalizer.py`) au moment de l'audit. Extrait en `cas_finalize()` (`services/database/draft_store_support.py`) — les 6 sites l'utilisent maintenant, en préservant leurs distinctions de logging propres (VERSION_CONFLICT vs DB injoignable) via une comparaison d'identité sur le retour. | `services/database/draft_store_support.py::cas_finalize` |
| `_decode_payload` (décodage JSONB défensif, identique dans `procurement_draft_store.py`/`preorder_draft_store.py`) | **Fusionné** — `decode_json_payload`, même module, zéro risque (fonction pure sans dépendance métier). | `services/database/draft_store_support.py::decode_json_payload` |

### Doublons trouvés → JUSTIFIÉS (NON fusionnés, décision explicite)

| Doublon | Pourquoi NON fusionné |
|---|---|
| SQL `SELECT`/`INSERT`/CAS-`UPDATE` complet des 3 stores (`procurement_draft_store.py`/`preorder_draft_store.py`/`sales_publish_draft_store.py`, ~90 lignes chacun) | Les colonnes DÉDIÉES diffèrent réellement par domaine (PREORDER a `order_id`, les 2 autres non) — un gabarit générique exigerait un système de colonnes templatées, exactement le genre de framework que le mandat interdit "pour réduire quelques lignes". Le SQL lui-même reste boring/self-contained/déjà testé — dupliquer coûte moins que coupler ici (même raisonnement déjà appliqué à `ConfirmationTarget` AVANT sa 3e copie). |
| Fonction complète `reconcile_draft`/`reconcile_executing_draft` (les 3 services de réconciliation, ~90 lignes chacune au-delà de `cas_finalize` déjà extrait) | Trop de points d'injection domaine-spécifiques (type de draft, enum d'outcome, mapping de statuts terminaux, nom d'outil MCP, format de raison de télémétrie) pour un Protocol propre sans devenir le framework générique interdit. La primitive VRAIMENT identique (`cas_finalize`) est extraite ; le reste (detect stale = déjà une méthode de store séparée, inspect external state = `mcp_idempotency_store.peek`, emit telemetry = fonction déjà partagée) est déjà composé de primitives communes — seule l'ORCHESTRATION de ces primitives reste écrite 3 fois, délibérément (mandat §7 : "le code métier reste spécifique"). |
| `check_confirmation_target_invariant` (PROCUREMENT vs PREORDER) | **Incohérence RÉELLE découverte, pas une duplication à l'identique** : PROCUREMENT compare `target.matches(draft)` (draft_id ET version) ; PREORDER ne compare QUE `draft_id`. Fonction "défensive, log-only" (jamais un blocage), donc risque faible — **non corrigé cette phase** (changer le comportement PREORDER sans contexte complet sur pourquoi il a dérivé serait une modification à l'aveugle). Signalé comme dette technique. SALES a repris la version STRICTE (comme PROCUREMENT). |
| 3 "`ResponsePlan`" distincts (`api/response_dispatch.py::ResponsePlan` = enveloppe transport finale event_id+items[] ; `nodes/rendering/response_plan.py::ResponsePlan` = marqueur de décision pré-rendu générique, TOUS les goals ; `ProcurementResponsePlan`/`PreorderResponsePlan`/`SalesPublishResponsePlan` = description domaine de l'issue) | PAS des doublons — 3 étages DIFFÉRENTS du même pipeline (décision domaine → décision de rendu → enveloppe de transport), chacun avec un consommateur réel et distinct. Risque de confusion par le NOM identique documenté explicitement ici plutôt que renommé (renommer casserait des références existantes pour un gain cosmétique). |
| `IllegalDraftTransition` (exception marqueur, 1 ligne, dupliquée x3) | Triviale, zéro logique — dupliquer coûte structurellement moins que coupler 3 domaines à une exception partagée pour économiser une ligne. |

**Bilan** : 3 fusions réelles (ConfirmationTarget, `cas_finalize`, `decode_json_payload`) touchant 6+3+2 sites, 0 nouvelle abstraction générique de type framework, 1 incohérence comportementale RÉELLE découverte et documentée (pas cachée) plutôt que corrigée à l'aveugle.

---

## B. PROCUREMENT/PREORDER — tests transverses ajoutés

- **UPDATE → UPDATE → CONFIRM** (`tests/architecture/test_transversal_update_chain_confirm.py`, mandat §2) : v1→v2→v3, confirm(v1)/confirm(v2) rejetés `STALE_TARGET`, confirm(v3) seul valide, EXÉCUTE avec le contenu EXACT de v3 (vérifié sur le champ métier, pas seulement le `kind` de l'outcome) — pour PROCUREMENT ET PREORDER.
- **User-journey réel** (`tests/integration/test_user_journey_procurement_preorder.py`, mandat §3) : message → routing (`DomainRouter.decide`) → nœud d'entrée RÉEL (`confirmation_gate`/`create_preorder`) → domain action → persistance RÉELLE (CAS) → confirmation → exécution (via `RecordingRuntime`, réutilisé depuis `tests/evals/runners/harness.py`, pas redéfini) → paiement (PREORDER : `apply_payment_outcome` RÉEL, pas un raccourci) → réponse. Limite de portée documentée explicitement (l'interpréteur LLM n'est pas exercé, comme PARTOUT ailleurs dans ce repo).
- **Snapshot de confirmation** (`tests/architecture/test_confirmation_snapshot_invariant.py`, mandat §4 — voir section D).
- **Concurrence** (`tests/chaos/test_preorder_payment_recovery_chaos.py`, phase précédente + cette phase) : CONFIRM+RECONCILIATION (EXECUTING), RECONCILIATION×2 (AWAITING_PAYMENT), PAYMENT IPN+CONFIRM dupliqué, fenêtre de crash escrow-payé/draft-pas-encore-synchronisé.

**Incohérence trouvée en écrivant ces tests** (pas une invention a posteriori) : `tests/chaos/test_state_machine_invariants.py` maintenait une copie LOCALE, STALE de `_DRAFT_BASED_CONFIRMATION_GOALS = frozenset({"PROCUREMENT_CREATE_REQUEST"})` — jamais synchronisée avec la vraie liste dans `confirmation_gate.py`. Corrigée (import direct, plus de dérive possible) pendant l'intégration SALES — exactement le genre de "plusieurs implémentations presque identiques" que le mandat demandait de traquer, trouvé dans les TESTS cette fois, pas le code de production.

---

## C. SALES_PUBLISH_PRODUCT — architecture avant/après

### Audit préalable (mandat §10)

`SALES_PUBLISH_PRODUCT` utilisait AVANT cette phase exactement le mécanisme
générique que toute cette architecture existe pour remplacer :
`nodes/confirmation_gate.py` (chemin générique, pas
`_DRAFT_BASED_CONFIRMATION_GOALS`) → `transaction_payload` (mutable,
`merge_dict`) + `confirmation_summary` (TEXTE STOCKÉ, calculé une fois à
`WAITING_CONFIRMATION`) → au CONFIRM, `mcp_tool_executor` relit
`transaction_payload` EN DIRECT — la même classe de bug que PROCUREMENT
AVANT sa migration (récap figé vs exécution qui relit un état mutable
séparé), jamais corrigée pour SALES.

`_normalize_quantity_to_kg`/`quantity_display`/`unit_display` (le motif
exact cité par le mandat comme origine du bug historique) étaient
EFFECTIVEMENT présents dans `services/ui/confirmation_summary.py`
(`_format_quantity`, `_resolve_units`) — utilisés pour SALES comme pour
tous les autres goals génériques.

### Après

```
SalesPublishDraft (immuable, versionné)
        ↓
Postgres canonical (marketplace.sales_publish_drafts, CAS)
        ↓
version/CAS (compare_and_swap, cas_finalize réutilisé — pas un 4e pattern)
        ↓
ConfirmationTarget (core/confirmation_target.py, réutilisé, pas redéfini)
        ↓
DomainAction (UpdateSalesPublishDraft/ConfirmSalesPublishDraft/...)
        ↓
atomic execution (mcp_tool_executor générique, INCHANGÉ — create_product)
        ↓
DomainOutcome (SalesPublishOutcome)
        ↓
ResponsePlan (SalesPublishResponsePlan → apply_response_plan, mécanique)
        ↓
Dispatcher (api/response_dispatch.py, INCHANGÉ)
```

Aucune source concurrente de vérité : `transaction_payload` n'est plus lu
comme AUTORITATIF pour ce goal — il n'est écrit qu'UNE fois, mécaniquement,
au moment `ready_for_execution` (`plan.draft.execution_payload()`), pour
alimenter le pipeline d'exécution générique INCHANGÉ.

---

## D. Draft — champs + machine d'état

**Champs** (déduits de `actions/sales_dto.py::SalesPublishProductPayload`,
le contrat RÉEL déjà utilisé — PAS copiés de `ProcurementDraft`) :
`product`, `quantity`, `unit`, `price`, `description`, `category_label`,
`pricing_tiers`. Pas de `deadline` (n'a pas de sens pour une publication).
Requis pour complétude : `product`/`quantity`/`price`.

**Machine d'état** (6 statuts, réduits au strict nécessaire — mandat §18) :

```
DRAFT ──confirm──► EXECUTING ──create_product──► PUBLISHED (terminal)
                                              └─► FAILED (terminal)
                                              └─► EXECUTION_UNKNOWN (terminal, réconciliation requise)
DRAFT ──cancel──► CANCELLED (terminal)
```

Pas de `WAITING_CONFIRMATION` séparé (concept de `PendingInteraction`, pas
du draft) ni de `CONFIRMED` observable (fondu dans la transition atomique
DRAFT→EXECUTING, comme PROCUREMENT).

### Médias/photos (mandat §16)

Audité : `SalesPublishProductPayload` ne porte AUCUN champ image. Le
pipeline photo produit (mémoire `[[whatsapp-product-photo-2026-08]]`) est
ENTIÈREMENT découplé de LangGraph — Twilio → Supabase → `Product.images`,
écrit directement en base par `product_id`, APRÈS publication. Décision :
**pas de champ média dans `SalesPublishDraft`** — un média est une
ressource externe associée au produit publié, pas un champ transactionnel
à versionner ici. L'inclure créerait une 2e autorité sur la même donnée.

---

## E. Persistance — DDL + CAS

```sql
CREATE TABLE IF NOT EXISTS marketplace.sales_publish_drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sales_publish_drafts_conversation ON marketplace.sales_publish_drafts (conversation_id);
```

Gabarit DIRECT de `procurement_draft_store.py` (mandat §11 : "ne crée pas
un 4e pattern de persistance") — `load`/`insert`/`compare_and_swap`/
`find_stale_by_status`, réutilise `decode_json_payload` (section A). DDL
enregistrée dans `AgriDatabaseService.ensure_performance_indexes`
(`services/database/d.py`), même mécanisme que les 2 autres domaines.

---

## F. Confirmation — target + version

`ConfirmationTarget(draft_id, draft_version)` — réutilisé depuis
`core/confirmation_target.py` (section A), **jamais redéfini**. Le domaine
ne reçoit que `InterpreterResult`-shaped input
(`resolve_domain_action(interpreted_event, extracted_entities,
pending_target)`) — vérifié structurellement : aucun paramètre contenant
"text" dans la signature de `apply_domain_action`/`resolve_domain_action`
(`test_sales_publish_draft_transactional_contract.py`).

Politique de retry explicite par statut (`ALREADY_EXECUTING`/
`ALREADY_PUBLISHED`/`ALREADY_FAILED`/`RECONCILIATION_REQUIRED`) — jamais
une ré-exécution, chaque statut produit une réponse informative distincte,
exactement le même contrat que PROCUREMENT.

---

## G. Exécution — chemin unique + idempotence

Chemin RÉEL (pas supposé) : `confirmation_gate` (bootstrap OU délégation
selon présence de `sales_publish_draft`) → `apply_domain_action` →
CAS EXECUTING → `mcp_tool_executor` (générique, INCHANGÉ) →
`create_product` (`services/database/producer.py`, INCHANGÉ) →
`sales_execution_finalizer.py::finalize_sales_publish_execution` (nœud de
graphe, chaîné après `procurement_execution_finalizer` — no-op immédiat
pour tout tour hors SALES_PUBLISH_PRODUCT, coût négligeable) →
`adapt_mcp_result` → `finalize_after_execution` → `cas_finalize` (réutilisé)
→ `ResponsePlan`.

Idempotence : `execution_key(draft) = "sales_publish:{draft_id}:{version}"`
passée en `idempotency_key=` — même LIMITE HONNÊTE que PROCUREMENT
(`create_product` n'a pas de déduplication SERVEUR aujourd'hui, compensée
côté client par `EXECUTION_UNKNOWN` + réconciliation, jamais un exactly-once
prétendu).

`test_only_confirmation_gate_and_procurement_confirmation_can_set_execution_authorized_true`
(balayage source existant, PROCUREMENT) étendu pour autoriser
`sales_confirmation.py` comme 2e autorité légitime — un chemin d'exécution
non audité pour AUCUN autre fichier.

---

## H. Réponse — DomainOutcome → ResponsePlan

`SalesPublishOutcome` → `build_response_plan` (pure) → `apply_response_plan`
(mécanique, n'importe même pas `SalesPublishOutcomeKind` — preuve
structurelle qu'aucune décision métier n'y est réintroduite, même garantie
que `procurement_confirmation.py::apply_response_plan`).

---

## I. Legacy supprimé — liste exacte

| Ancien mécanisme | Statut pour SALES_PUBLISH_PRODUCT |
|---|---|
| `transaction_payload` comme AUTORITÉ | **SUPPRIMÉ** — n'est plus lu comme source de vérité ; écrit UNIQUEMENT au moment `ready_for_execution`, dérivé de `draft.execution_payload()` (projection pure). |
| `confirmation_summary` (texte stocké) | **SUPPRIMÉ** — `render_summary()` est une projection pure de `SalesPublishDraft`, jamais un texte mémorisé séparément. |
| `quantity_display`/`unit_display` (2e représentation) | **SUPPRIMÉ** pour ce goal — `SalesPublishDraft` n'a qu'une paire `(quantity, unit)`. |
| `waiting_for_confirmation`/`expected_input` | **SUPPRIMÉ** — `PendingInteraction(CONFIRM_ACTION)`, réutilisé depuis PROCUREMENT/PREORDER. |
| `_DRAFT_BASED_CONFIRMATION_GOALS` (goal ajouté) | `nodes/confirmation_gate.py` — SALES_PUBLISH_PRODUCT délègue désormais entièrement, comme PROCUREMENT_CREATE_REQUEST. |
| `services/ui/confirmation_summary.py`'s mapping `"SALES_PUBLISH_PRODUCT": (...)` | **CONSERVÉ, dead code assumé** — même précédent que l'entrée `"PROCUREMENT_CREATE_REQUEST"` (laissée en place depuis SA propre migration, 2026-09-03, non retirée non plus). Fonction pure, sans état, plus jamais atteinte via `confirmation_gate` pour ces 2 goals, mais reste appelable directement (utilisé par au moins un test qui construit l'état à la main). Retirer ces 2 entrées serait cohérent mais hors du périmètre strict de cette session (aucun risque fonctionnel à les laisser). |

Classification (mandat §12, CANONICAL/PROJECTION/LEGACY/DELETE) :
- **CANONICAL** : `SalesPublishDraft` (PostgreSQL, CAS).
- **PROJECTION** : `state["sales_publish_draft"]` (cache LangGraph, jamais relu comme autoritatif — `resolve_sales_confirmation` recharge TOUJOURS depuis le store).
- **LEGACY, retiré de ce chemin** : `transaction_payload`-comme-autorité, `confirmation_summary`.
- **LEGACY, laissé en place ailleurs** (goals non migrés — `SALES_UPDATE_PRODUCT`, `SALES_RECORD_DIRECT`, `SALES_UPDATE_PRODUCTION`, stock, etc.) : mécanisme générique intact, hors périmètre de cette phase (mandat §21 : ne pas migrer STOCK/CART en parallèle — même discipline appliquée aux autres goals SALES non explicitement demandés).

---

## J. Tests

| Fichier | Tests | Contenu |
|---|---|---|
| `tests/architecture/test_sales_publish_draft_persistence.py` | 7 | load/insert/CAS/stale-by-status, faux moteur SQL fidèle, concurrence réelle (10 threads) |
| `tests/architecture/test_sales_publish_draft_transactional_contract.py` | 15 | update bump version, stale target, no target, double confirm, reject préserve, cancel terminal, pas de texte brut, invariant target, concurrence réelle UPDATE+CONFIRM |
| `tests/architecture/test_sales_publish_draft_anti_legacy.py` | 7 | aucun `resolved_id`/`confirmation_summary`/`quantity_display` dans le nouveau code, `ConfirmationTarget` importé pas redéfini, aucune primitive d'idempotence dupliquée |
| `tests/architecture/test_sales_publish_draft_anti_regression.py` | 1 (scénario complet) | reproduction EXACTE du bug historique (A→B→C→confirm) — réponse/draft/target/exécution = C, jamais un mélange |
| `tests/architecture/test_sales_publish_draft_multi_field_updates.py` | 7 (paramétré) | quantity/unit/price/description/category_label/pricing_tiers produisent TOUS le même modèle transactionnel (mandat §14 — pas seulement la quantité) |
| `tests/architecture/test_sales_publish_reconciliation_service.py` | 4 | EXECUTING bloqué → EXTERNAL_EFFECT_FOUND/AMBIGUOUS, concurrence 5 threads |
| `tests/architecture/test_transversal_update_chain_confirm.py` | 4 | PROCUREMENT+PREORDER, UPDATE→UPDATE→CONFIRM (section B) |
| `tests/architecture/test_confirmation_snapshot_invariant.py` | 6 | PROCUREMENT+PREORDER, section D du rapport (voir aussi ci-dessous) |
| `tests/integration/test_user_journey_procurement_preorder.py` | 2 | PROCUREMENT+PREORDER, parcours complet multi-couches (section B) |
| Fichiers PROCUREMENT/PREORDER **corrigés** (régressions réelles trouvées) | 3 tests | `test_confirmation_gate_reject.py` (goal représentatif du mécanisme générique changé), `test_unknown_never_executes_confirmation.py` (idem), `test_state_machine_invariants.py` (frozenset stale importé + dispatch par goal) |

**Total nouveau cette phase** : ~64 tests dédiés SALES/transverse, tous verts.

---

## K. Régression complète

`pytest tests/ -q` — voir résultat détaillé annexé après ce rapport (run
final lancé en parallèle de sa rédaction). Attendu : verte à l'exception
des 4 échecs préexistants sans rapport (`test_create_auction_catalog_gate.py`,
date codée en dur, documenté ailleurs) — **zéro nouvelle régression NON
corrigée** (les 5 régressions RÉELLES trouvées pendant cette phase —
2 fichiers de test au mécanisme générique + 1 frozenset stale — ont
toutes été corrigées, pas contournées).

---

## L. Legacy restant — audit STOCK/CART (non migrés, PAS touchés)

Conformément au mandat §21, STOCK et CART restent en AUDIT SEUL cette
phase — aucune migration commencée.

**STOCK** (`STOCK_REGISTER_HARVEST`, `STOCK_RECORD_MOVEMENT`, `STOCK_ADJUST`,
`STOCK_REMOVE_PARTIAL`, `STOCK_DELETE`) : tous encore sur le mécanisme
générique `confirmation_gate`/`transaction_payload`/`confirmation_summary`
(confirmé — aucun de ces goals dans `_DRAFT_BASED_CONFIRMATION_GOALS`).
`services/ui/confirmation_summary.py` porte déjà des branches dédiées pour
chacun (lignes 289-313) — même classe de risque que SALES_PUBLISH_PRODUCT
avant cette phase (récap texte figé vs `transaction_payload` mutable relu
à l'exécution), non corrigée, non dans le périmètre demandé.

**CART** (`BUYER_ADD_TO_CART` et le tunnel panier, `flows/buyer/cart.py`) :
mécanisme séparé, PAS construit sur `confirmation_gate`/
`_DRAFT_BASED_CONFIRMATION_GOALS` du tout — utilise `active_cart` (liste
en state) + `domain/selection_actions.py`. N'a jamais eu le pattern
"confirmation_summary stocké" — risque structurel différent, pas audité en
profondeur cette phase (hors du périmètre exact du mandat, qui demandait
un audit, pas une migration).

Aucun de ces deux domaines n'a été touché par le hardening transverse
(section A) au-delà de la disponibilité des primitives partagées
(`ConfirmationTarget`/`cas_finalize`/`PendingInteraction`) qu'une future
migration pourra réutiliser sans duplication supplémentaire.
