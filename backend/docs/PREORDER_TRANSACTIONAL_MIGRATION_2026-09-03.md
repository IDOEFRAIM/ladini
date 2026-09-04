# Migration transactionnelle PREORDER — 2026-09-03

Migration complète de `BUYER_PREORDER_INIT`/`BUYER_PREORDER_CONFIRM` vers
une architecture canonique — même standard que la refonte PROCUREMENT
(4 mandats précédents), sans copier-coller aveugle : `PreorderDraft` est
construit à partir de l'audit réel du workflow, pas des champs de
`ProcurementDraft`. Voir aussi
[PROCUREMENT_RECOVERY_AND_MCP_IDEMPOTENCY_2026-09-03.md](PROCUREMENT_RECOVERY_AND_MCP_IDEMPOTENCY_2026-09-03.md),
section G, pour l'audit qui a précédé cette migration.

---

## A. Architecture PREORDER finale

```
USER MESSAGE
    ↓
InterpreterResult (interpreted_event, extracted_entities — déjà produit en amont)
    ↓
flows/buyer/preorder.py::create_preorder   [traduit resolved_id → signal, JAMAIS un contrôleur]
    │
    ├── aucun draft actif → preorder_confirmation.py::bootstrap_preorder_draft
    │       → PreorderGateway.create_draft(idempotency_key=creation_key(phone, cart_fingerprint))
    │       → PreorderDraft.new(order_id=..., items=...) → INSERT PostgreSQL (v1)
    │
    └── draft actif → preorder_confirmation.py::resolve_preorder_confirmation
            → resolve_domain_action (CONFIRM/REJECT/CANCEL/UPDATE)
            → apply_domain_action        [SEULE autorité de mutation, CAS PostgreSQL]
            → (CONFIRM sans lieu connu)  → PendingInteraction(PROVIDE_LOCATION), draft INCHANGÉ
            → (CONFIRM avec lieu connu)  → _confirm_to_executing (1 seul bump de version)
                    → CAS persist EXECUTING AVANT tout appel MCP
                    → PreorderGateway.confirm_draft / EscrowGateway.initiate_escrow_payment
                      (idempotency_key=execution_key(draft), RÉELLEMENT dédupliqué serveur)
                    → adapt_mcp_result/adapt_escrow_result → finalize_after_execution
                    → CAS persist EXECUTED/FAILED/EXECUTION_UNKNOWN/AWAITING_PAYMENT
            → build_response_plan (PUR) → apply_response_plan (MÉCANIQUE)
```

`PreorderDraft` = **seule source de vérité métier** du brouillon. PostgreSQL
(`marketplace.preorder_drafts`) = **persistance canonique**, protégée par
`version`/CAS (même primitif que PROCUREMENT). `preorder_workflow` (state
LangGraph) devient une **projection dérivée**, réécrite à un seul endroit
(`apply_response_plan::_phase_projection`) — jamais une autorité indépendante.

---

## B. Avant / après — chaque ancienne source de vérité

