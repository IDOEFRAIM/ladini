# Contrat de cohérence des mutations récurrentes (B25)

Normatif. Autorité : `services/database/recurring_supply.py::update_recurring_need` et la matérialisation. Aucun flux
conversationnel ne décide quelle occurrence est réécrivable, quelle allocation survit ou quelle commande change.
Preuves : `backend/tests/schema/test_recurring_mutation_consistency_pg.py` (PostgreSQL réel, concurrence réelle).
Complète `RECURRING_INTENT_AND_CONTEXT_CONTRACT.md` (interprétation) ; ici : état et transactions.

## 1. Besoin vs occurrence
- `RecurringNeed` = contrat durable (quantité, unité, planning, statut, pause).
- `RecurringNeedOccurrence` = instantané d'UNE livraison. `requested_quantity` et `unit` sont copiés du besoin à la
  matérialisation (`_materialize_occurrences`) ; `quantity_matched` / allocations / commande en dérivent.
- `NeedAllocation` `PROPOSED` = réservation logique (aucun stock débité) ; `CONVERTED` = commande créée. `Order` = trace
  d'exécution, jamais réécrite par une mutation du besoin.

## 2. Occurrences non engagées vs engagées
| Statut d'occurrence | Catégorie | Quantité permanente | Fréquence permanente | Pause | Annulation |
|---|---|---|---|---|---|
| OPEN | non engagée | suit le contrat si non exceptionnelle | annulée si hors planning (sauf exception) | SKIPPED (avant reprise) | CANCELLED |
| MATCHED | non engagée | idem, proposition expirée, repart OPEN | idem, allocations expirées | SKIPPED, allocations expirées | CANCELLED, allocations expirées |
| ACCEPTED, PARTIALLY_ACCEPTED | engagée | jamais modifiée | jamais modifiée | préservée | préservée |
| FULFILLED, PARTIALLY_FULFILLED, UNFULFILLED | terminale | jamais | jamais | jamais | jamais |
| REJECTED, EXPIRED, SKIPPED, CANCELLED | terminale | jamais | CANCELLED peut être ré-ouvert (§4) | jamais | jamais |
| Dates passées | — | jamais | jamais | jamais | jamais |

Une exception explicite (`requested_quantity` ≠ quantité du contrat) n'est jamais écrasée ni supprimée par une mutation
permanente. Invariant : jamais d'allocation vivante au-delà de `requested_quantity`, jamais `matched > requested`, jamais de
proposition bâtie sur une quantité périmée. Toute réécriture passe par `_reset_uncommitted_occurrences` (statut/quantité,
`quantity_matched = 0`, `version + 1`, allocations `PROPOSED` → `EXPIRED`). Le rematch est fait ensuite par le moteur existant
(cron ou « rechercher maintenant »).

## 3. Versionnement et concurrence
- Pas de colonne ajoutée : le schéma est défini côté Drizzle. Le jeton de version d'un besoin est `updated_at` en
  microsecondes (`recurring_need_version_of`), réécrit à chaque mutation effective avec `clock_timestamp()`.
  Exposé : `need_version` dans `list_my_recurring_needs` et `get_recurring_need_detail`.
- `update_recurring_need(..., expected_version=N)` : le besoin est verrouillé `FOR UPDATE` ; si `N` ≠ version courante,
  résultat `{"status": "conflict", "outcome": "VERSION_CONFLICT", "current": {…}}` sans aucune écriture ni audit. Le flow
  répond par un message métier (état courant + « redites votre demande »), jamais une erreur technique, jamais un rejeu aveugle.
- Politique : conflit STRICT sur toute mutation du même besoin (quantité vs fréquence incluses) ; pas de fusion par champ.
- Sans `expected_version` (appelants hérités, cron, digest) : la mutation s'applique sur l'état verrouillé le plus récent ;
  deux mutations ne s'entrelacent jamais. Les commandes confirmées (menu fermé) embarquent la version vue à la proposition.
- Ordre des verrous : besoin → occurrence → allocations. Le matching verrouille l'occurrence puis ne verrouille pas le besoin ;
  l'acceptation verrouille occurrence puis allocations : pas de cycle.

