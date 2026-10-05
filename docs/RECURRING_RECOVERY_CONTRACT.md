# Contrat de récupération d'une occurrence récurrente (B28)

Un besoin récurrent est durable. Une occurrence est une obligation de livraison. Une allocation ou une commande n'est qu'**une tentative** de l'honorer.
Si un producteur échoue, Ladini n'oublie pas le besoin de l'acheteur : si la livraison est encore possible, elle est rouverte et re-matchée **exactement** ; sinon elle est clôturée honnêtement et le besoin continue.

Autorité : `RecurringSupplyMixin` (`services/database/recurring_supply.py`). Ni le flux WhatsApp, ni le LLM, ni `context_arbitration` ne décident — ils résolvent la cible, appellent la primitive, rendent le résultat.

## 1. Need / Occurrence / Allocation / Order

| Niveau | Rôle | Un échec ici tue… |
|---|---|---|
| `RecurringNeed` | demande durable | seulement sur annulation/pause explicite de l'acheteur |
| `RecurringNeedOccurrence` | UNE livraison (date, quantité, `version`) | seulement si la fenêtre est fermée (§4) |
| `NeedAllocation` | une proposition de sourcing (occurrence, producteur, produit) | elle-même (`EXPIRED`/`REJECTED`), reste historique |
| `Order` | une tentative d'exécution acceptée | elle-même (`CANCELLED`), reste historique |

Allocation échouée ≠ occurrence échouée ; tentative de commande échouée ≠ occurrence échouée.

## 2. Taxonomie des échecs

| Échec | Allocation | Commande | Occurrence | Besoin |
|---|---|---|---|---|
| Producteur ne confirme pas à temps (`producer_confirmation_expired`) | historique | `CANCELLED` | rouverte si fenêtre ouverte | actif |
| Rejet explicite avant engagement (`declined_by_producer`) | historique | `CANCELLED` | rouverte si fenêtre ouverte | actif |
| Annulation producteur après confirmation (`cancelled_by_producer`) | historique | `CANCELLED` | rouverte si fenêtre ouverte et aucune autre commande vivante | actif |
| Allocation expirée / stock indisponible | `EXPIRED` | — | `OPEN` (re-match) | actif |
| Proposition refusée par l'acheteur | `REJECTED` | — | `REJECTED`, récupérable **sur demande explicite** seulement | actif |
| Annulation par l'acheteur | — | `CANCELLED` | `UNFULFILLED`, non récupérable | actif |
| Livraison impossible / réception litigieuse | — | — | `UNFULFILLED`, non récupérable | actif |

Raisons structurées (jamais du texte libre) : `PRODUCER_TIMEOUT`, `PRODUCER_REJECTED`, `PRODUCER_CANCELLED`, `PROPOSAL_REJECTED` (dérivées de `OrderStatusHistory.note`). Le niveau d'annulation de la commande reste `CANCELLED` : la raison fine est portée par la note d'historique et par le payload de notification.

## 3. Statuts d'occurrence (aucun nouveau statut, aucune migration)

`OPEN`, `MATCHED`, `ACCEPTED`, `PARTIALLY_ACCEPTED`, `REJECTED`, `EXPIRED`, `SKIPPED`, `CANCELLED`, `FULFILLED`, `PARTIALLY_FULFILLED`, `UNFULFILLED`.
« Récupérable » est un état **logique dérivé** (`can_recover_occurrence`), pas une colonne. La réouverture utilise la transition existante vers `OPEN`.

## 4. Récupérabilité et fenêtre

Fonction pure `can_recover_occurrence(occurrence, need_status, today, live_orders, sourcing_failed, user_initiated)` → `RecoveryDecision(outcome, recoverable, in_place)`.

- Fenêtre : **le jour de livraison inclus** (`occurrence_date >= aujourd'hui`). Aucune durée codée en dur (pas de 24 h/48 h).
- Besoin non `ACTIVE` → `NEED_NOT_ACTIVE`.
- Date dépassée → `RECOVERY_WINDOW_CLOSED` (jamais « la suivante »).
- `OPEN`/`MATCHED` → récupérable sur place ; `ACCEPTED`/`PARTIALLY_ACCEPTED` sans commande vivante → récupérable ; avec commande vivante → `OCCURRENCE_COMMITTED` (engagement irréversible).
- `UNFULFILLED` → récupérable seulement si toutes les tentatives ont échoué côté producteur/système ; `REJECTED` → seulement sur demande utilisateur.
- `EXPIRED`, `SKIPPED`, `CANCELLED`, `FULFILLED`, `PARTIALLY_FULFILLED`, statut inconnu → non récupérable (fail-closed).

**Constat B14 important.** Par défaut la date limite de confirmation producteur est la date de livraison : un timeout se produit donc *après* la fenêtre, et l'issue honnête est `RECOVERY_WINDOW_CLOSED`. Le paramètre `RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS` (défaut `0`, décision produit) avance l'échéance (`expected_fulfillment_date < aujourd'hui + lead`) pour qu'un timeout laisse du temps à la récupération. Un rejet/annulation explicite avant le jour de livraison est récupérable sans ce réglage.

