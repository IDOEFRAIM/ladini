# Recurring — délai avant première livraison, réglage admin, console Operations

## 1. Définition

```
minimum_start_date = aujourd'hui (métier) + minimum_recurring_start_lead_days
```

Nombre minimal de jours entre la **création** d'un besoin récurrent et sa **première livraison**.
Ce n'est ni la fréquence (`recurrence.py`), ni la fenêtre de confirmation producteur
(`RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS`), ni la fenêtre de récupération : trois notions distinctes.

- **Défaut : 4 jours** (`start_policy.DEFAULT_START_LEAD_DAYS`) tant qu'aucun admin n'a enregistré de valeur.
- **Bornes : 0 à 30.** Aucune convention de bornes n'existait dans le dépôt pour ce type de réglage ; 30 est un plafond de
  sécurité contre une faute de frappe, pas une règle métier. Entier obligatoire (`True`, `2.5`, `"4"` refusés).

## 2. Source de vérité unique

| Rôle | Où |
|---|---|
| Règle pure (date, validation, défaut) | `domain/recurring_supply/start_policy.py` |
| Lecture/écriture du réglage | `services/platform_settings.py` (`get_recurring_settings`, `update_recurring_settings`) |
| Stockage | table `governance.platform_settings` (clé `recurring_supply.minimum_start_lead_days`, JSONB, versionnée) |
| Application à la création | `RecurringSupplyMixin._start_decision` (service) — jamais le flow, jamais le cron |

Aucune constante `4` dans le flow conversationnel ni le service (test d'architecture :
`tests/unit/test_recurring_start_policy.py::test_lead_time_is_not_hardcoded_in_the_flow_or_service`).
Pas de cache : une lecture indexée par création → une modification admin est visible tout de suite, sur toutes les instances.
Concurrence : verrou de ligne + `expected_version` optionnel (409 si périmé) ; sans lui, dernier écrit gagne et l'audit
conserve ancienne/nouvelle valeur.

## 3. Règles de date

| Cas | Résultat |
|---|---|
| Aucune date / « dès que possible » | `minimum_start_date` (jamais « demain » automatique) |
| Date explicite ≥ minimum | respectée telle quelle |
| Date explicite < minimum (demain, aujourd'hui, passée) | repoussée au minimum, **dite à l'utilisateur** (`start_adjusted`) |
| `WEEKLY` / `MONTHLY` | l'ancre est `starts_at` (jour de semaine / quantième) : 1ʳᵉ échéance = la date retenue |
| `DAILY` / `WEEKLY_DAYS` / jours exclus | 1ʳᵉ échéance = 1ʳᵉ date valide du calendrier ≥ minimum (`first_due_date`) |

Conséquence MONTHLY : un besoin « le 6 de chaque mois » créé le 5 octobre (délai 4) démarre le **9 octobre** ; le moteur
ancre ensuite sur le 9. L'utilisateur peut demander explicitement « à partir du 6 novembre » (ancre = 6).
Le cron de matérialisation lit `starts_at` déjà validé : le délai n'est **pas** dupliqué dans le cron.

## 3 bis. Conversation

- L'interpréteur extrait `starts_at` (`YYYY-MM-DD`, ou `null` pour « dès que possible ») ; le **domaine** décide.
- Avant confirmation, le flow appelle `get_recurring_start_policy` et **écrit la vraie date dans le brouillon** ; le
  récapitulatif (`nodes/rendering/confirm.py`, désormais `RecurringNeedDraft.render_summary()` — corrige aussi H1 : fréquence
  et tous les produits) affiche « Première livraison prévue : 9 octobre ».
- Date trop proche : une phrase explique (« …au plus tôt le 9 octobre. Je la programme à cette date. »), uniquement dans ce cas.
- Réglage modifié entre le récapitulatif et le « oui » : **la valeur courante s'applique à la création** ; le message final
  affiche la date réellement retenue.
- Une date « 20 octobre » n'est plus lue comme une quantité orpheline (`find_bare_number_candidates`).

## 4. Besoins existants

Aucun recalcul : `RecurringNeed.starts_at` est figé à l'écriture. Passer 4 → 7 n'affecte que les besoins créés ensuite.
Le délai appliqué à la création **n'est pas historisé** : la fiche admin affiche le réglage courant et dit explicitement que
la valeur d'origine n'est pas conservée (`applied_at_creation: null`) ; `starts_at` fait foi.

## 5. Administration (API interne, `X-Internal-Token`)

Même posture que `/internal/commercial` : l'adaptateur Next.js impose la session ADMIN ; **en plus**, l'écriture re-vérifie en
base que `actor_id` est `ADMIN` (403 sinon). Validation 422, conflit 409, audit dans `intelligence.audit_logs`
(`PLATFORM_SETTING_CHANGED`, ancienne/nouvelle valeur, acteur, IP, horodatage).

| Route | Rôle |
|---|---|
| `GET /internal/recurring-admin/settings` | valeur courante + source (`DEFAULT`/`DATABASE`) + version + bornes |
| `PUT /internal/recurring-admin/settings` | `{actor_id, minimum_start_lead_days, expected_version?}` |
| `GET /internal/recurring-admin/needs` | liste détaillée, filtres : `status`, `product`, `region`, `frequency`, `buyer`, `q` (id/acheteur), `starts_from/to`, `next_from/to`, `limit/offset` |
| `GET /internal/recurring-admin/needs/{id}` | fiche : `overview`, `schedule`, `occurrences` (+ allocations), `orders`, `mutations`, `operational_state` |

Aucune route n'écrit sur un besoin : toute mutation passe par `update_recurring_need` (service métier).

## 6. Operations ≠ Analytics

```
Recurring
├── Needs      (OPERATIONS) → /internal/recurring-admin/needs[/{id}]   inspecter UN objet réel
├── Settings   (OPERATIONS) → /internal/recurring-admin/settings
└── Analytics  (AGRÉGATS)   → /internal/analytics/buyers/recurring     inchangé : performance globale
```

Operations répond « que se passe-t-il pour ce besoin ? » ; Analytics « comment le système performe-t-il ? ». Aucun code partagé.

## 7. Limites assumées

- **Interface** : ce dépôt expose les API ; les pages Next.js (liste, fiche, réglage) vivent dans `ladinifront` (hors de ce dépôt).
  **La table `governance.platform_settings` doit aussi y être déclarée (Drizzle = source de vérité) et mergée en premier.**
- **Historique des mutations** (pause, reprise, quantité, fréquence) : aucun journal durable lisible → `mutations.available=false`,
  jamais un faux historique reconstitué depuis l'état courant.
- **Surcharge admin** (démarrer avant le minimum) : volontairement NON implémentée. Si ajoutée : explicite, auditée, rare.
- Horloge : `_today()` = `datetime.now().date()` (dette timezone existante, inchangée).
- Portée : réglage GLOBAL uniquement (pas par région/produit/fréquence).