| Ancienne source | Rôle avant | Statut après |
|---|---|---|
| `preorder_workflow["phase"]` | Pivot de routage, écrit à **~15 endroits** dans `preorder.py` seul (+ `cart_service.py`, `cognitive.py`, `goal_planner.py`) | **PROJECTION** dérivée de `draft.status`, écrite à UN SEUL endroit (`_phase_projection`) pour un draft actif. Les écritures restantes (`cart_service.py`, `cognitive.py`, `goal_planner.py`, les branches CANCEL/ADD_MORE hors-draft de `preorder.py`) sont des **RESETS légitimes** (retour à CART avant/hors qu'un draft existe) — non touchées, non problématiques (jamais en conflit avec un draft ACTIF). |
| `preorder_workflow["preorder_id"]`/`["total_amount"]` | Copie indépendante du résultat MCP | **PROJECTION** de `draft.order_id`/`draft.total_amount`. |
| `preorder_workflow["gps_stage"]`/`["gps_default"]` | État de l'étape GPS | **CONSERVÉ tel quel** — légitimement éphémère/conversationnel (suggestion de point, pas un fait métier durable), jamais la cause de bug identifiée par l'audit. `PendingInteraction(PROVIDE_LOCATION)` reste l'autorité sur "qu'attend le prochain message", `gps_default` n'est qu'une donnée d'accompagnement. |
| `transaction_payload["resolved_id"]` | Contrôlait CANCEL/ADD_MORE/CONFIRM à travers TOUT le fichier (relu dans 3 branches différentes de l'ancien `create_preorder`) | **TRADUIT une seule fois**, en tête de `create_preorder`, vers soit une branche d'orchestration explicite (CANCEL/ADD_MORE, hors draft), soit `interpreted_event="CONFIRM"` avant délégation — ne pilote plus AUCUNE décision au-delà de cette traduction. `resolve_domain_action` ne connaît même pas son existence (vérifié : absent de sa signature). |
| `active_cart` | Lu à la fois pour composer le récap ET pour la collecte panier | **SOURCE uniquement pour l'AMORÇAGE** du draft (`items_payload` construit une fois, à la création). Dès qu'un draft existe, `draft.items` est l'UNIQUE source du récap/résumé de commande (vérifié : `apply_response_plan`/`render_summary` ne lisent jamais `active_cart`). |
| `Order(status=DRAFT)` côté serveur (`create_preorder_draft`) | Seule trace durable du brouillon, sans version/CAS locale | **RESTE la source de vérité DURABLE finale** (débit stock réel) — désormais MIROITÉE par `PreorderDraft`/`preorder_drafts`, qui ajoute le versionnement/CAS/machine à état qui manquaient localement. La garde serveur `SELECT...FOR UPDATE`/`status != DRAFT` de `confirm_preorder_draft` est **préservée intégralement**, jamais retirée (mandat §14). |
| `_execute_confirm` (fonction, 235 lignes) | Mélangeait décision, résolution GPS, 2 chemins d'exécution (escrow/non-escrow), gestion d'erreur, construction de patch — dans UNE fonction | **SUPPRIMÉE**, remplacée par `_execute_and_finalize` (execution) + le corps de `resolve_preorder_confirmation` (décision/GPS) — responsabilités séparées, comme PROCUREMENT. |
| `PreorderPhase` (classe `contexts.py`) | Wrapper typé pour `preorder_workflow` | **JAMAIS UTILISÉE** ailleurs que sa propre déclaration (vérifié par recherche globale) — code mort, PAS lié à cette migration, signalé en section M/legacy, non supprimé (hors périmètre : aucun appelant à casser). |

---

## C. Persistance — DDL + version + CAS

```sql
CREATE TABLE IF NOT EXISTS marketplace.preorder_drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_preorder_drafts_conversation ON marketplace.preorder_drafts (conversation_id);
```

Gabarit DIRECT de `procurement_draft_store.py` (même schéma, même CAS
`UPDATE ... WHERE draft_id = :id AND version = :expected_version` →
`rowcount==1` gagné / `0` → `VERSION_CONFLICT`) — décision snapshot
versionné (pas event-log) justifiée de façon identique (voir le rapport
PROCUREMENT, section B, même raisonnement applicable ici).

`PreorderDraft` — champs DÉDUITS du workflow réel (pas copiés de
`ProcurementDraft`) :
```python
draft_id, version, status,
order_id,                     # id Order côté serveur — connu dès new() (§ D)
items: Tuple[Dict, ...],      # snapshot immuable des lignes de commande
total_amount, currency,
delivery_zone_id, payment_method,
delivery_lat, delivery_lon,   # posés SEULEMENT au moment de EXECUTING
created_at
```

---

## D. Machine d'état

```
DRAFT
  ├── UPDATE  → DRAFT(v+1)                         [nouveau panier, nouveau order_id déjà obtenu]
  ├── CANCEL  → CANCELLED (terminal)
  └── CONFIRM
        ├── (pas de lieu connu)  → DRAFT inchangé, PendingInteraction(PROVIDE_LOCATION)
        └── (lieu connu)         → EXECUTING (1 seul bump de version, jamais observable via CONFIRMED)
                                        ├── EXECUTED             [confirm_draft réussi]
                                        ├── FAILED               [confirm_draft échec confirmé]
                                        ├── EXECUTION_UNKNOWN    [ambigu — timeout/exception]
                                        └── AWAITING_PAYMENT     [escrow — réservé, débit DIFFÉRÉ à l'IPN]
```

Différence structurelle assumée vs PROCUREMENT (documentée dans
`domain/preorder_draft.py`) : la création du draft LOCAL n'a lieu
qu'APRÈS le succès de l'appel MCP `create_preorder_draft` — pas de statut
`CREATING` séparé, l'idempotence de CRÉATION (mandat §15) est gérée par
`mcp_idempotency_store` (réutilisé tel quel depuis PROCUREMENT), pas par
la machine à état locale.

`EXECUTION_UNKNOWN`/`AWAITING_PAYMENT` sont **terminaux** dans cette
machine — pas de transition automatique sortante, même doctrine que
PROCUREMENT ("nécessite une réconciliation humaine, jamais un retry
aveugle"). **Limite verrouillée par un test dédié** :
`test_agent_hard_resilience.py::test_confirmation_failure_is_reported_as_execution_unknown_not_a_silent_retry`
— une exception réseau pendant `confirm_draft` transite désormais vers
`EXECUTION_UNKNOWN`, PAS un "réessayez juste" silencieux comme avant
cette migration (comportement changé DÉLIBÉRÉMENT, moins pratique mais
correct : un timeout est authentiquement ambigu, deviner "aucun effet"
aurait pu laisser rejouer un débit stock déjà survenu côté serveur).

---

## E. Confirmation — cible, consommation, retry

`ConfirmationTarget(draft_id, draft_version)` — dupliqué (pas partagé)
depuis `procurement_draft.py` (mandat §27 : un type à 2 champs ne justifie
pas un couplage). `apply_domain_action`'s branche CONFIRM applique la
MÊME précédence que le correctif trouvé côté PROCUREMENT ce même jour :
politique de retry par statut évaluée AVANT la correspondance de version
(un retry légitime sur un draft déjà `EXECUTING` obtient `ALREADY_EXECUTING`,
jamais `STALE_TARGET`) — appliquée ICI dès la conception, pas après coup.

Retry (double CONFIRM) : `claim_once(preorder_confirm:{draft_id}:{version})`
— même primitif partagé que PROCUREMENT (mandat §16, "ne recrée pas un
système parallèle").

---

## F. Exécution — tous les effets externes et leur sémantique

| Effet | Déclencheur | Idempotence | Sémantique |
|---|---|---|---|
| `create_preorder_draft` (Order DRAFT) | Bootstrap | `idempotency_key = preorder-create:{phone}:{cart_fingerprint}` — RÉELLEMENT dédupliqué (table `mcp_idempotency_records`, chantier PROCUREMENT) | Aucune garde serveur pré-existante (retry = risque de doublon AVANT ce chantier) — fermé par la clé d'idempotence. |
| `confirm_preorder_draft` (débit stock, Order DRAFT→CONFIRMED) | CONFIRM, non-escrow | `idempotency_key = preorder:{draft_id}:{version}` (dédup RÉELLE) **+** garde serveur `SELECT...FOR UPDATE`/`status != DRAFT` (préservée, DOUBLE protection) | `PreorderExecutionResult(success, order_id, order_number)` — `EXECUTED`. |
| `initiate_escrow_payment` (réservation + lien Paydunya) | CONFIRM, escrow activé | `idempotency_key` (même primitif) — PAS de garde `status != DRAFT` équivalente connue côté ce tool spécifique | `PreorderExecutionResult(success, checkout_url, ttl_hours)` — `AWAITING_PAYMENT`, PAS `EXECUTED` (débit réel différé, hors de ce nœud — voir G). |

Deux adaptateurs DISTINCTS (`adapt_mcp_result`/`adapt_escrow_result`) —
jamais un `success=True` générique masquant LEQUEL des deux effets a eu
lieu (mandat §17).

---

## G. Idempotence — création, confirmation, effets externes

- **Création** : `creation_key(phone, cart_fingerprint)` — stable tant que
  panier+acheteur sont identiques (PAS basé sur `draft_id`, qui n'existe
  pas encore avant l'appel). Verrouillé : `TestInvariantJ` (2 tests).
- **Confirmation** : `claim_once` (Redis) + `execution_key` (table MCP) —
  DEUX mécanismes complémentaires, même distinction que le rapport
  PROCUREMENT (claim = dédup locale/course ; table MCP = dédup requête ;
  CAS version = course d'état métier).
- **Escrow** : limite HONNÊTE — `initiate_escrow_payment` n'a PAS de garde
  serveur `status != DRAFT` connue équivalente à `confirm_preorder_draft`
  (vérifié dans `services/database/buyer.py` : cette méthode n'a pas été
  auditée en détail dans ce chantier, HORS périmètre — seule la couche
  MCP (`idempotency_key`) protège ce chemin).

---

## H. Recovery — crash, timeout, redémarrage worker

Réutilise **intégralement** l'infrastructure construite pour PROCUREMENT
(mandat §25 : "ne crée pas un deuxième pattern de recovery") :
- `preorder_draft_store.find_stale_executing(older_than_seconds)` — même
  requête (`updated_at` comme `started_at` implicite), même faux moteur
  testé (`test_preorder_draft_persistence.py`, 2 tests staleness).
- **NON câblé cette session** : un cron `workers/crons/preorder_reconciliation.py`
  symétrique à `procurement_reconciliation.py` — n'existe pas encore.
  `services/reconciliation/procurement_reconciliation_service.py` est
  SPÉCIFIQUE à `create_auction`/PROCUREMENT (`PROCUREMENT_MCP_TOOL_NAME`
  en dur) ; une version PREORDER nécessiterait soit une généralisation du
  service (paramétrer le tool_name/l'adaptateur), soit une copie dédiée —
  décision délibérément DIFFÉRÉE (hors du périmètre "migration du
  workflow", qui portait sur `PreorderDraft`/confirmation/exécution, pas
  sur la réconciliation post-crash). **Limite explicite, pas cachée.**
- Timeout/exception pendant `confirm_draft` → `EXECUTION_UNKNOWN` (section D).

---

## I. GPS — intégration au modèle canonique

`enter_gps_stage`/`resolve_gps_stage` (`gps_delivery_gate.py`) réutilisées
**telles quelles**, aucune réinvention. `PendingInteraction(PROVIDE_LOCATION,
target=ConfirmationTarget)` est l'autorité conversationnelle exacte
(mandat §20) — la cible est **la même version** que celle du CONFIRM
initial, garantissant qu'une UPDATE survenue entre-temps invaliderait
correctement la résolution GPS en cours (`STALE_TARGET`, jamais un lieu
appliqué à la mauvaise version). Le point de livraison RÉSOLU est
persisté dans `draft.delivery_lat`/`delivery_lon` **au moment même** de la
transition vers `EXECUTING` (`_confirm_to_executing`, un seul bump de
version) — jamais un état intermédiaire "DRAFT avec lieu mais pas encore
EXECUTING" observable.

---

## J. Code supprimé (pas seulement le code ajouté)

- `_execute_confirm` (235 lignes, `preorder.py`) — remplacée.
- La fabrication d'état SYNTHÉTIQUE dans `update_preorder_phase` (double
  appel à `create_preorder` pour "drafter puis confirmer en un tour") —
  `create_preorder` sait désormais enchaîner nativement (section A),
  `update_preorder_phase` réduit de ~45 lignes à ~15.
  ```
  résultat mesurable : update_preorder_phase 45 lignes -> 15 lignes,
  create_preorder ne relit plus resolved_id qu'UNE fois en tête.
  ```
- La branche `resolved_id`-driven de PHASE 2 (confirmation) dans l'ancien
  `create_preorder` (~130 lignes gérant CONFIRM/GPS/exécution avec
  `if`/`elif` imbriqués sur `resolved_id`/`phase`/`gps_stage`) —
  remplacée par la délégation à `resolve_preorder_confirmation`.

---

## K. Tests

| Fichier | Tests | Portée |
|---|---|---|
| `tests/architecture/test_preorder_draft_persistence.py` | 6 | CAS, concurrence réelle (10 threads), staleness — faux moteur SQL fidèle |
| `tests/architecture/test_preorder_transactional_contract.py` | 13 | Invariants A-L du mandat (source unique, resolved_id non-contrôleur, active_cart non-divergent, UPDATE=version+1, target invalidé, CONFIRM stale/sans-cible refusés, double CONFIRM=1 exécution, concurrence réelle UPDATE/CONFIRM, création idempotente, ancien résumé disparu) |
| `tests/nodes/test_preorder_confirm_ux_and_gps.py` | 9 | RÉÉCRIT en entier pour la nouvelle architecture — UX oui/non, déviation LLM (`DRAFT_UNCHANGED`, nouveau kind ajouté ce jour), gate GPS (stocké/nouveau/texte libre/annulation), escrow vs non-escrow |
| `tests/nodes/test_agent_hard_resilience.py` | 2 (1 réécrit) | Panne réseau création (retour panier) ; panne réseau confirmation → **comportement changé** vers `EXECUTION_UNKNOWN` (voir D), verrouillé explicitement |

Régression complète (`pytest tests -q`) : **zéro nouvelle régression**,
mêmes 4 échecs pré-existants (`test_create_auction_catalog_gate.py`).

---

## L. Legacy restant — classification (mandat §M)

| Terme | Où (hors PREORDER) | Classification |
|---|---|---|
| `preorder_workflow["phase"]` | `preorder.py` (branches CANCEL/ADD_MORE hors-draft), `cart_service.py` (4 sites), `cognitive.py`, `goal_planner.py` | **PROJECTION/RESET LÉGITIME** — écrit UNIQUEMENT pour "retour à CART" avant/hors qu'un draft actif existe, jamais en conflit avec `draft.status` (vérifié section B) |
| `transaction_payload["resolved_id"]` | Uniquement `preorder.py`, traduit une fois | **PROJECTION** — n'est plus lu ailleurs comme contrôleur |
| `active_cart` | `flow.py`, `cart_service.py`, `cart.py` (gestion panier générique, HORS du cycle confirmation) | **CANONIQUE** pour son rôle réel (panier avant précommande) — pas une divergence avec `PreorderDraft.items`, qui ne prend le relais qu'APRÈS la création |
| `confirmation_summary`, `waiting_for_confirmation`, `expected_input` | 25 fichiers au total dans `market_coach/` — AUCUN dans le périmètre PREORDER (vérifié : absents de `preorder.py`/`preorder_confirmation.py`) | **HORS PÉRIMÈTRE** — champs legacy partagés avec producteur/négociation/autres tunnels, déjà identifiés comme projections de compatibilité par un refactor antérieur ([[market-coach-turn-boundary-state]]) ; un audit global de ces 25 fichiers dépasse le périmètre "migration PREORDER" et n'a pas été fait ici |
| `PreorderPhase` (classe, `contexts.py`) | Jamais importée ailleurs (vérifié par recherche globale) | **DELETE** candidat — code mort, mais suppression HORS de ce chantier (zéro appelant à casser, mais pas vérifié si un futur code y comptait) |

---

## M. Limites restantes (honnêtes, non maquillées)

1. **Pas de worker de réconciliation PREORDER** — `find_stale_executing`
   existe et est testé, aucun cron ne le consomme (section H).
2. **`initiate_escrow_payment` n'a pas de garde serveur `status != DRAFT`
   auditée** — seule la couche MCP (idempotency_key) protège ce chemin,
   contrairement à `confirm_preorder_draft`.
3. **`EXECUTION_UNKNOWN`/`AWAITING_PAYMENT` restent des culs-de-sac** —
   aucune réconciliation automatique ne les résout (même limite que
   PROCUREMENT, assumée).
4. **Le débit stock réel d'une commande escrow (IPN Paydunya) ne met PAS à
   jour `preorder_draft_store`** — `PreorderDraft.status` reste
   `AWAITING_PAYMENT` en base même après confirmation réelle du paiement ;
   câbler `workers/payments/paydunya_ipn_task.py` vers cette table est un
   chantier séparé, non fait.
5. **`ADD_MORE` (ajouter un produit) n'annule pas l'ancien `Order(DRAFT)`
   serveur** — comportement IDENTIQUE à avant cette migration (déjà
   orphelin), pas une régression introduite ici, mais pas non plus corrigé.
6. **Aucun test contre un vrai PostgreSQL** — même portée honnête que
   PROCUREMENT (faux moteur SQL fidèle, jamais une vraie base).
