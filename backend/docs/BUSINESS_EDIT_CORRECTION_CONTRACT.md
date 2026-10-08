# BUSINESS EDIT & CORRECTION — contrat

> La conversation INTERPRÈTE la correction. Le domaine POSSÈDE la mutation.

Le modèle ne produit jamais un identifiant, jamais un prix recalculé, jamais un total. Il émet une intention structurée
(`cart_edit`, `is_correction`, `remove`) ; le domaine valide, revalide (stock, minimum de commande), applique avec un contrôle de
version et répond par un statut explicite.

## 1. Panier acheteur (`domain/cart_edit.py`)

| Élément | Règle |
|---|---|
| Identité d'une ligne | `line_id` stable (pas l'index affiché, pas le nom) ; lignes anciennes migrées à la lecture (`with_line_ids`) |
| Champs éditables | `QUANTITY`, `PACKAGE_COUNT`, `REMOVE` |
| Quantité vs nombre de paquets | jamais confondus : sur une ligne à paliers, un nombre nu = nombre de paquets ; une unité de mesure égale à l'unité du contenu est refusée (« Combien de sachet… ? ») |
| Valeurs refusées | 0, négatif, NaN/inf, unité incompatible (`REJECTED`, panier inchangé) |
| Totaux | recalculés par le domaine ; jamais repris du modèle |
| Concurrence | `expected_version` (version vue par l'utilisateur) ≠ version courante → `CONFLICT`, aucune mutation |
| Rejeu | même valeur → `UNCHANGED` (idempotent, pas de bump de version) |
| Ligne ambiguë | plusieurs candidates → `AMBIGUOUS` + question ; jamais la première ligne |
| Ligne introuvable | `NOT_FOUND` |
| Stock / minimum | revérifiés avant `commit_edit` (I/O hors de la fonction pure `plan_edit`) |

Statuts : `APPLIED`, `UNCHANGED`, `CONFLICT`, `REJECTED`, `AMBIGUOUS`, `NOT_FOUND`. Échec = fermé (rien n'est muté).

### Pré-commande (`PreorderDraft`, Postgres CAS)

* Brouillon `DRAFT` : une correction du panier recompose le brouillon (nouvelle version, même `draft_id`), l'ancienne confirmation
  est périmée (`STALE_TARGET`) → nouveau récapitulatif, nouvelle confirmation. « oui mais mets 10 » = édition d'abord, jamais
  confirmation de l'ancienne valeur.
* Commande engagée (`EXECUTING`, `COMPLETED`, …) : jamais éditée en silence (« déjà engagée »).
* Routage : `BUYER_EDIT_CART` pendant une pré-commande active est remappé vers le goal de pré-commande avec l'évènement `UPDATE`
  (le planificateur de goals n'efface donc pas le brouillon).

## 2. Brouillon vendeur (`domain/sales_publish_draft.py`)

* Brouillon versionné immuable, modifiable seulement en `DRAFT`.
* Correction de quantité / prix : seul ce champ change, version +1.
* Correction du PRODUIT (`is_correction`) : les champs physiques compatibles (`quantity`, `unit`) sont conservés ; les champs liés au
  produit (prix, paliers, offre commerciale certifiée) sont purgés puis redemandés (« il me manque le prix »). Jamais la question
  « annuler et commencer la vente ? » pour une correction.
* Une valeur redite dans la correction n'est jamais écrasée.
* Le validateur laisse passer une correction vers la porte de confirmation même si elle rend la charge incomplète, pour que le brouillon
  actif (propriétaire de l'état) soit mis à jour avant que le champ manquant soit demandé.
* L'enrichissement textuel ne fabrique pas de 2ᵉ champ : « ah non c 350 le prix » corrige le prix, pas la quantité.

## 3. Contraintes de recherche acheteur (`domain/search_constraints.py`)

* Poser / remplacer / RETIRER (`remove: ["max_price"|"packaging"]`) ; « le prix n'importe plus » supprime réellement le plafond.
* La liste est recalculée à partir du pool des résultats d'origine (`search_pool`), jamais une relaxation silencieuse ni un plafond factice.
* Sans pool disponible, retirer une contrainte est refusé avec une explication.

## 4. Matrice de mutabilité

| Entité | État | Éditable ? | Mécanisme |
|---|---|---|---|
| Ligne de panier | présente | oui | `edit_cart_line`, CAS version |
| Panier | vu périmé | non (CONFLICT) | version |
| Pré-commande | `DRAFT` | oui (recomposition, version +1) | `bootstrap_preorder_draft` |
| Pré-commande | engagée / exécutée / terminée | non | garde « déjà engagée » |
| Brouillon de vente | `DRAFT` | oui | `UpdateSalesPublishDraft` |
| Brouillon de vente | publié / annulé | non | statut |
| Contraintes de recherche | menu actif | oui (poser/remplacer/retirer) | `merge_constraints` |
| Brouillon récurrent | modifiable seulement hors exécution | voir B25/B26 — non étendu par cette phase |
| Producteur / palier d'une ligne existante | — | NON (hors périmètre : exige un snapshot d'offre vivant) ; l'utilisateur est invité à rechercher à nouveau |

## 5. Observabilité

Journaux : `business_edit_resolved | continuation_of=…`, `business_edit_applied | entity=search_constraints | before=… | after=… | kept=…`,
`business_edit_resolved | entity=<draft> | route=confirmation_gate`. Aucune donnée personnelle dans les messages.

## 6. Preuves rouges (code cassé → test qui échoue)

| # | Cassure | Test qui devient rouge |
|---|---|---|
| R1 | contrôle de version du panier désactivé | `TestVersionConflict` |
| R2 | passage validateur → porte de confirmation retiré | correction de produit vendeur (e2e) |
| R3 | prix reporté sur le nouveau produit | `test_a_product_correction_keeps_the_physical_values…` |
| R4 | drapeau `is_correction` non transmis au domaine | `test_the_correction_flag_reaches_the_domain_action…` |

## 7. Contrat de fiabilité avec le vrai modèle (stabilisation)

Principe : **le modèle peut être incertain, le domaine ne l'est jamais.** Langage incertain -> clarifier ; état périmé -> refuser ; édition valide -> l'appliquer une seule fois.

### 7.1 Validation d'édition structurée (`domain/edit_validation.py`, pure)

| Contrôle | Règle |
|---|---|
| Signal du texte | `cue_for_value(texte, nombre)` -> `MONEY` (« 280 francs », « 280/kg », « 600 le sachet »), `MEASURE` (« 250 kg », « 8 litres »), `BARE` ; construit sur `price_unit_next_to_amount` / `scan_number_candidates` / `normalize_unit` (aucune liste de phrases) |
| Quantité vs prix | `quantity` + `MONEY` -> reclassé `price` ; `price` + `MEASURE` -> reclassé `quantity` ; conflit (les deux champs déjà posés) -> clarifier |
| Unité invalide | quantité avec une unité monétaire -> clarifier |
| Nombre nu | jamais tranché ici : la question en attente / le champ nommé décide. S'il y a ≥ 2 champs numériques possibles et aucun champ nommé -> clarifier |
| « c'est pas 300 c'est 250 » | `extract_old_new_values` : l'ancienne valeur désigne le champ dans l'état courant (`resolve_edit_field_from_state`) ; deux champs égaux -> **ambigu, clarifier** ; le signal monétaire/mesure de l'ancien nombre départage |
| Valeur absente | jamais d'édition (« c'est trop cher » ne pose aucun plafond) |

Branchements : correction vendeur (`flows/producer/sales_confirmation.py`, avant `resolve_domain_action`), ligne de panier
(`flows/buyer/cart.py`, une valeur monétaire n'est jamais une quantité de ligne).

### 7.2 Accord ou refus ≠ correction

* Un `CONFIRM`/`REJECT` porteur d'un nombre est invalide (`message_carries_value`) : relance de réparation puis `UNKNOWN`, jamais d'exécution.
* Un `CONFIRM`/`REJECT` qui porte un `cart_edit` est lu comme l'édition que sa structure décrit.
* Un `CONFIRM`/`REJECT` libre en attente de validation reçoit une seconde opinion indépendante (« demande-t-il aussi un changement ? ») ; veto
  uniquement sur un « oui » explicite (illisible/panne -> pas de veto). Le veto donne `UNKNOWN` : rien n'est exécuté.

### 7.3 Clarification sûre

La question est affichée avec le récapitulatif inchangé ; le brouillon (version) et la confirmation en attente ne bougent pas.

### 7.4 Évaluation multi-passes (`tests/field_corpus/test_business_edits.py`)

Issue d'une passe : `DIRECT` (état final = attendu, ou inchangé quand « ne rien muter » est correct) · `SAFE_CLARIFICATION` (inchangé alors qu'un effet
était attendu, ou menu de recherche abandonné sans donnée métier touchée) · `UNSAFE` (autre changement, ou outil d'exécution appelé).
Agrégat par cas : `STABLE_PASS`, `FLAKY_PASS`, `SAFE_FAILURE`, `UNSAFE_FAILURE`. Critère de release : **0 `UNSAFE`**.

```bash
# CI / local, déterministe (modèle scripté, y compris FAUX pour les cas adversariaux)
python -m pytest tests/field_corpus/test_business_edits.py tests/unit/test_edit_validation.py -q
# vrai modèle, 3 passes, rapport JSON hors Git
FIELD_CORPUS_REAL=1 FIELD_CORPUS_RUNS=3 REDIS_URL=redis://localhost:6379/15   python -m pytest tests/field_corpus/test_business_edits.py -k real_model -s -p no:cacheprovider
```

Mesures (29 cas × 3 passes, base #107 avant correctifs -> après) :

| | base #107 | après |
|---|---|---|
| réussite directe | 71,9 % | 90,7 % |
| clarification sûre | 13,5 % | 9,3 % |
| mutation dangereuse | 14,6 % | **0 %** |
| clarification inutile (effet attendu) | 15,6 % | 9,1 % |
| cas STABLE / FLAKY / SAFE_FAILURE / UNSAFE | 17 / 6 / 2 / 5 | 22 / 7 / 0 / 0 |

### 7.5 Matrice champ / opération

| Contexte | Phrase | Champ | Opération |
|---|---|---|---|
| ligne de panier | `mets 20 litres` | quantity | SET |
| ligne de panier | `enlève ça` | ligne | REMOVE |
| vendeur | `non 250 kg` | quantity | SET |
| vendeur | `à 350 francs` | price | SET |
| vendeur | `c'est pas 300 c'est 250` | champ valant 300 dans l'état (quantity) | SET |
| recherche | `le prix n'importe plus` | max_price | REMOVE |
| recherche | `c'est trop cher` | max_price | valeur manquante -> demander |
| confirmation | `oui mais mets 10` | quantity | SET + invalidation de la confirmation |

### 7.6 Observabilité

`business_edit_requested` (parse), `business_edit_validation` (reclassement / ancienne valeur résolue), `business_edit_safe_clarification`,
`business_edit_unsafe_blocked`, `business_edit_applied`, `business_edit_conflict`. Métriques préparées par le harnais d'évaluation :
`business_edit_direct_success_rate`, `business_edit_safe_clarification_rate`, `business_edit_unsafe_mutation_rate`, `unnecessary_clarification_rate`.

## 8. Limites déclarées

* Le vrai modèle reste variable : 7 cas critiques sont `FLAKY_PASS` (réussissent ou clarifient selon la passe), aucun `UNSAFE`. Résultat mesuré sur 3 passes
  d'un seul fournisseur/modèle, pas une garantie statistique.
* Un nombre nu avec plusieurs champs possibles (« non 280 » face à quantité ET prix) déclenche une clarification : volontaire (sûr).
* Les brouillons vendeur sont versionnés (CAS) ; pas de test Postgres ajouté (aucune nouvelle mutation DB ; chemin pré-commande couvert par la CI existante).
* Éditions de brouillons récurrents, de producteur ou de palier d'une ligne existante : non traitées (voir matrice §4).
* Un `REJECT` structuré lu sur un menu de recherche peut abandonner le menu (« c'est trop cher » parfois) : classé échec sûr, aucune donnée métier touchée.
