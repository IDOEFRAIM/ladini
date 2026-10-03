# B13 — Audit Buyer recurring : visibilité et bout en bout (2026-10-03)

## Commandes Buyer réellement supportées

| Commande utilisateur | Intent | Goal / flow | Service | Résultat |
|---|---|---|---|---|
| « mes besoins récurrents » et 5 variantes | `GET_MY_NEEDS` (classifié par le LLM **uniquement**, aucune route déterministe) | `recurring_need_flow` → `_render_needs_list` | `list_my_recurring_needs` | liste numérotée (produit, quantité/fréquence, actif/pause, disponibilité « demain ») |
| « 1 » (après la liste) | sélection | `_show_need_detail` | `get_recurring_need_detail` | détail : producteurs, quantités, prix, total ; menu 1 Confirmer / 2 Pas cette fois / 3 Retour |
| « 1 » / « 2 » sur le détail | sélection | `_respond_to_match` | `accept_match_proposal` (occurrence + version affichées) | ACCEPT / REJECT exact |
| « oui », « non », « pas demain » après un digest | filets déterministes (sans LLM) | `UPDATE_RECURRING_NEED` + `CONFIRM_MATCH` / `REJECT_MATCH` | `accept_match_proposal` (occurrence + version du digest) | accepte / refuse les occurrences du dernier digest |
| « modifier » après un digest | filet déterministe | mini-flow digest | `update_recurring_need(OCCURRENCE_OVERRIDE)` | quantité d'UNE occurrence |
| « pas demain pour X » | filet déterministe | `_digest_skip_named_product` | `update_recurring_need(OCCURRENCE_SKIP)` | skip d'un besoin |
| créer / pause / reprise / annulation / quantité / fréquence | `CREATE_RECURRING_NEED` / `UPDATE_RECURRING_NEED` (LLM) | `recurring_need_flow` | `create_recurring_need(s)` / `update_recurring_need` | aucune entrée de menu pour pause/reprise/annulation |

## Cron vs requête Buyer

- Cron : réapprovisionnement des occurrences (+ expiration B12), matching (récent / à venir), digest.
- À la demande du Buyer (sans cron) : liste, détail, ACCEPT/REJECT depuis le détail (la proposition est lue en base,
  `notified_at` n'est pas requis quand une version est fournie).

## Découvertes

| Classe | Découverte |
|---|---|
| **BLOCKER E2E (corrigé, minimal)** | Liste → « 1 » → détail → confirmer était INATTEIGNABLE sur le vrai graphe : la liste/détail renvoyaient `COMPLETED` (le goal `GET_MY_NEEDS` était effacé par `nodes/cleanup.py`) et le `pending_interaction` est consommé avant le flow (`_resolve_menu_reply` exigeait un pending vivant). Les tests existants injectaient l'état à la main. Correctif : `WAITING_INPUT` + repli sur le menu de `working_memory` (TTL propre). |
| BUG EXISTANT | Interaction B11 × VS5 : `confirm_delivery_and_payment` accepte une commande `RECURRING_SUPPLY` → COMPLETED/PAID/DELIVERED sans réception acheteur et sans `quantity_delivered` (figé par un test PG). Non corrigé. |
| BUG EXISTANT (mineur) | La branche « annulé » du rendu de liste est inatteignable : le service exclut `CANCELLED`. |
| FEATURE GAP | Aucune route déterministe pour « mes besoins récurrents » (LLM seul). |
| FEATURE GAP | Statuts d'occurrence `FULFILLED` / `PARTIALLY_FULFILLED` / `UNFULFILLED` permis par la contrainte, jamais écrits : l'occurrence reste `ACCEPTED` après orders COMPLETED. |
| FEATURE GAP | Aucune expiration automatique d'une commande recurring en `PENDING_PRODUCER_CONFIRMATION` (stock déjà débité). |
| UX GAP | « disponibles demain » codé en dur (la date réelle n'est pas affichée) ; quantités « 20.0 » ; la date/version/statut d'occurrence ne sont pas affichés ; « mes commandes » (Buyer et Producteur) ne distingue pas une commande recurring. |
| OBSERVABILITY GAP | Pas de dernière notification affichée ; pas de lien occurrence ↔ commande visible côté Buyer. |

## Non exécuté localement

Aucun PostgreSQL local : `tests/schema/test_recurring_e2e_pg.py` (parcours réel matching → digest → vue → accept → producteur →
livraison/réception → occurrence suivante, no-response, reject, partiel, multi-producteur, deux besoins, écart B11) et
`tests/schema/test_recurring_lifecycle_pg.py` ne tournent qu'en CI.