## 5. Réouverture d'une occurrence

À l'échec d'une tentative, `_recompute_occurrence_fulfillment` (la primitive unique de fulfillment) recalcule depuis les lignes de commandes. Si plus aucune commande vivante, échec côté producteur/système et fenêtre ouverte : statut `OPEN`, compteurs remis à zéro, `version + 1`, événement `RECOVERY_APPLIED`/`REOPENED`. Sinon clôture (`UNFULFILLED`) avec l'outcome métier.
L'ancienne allocation, la proposition, l'ancienne commande, la raison et les horodatages restent en base (historique). La commande échouée n'est jamais réutilisée.

## 6. Re-match

`recover_occurrence_sourcing(phone, recurring_need_id, occurrence_id, expected_occurrence_version, source)` (et `refresh_recurring_need_matching(..., occurrence_id, expected_occurrence_version)`) appelle `NeedMatchingService.rematch_occurrence` — le moteur existant, aucun nouveau moteur. Le re-match :
- cible l'occurrence exacte, jamais la prochaine ;
- utilise `occurrence.requested_quantity` (override préservé ; une mise à jour permanente ultérieure ne réécrit pas une livraison déjà snapshotée) ;
- exclut les producteurs dont l'allocation `CONVERTED`/`REJECTED` existe **pour cette occurrence uniquement** (pas pour les futures) ;
- ne crée jamais de commande et n'accepte jamais un producteur : l'acceptation reste celle de l'acheteur (B26).

Issues : `RECOVERY_APPLIED`, `ALREADY_RECOVERED`, `RECOVERY_WINDOW_CLOSED`, `NOT_RECOVERABLE`, `NEED_NOT_ACTIVE`, `OCCURRENCE_COMMITTED`, `VERSION_CONFLICT`, `TARGET_NOT_FOUND`, `MATCH_ERROR`.

## 7. Récupération partielle

Les engagements encore valides sont conservés ; seules les allocations vivantes de la livraison en échec sont invalidées. Invariant : somme des allocations vivantes ≤ quantité demandée restante après récupération (testé).

## 8. Proposition périmée

L'ancienne proposition perd sa validité : `occurrence.version` est incrémentée par la réouverture et par le re-match qui change l'état. Un « je prends » issu de l'ancien écran reçoit `PROPOSAL_CHANGED` / `VERSION_CONFLICT`, jamais une acceptation.

## 9. Concurrence

Ordre de verrous B25 : besoin → occurrence → allocations (`FOR UPDATE`, `populate_existing`). Deux récupérations simultanées se sérialisent : la seconde voit l'état déjà récupéré (`ALREADY_RECOVERED`). Récupération ∥ cron de matching : clé d'unicité `(occurrence, producteur, produit)` → aucun doublon d'allocation, d'occurrence ni de commande.

## 10. Confirmation tardive du producteur

Une commande échouée est `CANCELLED` : une confirmation tardive est refusée (statut non confirmable) et ne ressuscite pas la tentative.

## 11. Pause / annulation / saut

Besoin en pause ou annulé → `NEED_NOT_ACTIVE`, aucun nouveau matching. Occurrence `SKIPPED` → `NOT_RECOVERABLE`. Les occurrences futures du besoin continuent normalement.

## 12. Versioning

Toute commande de récupération porte `occurrence_id` et `expected_occurrence_version` (B26) ; la version n'est jamais relue silencieusement. Version absente → refus `version_required`. Version périmée sur une occurrence déjà `OPEN`/`MATCHED` → `ALREADY_RECOVERED` (un seul effet) ; sur un autre état → `VERSION_CONFLICT`.
La notification d'échec porte `recovery_candidate`, `recovery_outcome`, `failure_reason`, `occurrence_id`, `occurrence_version`, date — alimentée par les vrais services d'échec (timeout, rejet, annulation), pas par une fixture. B27 (`get_last_interactive_outbound`) en tire le contexte ; jusqu'à 10 livraisons en échec sont agrégées, plusieurs → clarification.

## 13. Convergence cron

Le cron `match_upcoming_occurrences` re-matche déjà les occurrences `OPEN` : la réouverture donne donc un re-match automatique, avec la même exclusion de producteur, sans accepter quoi que ce soit. Le self-service appelle la même primitive de re-match ; il n'existe pas de `cron_recovery()` / `user_recovery()` séparés.

## Observabilité

Logs structurés sans PII : `RECURRING_RECOVERY_{REQUESTED,APPLIED,SKIPPED,FAILED,REMATCHED}` avec statut d'occurrence, raison, récupérable oui/non, outcome, source (`USER`/automatique).

## Limites connues

- Fuseau : « aujourd'hui » = date serveur (limite B25/B27 inchangée).
- Pas d'audit durable dédié : l'historique est porté par allocations, commandes et `OrderStatusHistory`.
- Pas d'agrégation de notifications (documenté seulement).
- `RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS=0` par défaut : un simple timeout producteur reste donc non récupérable tant que ce réglage n'est pas décidé.
