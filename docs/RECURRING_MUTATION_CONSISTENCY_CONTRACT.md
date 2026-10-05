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
- La version est OBLIGATOIRE depuis B26 (voir « Version Contract » ci-dessous) : plus d'application « sur l'état courant » par omission.
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
- Demande « à froid » (aucun écran présenté) : la version lue dans le tour est transmise ; elle protège la fenêtre lecture → écriture,
  pas une intention périmée (il n'y en a pas). Réponses au digest antérieur à B12 (sans snapshot) : accept/reject sans version (repli transitoire).
- Pas de RECURRING_NEED_UPDATED durable : `analytics.business_events.event_name` a un CHECK fermé côté Drizzle (migration frontend requise).
- La reprise automatique a lieu au prochain passage du cron ou à l'ouverture du besoin, pas à la seconde près.

## 11. Version Contract (B26)
1. **Objet.** Empêcher qu'une commande bâtie sur un état présenté plus tôt (écran, liste, confirmation, digest) s'exécute sur un
   état métier plus récent : *intention périmée*. Quatre invariants distincts, tous nécessaires, aucun ne remplace un autre :
   le **verrou de ligne** (`FOR UPDATE`) sérialise physiquement ; **`expected_version`** valide logiquement l'état observé ;
   l'**idempotence** empêche qu'une même commande s'applique deux fois ; le **TTL du menu** borne la fraîcheur conversationnelle.
2. **Version du besoin** = `updated_at` en µs (entier, `recurring_need_version_of`), sans fuseau (UTC), `< 2^53` (JSON sûr). Elle change
   exactement une fois par mutation effective (`_bump_need_version`) : quantité, fréquence/jours, statut, `paused_until`, y compris la
   reprise automatique du cron. Elle ne change PAS : sur un rejeu idempotent (`ALREADY_APPLIED`), un conflit, ni pour une mutation d'occurrence
   (skip, override), un matching, une allocation, une commande ou une matérialisation. Les écrivains de `recurring_needs` : tous dans
   `recurring_supply.py` et tous passent par `_bump_need_version` (audit B26 : aucun écrivain externe).
3. **Version de l'occurrence** = `recurring_need_occurrences.version` (`+1` à chaque réécriture, matching ou réponse). Jamais confondue
   avec celle du besoin : `OCCURRENCE_SKIP`/`OCCURRENCE_OVERRIDE` exigent `expected_occurrence_version` (et ne demandent pas celle du besoin),
   `accept_match_proposal` garde son `expected_version` d'occurrence (B12), inchangé.
4. **Source de la version.** Toujours l'état PRÉSENTÉ, jamais une relecture avant d'écrire : `working_memory.recurring_need_menu.target`
   (`need_version`, `occurrence_*`) pour un détail, `versions` pour la liste, `commands[k].expected_version|expected_occurrence_version` pour un
   ordre à confirmer, `selected.occurrence_version` (digest : `digest_occurrence_version`) pour le digest. Elle ne dépend pas du TTL du menu.
   Frontière : la navigation (liste → détail) rafraîchit ; la mutation compare à l'état affiché. Sans écran présenté (demande à froid), la
   version lue dans le tour est transmise (`same_turn_read`).
5. **Obligatoire.** Service : `expected_version` (besoin) ou `expected_occurrence_version` (occurrence) manquant →
   `BusinessRuleException(reason="version_required")` avant toute lecture. Passerelle : `MCPCallError` sans appel émis. Aucun mode « interne »
   n'existe : le cron et la reprise automatique n'appellent pas `update_recurring_need` (primitives privées sous verrou).
6. **Ordre des contrôles.** propriété (le besoin est résolu sous `buyer_id` : « introuvable » identique pour une version juste ou fausse) →
   version du besoin → (occurrence : existence/statut → version de l'occurrence) → état (`need_not_active`) → action. Donc une intention
   périmée sur un besoin annulé ou suspendu est un `VERSION_CONFLICT`, jamais une réactivation.
7. **Snapshot de confirmation.** L'ordre confirmé est immuable : `{action, recurring_need_id, expected_version | expected_occurrence_version,
   valeurs}`. La seconde confirmation de CANCEL garde la version de la première. Le tour suivant n'est jamais reconstruit depuis la base.
8. **Conflit.** `{"status": "conflict", "outcome": "VERSION_CONFLICT", "scope": "NEED|OCCURRENCE", "current": {…}}` : aucune écriture, aucun
   effet de bord (pas d'occurrence, d'allocation, de commande, d'événement, d'outbox, d'audit de mutation, de version). Le flow dit ce qui a
   changé, RÉAFFICHE l'écran à l'état courant (la version présentée devient l'actuelle) et l'acheteur décide de nouveau ; jamais de rejeu automatique.
   La version n'est jamais montrée à l'utilisateur.
9. **Rejeu.** Une commande déjà appliquée rejouée avec sa version d'origine est périmée (`VERSION_CONFLICT`) ; avec la version courante, elle est
   `ALREADY_APPLIED` (sans écriture ni bump). Même état désiré après un changement concurrent (ex. reprise manuelle après reprise automatique) :
   conflit propre, jamais une seconde mutation.
10. **Digest.** Le digest ne propose que des actions d'occurrence (accepter, refuser, ignorer, modifier la livraison) : aucune mutation du
    besoin. Elles portent la version d'occurrence du digest reçu (`digest_occurrence_version`) ; sans snapshot de digest, celle lue dans le tour.
11. **Observabilité.** `RECURRING_VERSION_CHECK` (action, scope, `expected_version_present`, `version_match`, outcome — jamais le jeton ni de
    donnée personnelle) côté service ; `RECURRING_VERSION_CONFLICT` (chemin appelant, action, source de la version) côté flow. Aucun compteur
    Prometheus dédié (les journaux suffisent à compter).
12. **Pourquoi `updated_at` et pas un BIGINT.** Écrit par une seule voie, sous verrou de ligne, `clock_timestamp()` à la µs, monotone et sans collision
    mesurée (40 mutations en rafale). Risques acceptés : surcharge sémantique (date de dernière modification = jeton), dépendance à l'horloge du
    serveur de base (un retour d'horloge ne casse pas le CAS : égalité stricte, pas d'ordre), précision liée au type `timestamp` Drizzle. Une
    colonne `version BIGINT` n'apporterait rien d'immédiat et exigerait une migration Drizzle : non retenue.
