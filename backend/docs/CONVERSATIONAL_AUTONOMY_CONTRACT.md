# Conversational Autonomy — contrat d'interaction flexible

```
UN MENU EST UN CONTEXTE, PAS UN PROTOCOLE.   UN NUMÉRO EST UN RACCOURCI.   UN MESSAGE EST DU LANGAGE NATUREL.
L'utilisateur n'a jamais à apprendre l'interface de Ladini : Ladini apprend son intention.
```

## 1. Les menus sont une aide optionnelle

Un menu (producteurs, conditionnements…) n'est jamais un format de réponse obligatoire. Le contexte (but métier + options
**réellement affichées**) sert de *prior* pour comprendre le message : sélection, affinage, correction, question, interruption.

## 2. Deux chemins, une seule commande domaine

| Chemin | Exemples | Où |
|---|---|---|
| **A — raccourci fermé** (déterministe, sans LLM) | `4`, `04`, `4.`, `option 4`, `choix 4`, `n°4` | `interpreter/routing.py` (fast-path 1, `_CLOSED_INDEX`) — `interaction_mode=NUMERIC_FAST_PATH` |
| **B — référence naturelle** (LLM → référence structurée → résolveur Python) | `le quatrième`, `Gilbert`, `celui à 500`, `celui de Ouaga`, `le moins cher`, `le sachet de 500 ml` | `structured_action_*` + `domain/selection_reference.py` — `interaction_mode=NATURAL_REFERENCE` |

Les deux convergent vers la même `SelectionAction` validée (`domain/selection_actions.py::validate_action`, seule porte) puis `cart.py`.
Un nombre accompagné d'une unité/d'un mot de quantité (`4 litres`, `500 francs`, `mets-en 4`) n'entre **jamais** dans le chemin A.

## 3. Le LLM ne choisit jamais d'identifiant

Il comprend le langage et renvoie une **référence structurée** :

```json
{"reference_type": "ORDINAL",    "ordinal": 4}              // ou "position": "LAST" | "PENULTIMATE"
{"reference_type": "ATTRIBUTE",  "producer_name": "Gilbert", "price": 500, "region": "Ouaga", "volume": 0.5, "packaging": "sachet"}
{"reference_type": "PREFERENCE", "criterion": "CHEAPEST" | "HIGHEST_AVAILABILITY" | "SUBJECTIVE"}
{"reference_type": "REFINEMENT", "region": "...", "max_price": 600, "criterion": "CHEAPEST"}
{"reference_type": "PAGINATION"}      // « montre les autres »
{"reference_type": "NONE_OF_THESE"}   // « aucun ne me convient »
```

Tout champ non déclaré (`producer_id`, `offer_id`…) est ignoré par le contrat (Pydantic). Le résolveur compare la référence aux
**options visibles** et produit `EXACT | AMBIGUOUS | NOT_FOUND | NOT_VISIBLE`. Le « moins cher » n'est **jamais** calculé par le modèle.

## 4. Résolution de cible