## 4. Planning et matérialisation
- La matérialisation (cron `replenish_occurrence_windows` ET self-service) reverrouille le besoin et ne crée rien s'il n'est
  plus `ACTIVE` ; elle snapshotte l'état commité (jamais un mélange ancienne quantité / nouvelle fréquence).
- Changement de fréquence : dates futures non engagées, non exceptionnelles, plus dues → `CANCELLED` ; nouvelle fenêtre
  matérialisée dans la même transaction. Si le planning redemande une date `CANCELLED` (seule source sous un besoin actif),
  elle est ré-ouverte avec le snapshot courant. Une exception explicite hors planning est conservée.

## 5. Pause, reprise, annulation
- `PAUSE` : `paused_until` = premier jour de reprise (exclu). Dates non engagées strictement avant → `SKIPPED` (sans date :
  toutes). `ACCEPTED` préservé. `paused_until` ≤ aujourd'hui → refus `invalid_pause_date`. Rejeu identique → `ALREADY_APPLIED`.
- Reprise automatique : `replenish_occurrence_windows` (même cron) et `ensure_next_recurring_occurrence` (self-service)
  repassent `ACTIVE` un besoin dont `paused_until` est atteint. Sans date : reprise manuelle uniquement.
- `RESUME` : `ACTIVE`, planning régénéré à partir d'aujourd'hui. Aucun rattrapage de la période de pause (pas de tempête
  d'occurrences en retard) ; les dates sautées restent sautées.
- `CANCEL` : fin durable. Non engagées (OPEN, MATCHED) → `CANCELLED`, propositions expirées ; `ACCEPTED` et commandes
  conservés. Un besoin annulé refuse toute mutation (`need_not_active`) et ne reprend jamais.
- Date de référence : `_today()` (date du serveur) ; dates d'occurrence sans fuseau, minuit.

## 6. Occurrence seule
- `OCCURRENCE_SKIP` : une occurrence non engagée → `SKIPPED` ; besoin et planning inchangés ; rejeu `ALREADY_APPLIED` (un
  seul événement `RECURRING_OCCURRENCE_SKIPPED`).
- `OCCURRENCE_OVERRIDE` : quantité d'UNE occurrence non engagée ; sa proposition expire ; même quantité → `ALREADY_APPLIED`.

## 7. Idempotence
Rejeu d'une mutation sans effet : `outcome = ALREADY_APPLIED`, aucune écriture, aucune version, aucun audit, aucun événement.
Clés d'événement existantes réutilisées (`RECURRING_OCCURRENCE_SKIPPED:<occurrence>`). Pas de nouvelle clé de commande.

## 8. Audit
Chaque mutation effective journalise `recurring_need.mutation` (besoin, action, ancienne/nouvelle valeur, version, acteur,
horodatage UTC ; ni téléphone ni nom) ; les occurrences `occurrence.mutation`. Limite : `analytics.business_events` a un CHECK
fermé sur `event_name` (schéma Drizzle) ; `RECURRING_NEED_UPDATED` / `RECURRING_OCCURRENCE_OVERRIDDEN` exigent une migration
côté frontend. Pas de table d'historique ni d'UX d'historique.

## 9. Politique de confirmation (inchangée, audit B25)
| Action | Confirmation | Justification |
|---|---|---|
| Quantité / fréquence | directe | réversible (redire), borné au futur non engagé ; protégé par version + audit |
| Pause, reprise, skip, override | fermée | effet immédiat sur des livraisons attendues |
| Annulation | double | terminale, non réversible |

## 10. Limites connues
- Une exception posée à la même valeur que le contrat est indiscernable d'une occurrence non exceptionnelle.
- Pas de rematch inline : après une réécriture, la recherche est relancée par le cron ou le self-service.
- `expected_version` est optionnel : un appelant qui ne le transmet pas n'est protégé que contre l'entrelacement.
- La reprise automatique a lieu au prochain passage du cron ou à l'ouverture du besoin, pas à la seconde près.