| Candidats | Résultat |
|---|---|
| 0 | `NOT_FOUND` — on le dit (« Je ne vois pas d'offre à 700… »), le menu reste actif |
| 1 | `EXACT` — sélection |
| N | `AMBIGUOUS` — **clarification ciblée** avec les faits qui distinguent (« Tu parles de Gilbert-prod (500 FCFA/L) [n°4] ou de Gilbert Ouedraogo (650 FCFA/L) [n°5] ? ») |
| option jamais montrée | `NOT_VISIBLE` — « pas encore affiché, dis *montre les autres* » (jamais devinée) |

Critères **objectifs** (moins cher, plus de stock) : tranchés seulement entre options **comparables** (même unité, base de prix
non-lot) ; une égalité reste `AMBIGUOUS`. Critère **subjectif** (« le plus intéressant ») : toujours clarifié.
Nom : tous les mots dits doivent correspondre (accents/traits d'union ignorés). Région : résolveur géographique existant
(`Ouaga` ⇒ Kadiogo : `Ouagadougou` et `Kadiogo` correspondent). Prix/stock : égalité exacte sur le visible. Une mutation n'a lieu que sur **une seule** cible.

## 5. Le snapshot est le contexte sémantique

`vendor_selection_context.vendors` porte les **faits visibles** de chaque offre (nom, zone, prix, libellé de prix certifié, unité,
stock, conditionnements, `offer_id`, `display_index`, `menu_id`). `SelectionContext.producer_options[*].facts` les expose au résolveur ;
le libellé montré au modèle contient région et stock. Les références naturelles se résolvent **dans ce snapshot**, jamais dans toute la base.

## 6. Shortlist WhatsApp

Par défaut `BUYER_SHORTLIST_SIZE=5` offres affichées (ordre du ranking existant — aucun nouveau score) puis
« J'ai aussi N autres offres — dis « montre les autres » ». Les offres non montrées sont gardées à part (`vendors_more`) et ne sont
**pas sélectionnables** (ni par numéro, ni par langage) tant qu'elles ne sont pas affichées. « montre les autres » ne rédige que les
**nouvelles** offres (même `menu_id`, numérotation continue). `0` = tout afficher (ancien comportement).

## 7. Affinage, correction, interruption — pas un échec de sélection

- **Affinage** (`REFINEMENT`) : contrainte ajoutée → les offres courantes (visibles + gardées à part) sont restreintes/triées et une nouvelle shortlist est affichée ; aucun résultat → on le dit et on garde la liste.
- **Correction / quantité** : un nombre métier n'est jamais un index (garde de provenance `text_states_a_quantity`, garde de produit B4/B6) ; un index hors bornes ne mute rien.
- **Interruption / nouvelle tâche** : relation au contexte B27 (`context_arbitration`, route `DEVIATION` → classifieur NEW_TASK) — inchangée.
- **Clarification ciblée** : n'incrémente pas `retry_count` (`cognitive_guard`) : l'utilisateur n'est jamais « abandonné » pour avoir été ambigu.
- **Pas de réaffichage aveugle** : `render_selection_menu` / `render_recovery` affichent la clarification ciblée à la place du menu complet.

## 8. Multi-slot dans une phrase

`je prends Gilbert-prod, 10 litres` → désignation **et** quantité (appliquée si le nombre est réellement dans le texte, validée par le
domaine : stock, unité, minimum). `je prends le sachet de 500 ml, j'en veux 5` → palier **et** nombre de paquets. Une désignation par
`selection_index`/`selected_value` ne peut PAS porter de quantité (incident « le premier, c'est-à-dire 5 L » : 5 lu comme un index).

## 9. Observabilité

Logs structurés : `interaction_mode=NUMERIC_FAST_PATH | NATURAL_REFERENCE | REFINEMENT | CLARIFICATION` (+ `multi_slot=…`, `pagination=SHOW_MORE`,
`menu_redisplay=suppressed`). Les compteurs `menu_numeric_reply_rate`, `menu_natural_reply_rate`, `menu_redisplay_rate`,
`targeted_clarification_rate`, `menu_abandonment_rate`, `average_options_shown` se dérivent de ces champs (pas de store de métriques ajouté ici).
KPI critique : part des interactions résolues **sans** la syntaxe suggérée = `NATURAL_REFERENCE + REFINEMENT` / total des réponses de menu.

## 10. Limites assumées

- Demande initiale complète (« 20 L de lait en sachets de 500 ml à Ouaga, max 600 FCFA/L ») : l'extracteur existant et les filtres du catalogue ne sont pas étendus ici (le prix max / conditionnement ne filtrent pas la 1ʳᵉ recherche) ; l'affinage ci-dessus le fait au tour suivant.
- Commandes photo en langage naturel (« les photos du quatrième »), annulation à portée explicite (sélection / panier / action), menus vendeur/enchères/stock : le résolveur est générique, mais seuls les menus **producteur** et **conditionnement** sont branchés dans cette phase.
- Péremption d'un menu : on réutilise les mécanismes de snapshot existants (`menu_id`, TTL) ; aucune résolution silencieuse sur un nouveau menu n'est ajoutée, mais le comportement « snapshot expiré + référence naturelle » n'a pas de test dédié.
- Pas de modèle réel appelé en CI : les tests jouent le rôle de la compréhension (LLM scripté) ; le résolveur, le contrat et le graphe sont réels.
