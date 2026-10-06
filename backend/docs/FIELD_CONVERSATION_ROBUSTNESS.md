# Field Conversation Robustness — langage réel du terrain

```
L'utilisateur ne parle pas en flux. Il parle en fragments, corrections, références, questions, raccourcis et changements d'avis.
Ladini conserve le contexte métier tout en comprenant ce langage. L'état du domaine reste structuré ; la conversation, non.
```

Aucun nouveau routeur, planner, graphe ni framework : on durcit les contrats existants (micro-prompts, résolveur, rendu).

## 1. Méthode : le vrai modèle d'abord

Les tests scriptés prouvent le CODE, pas la compréhension. Cette phase a donc été pilotée par un **corpus terrain** rejoué avec le **vrai modèle**
(Groq via la gateway de production, MCP simulé) puis, une fois les contrats stabilisés, figé en tests déterministes.

`tests/field_corpus/` : `cases.py` (contexte métier + phrase + invariants attendus), `harness.py` (même cas en mode scripté ou réel), `test_field_corpus.py`.
Mode réel : `FIELD_CORPUS_REAL=1 REDIS_URL=… pytest tests/field_corpus -s` (mesure, ne fait pas échouer la CI). `debug_case.py` / `debug_seller.py` : un cas, avec ce que le modèle a réellement renvoyé.
Les invariants sont des **effets métier** (producteur choisi, panier, état préservé, pas de menu rejoué…), jamais un texte exact de réponse.

## 2. Ce que le vrai modèle a révélé (et qu'aucun test scripté ne voyait)

| # | Constat réel | Catégorie | Correction |
|---|---|---|---|
| 0 | « Je veux du lait » → UNKNOWN : le modèle écrit `null` pour une liste vide, le schéma NEW_TASK le rejetait (puis le repair aussi) | RELATION | lecteur tolérant : `null` → `[]` (`new_task_contract`) |
| 1 | « il livre ? » → « Oui, nous effectuons bien la livraison » ; « c'est certifié ? » → « Oui, nos produits sont certifiés » (**faits inventés**) | DOMAIN/RENDER | règle `NO_FABRICATED_FACTS_RULE` dans les réponses libres + question comprise puis répondue par le domaine |
| 2 | « lequel est moins cher ? » traité comme une SÉLECTION (muté : producteur choisi) | RELATION | disposition `QUESTION` (le modèle comprend, ne répond pas) |
| 3 | « pas celui-là, l'autre » → GARIKO choisi arbitrairement | REFERENCE | référence `OTHER` ; sans option rejetée nommée → clarification |
| 4 | « celui-là » → `ORDINAL LAST` : un ordinal que l'utilisateur n'a pas dit | REFERENCE | `ordinal_is_evidenced` ; `selection_is_evidenced` pour un index deviné |
| 5 | « c'est trop cher » → REJECT → « j'annule ce choix » et menu effacé | RELATION/STATE | objection = REFINEMENT ; on demande le plafond, le menu reste |
| 6 | « pas Gilbert, Moussa » → `OTHER(Gilbert)` (3 candidats) ; changement de producteur impossible après le choix | REFERENCE | prompt : remplaçant nommé = ATTRIBUTE ; producteurs exposés aux étapes palier/quantité |
| 7 | « le quatrième » → `ORDINAL 4` + `selection_index 4` : JSON invalide, puis UNKNOWN | RELATION | lecteur tolérant (la référence fait foi) |
| 8 | `{"reference_type": "SUBJECTIVE"}` (critère écrit comme type) → invalide | RELATION | lecteur tolérant → PREFERENCE |
| 9 | `"disposition": "BUYER_VIEW_CART"` (nom d'intention à la place de NEW_TASK) → AMBIGUOUS | RELATION | lecteur tolérant |
| 10 | « J'ai 300 kg de tomates **à vendre à 250 FCFA/kg** » → AMBIGUOUS publier/stocker | RELATION | règle : « à vendre » / prix de vente dit = intention de vente |
| 11 | « à 300 francs » après un récapitulatif → UNKNOWN : la correction était **perdue en silence** | STATE | signal d'état `draft_context` : un message court corrige le récap |
| 12 | réponse non comprise sur un menu producteur → menu complet rejoué | RENDER | question courte « Tu parles de quel producteur ? (noms) » |

## 3. Contrats ajoutés / durcis

- `QUESTION` (micro STRUCTURED_ACTION) + `domain/context_answers.py` : TOTAL (panier), STOCK (avec fraîcheur), PRICE, LOCATION, COMPARISON (objectif calculé en Python ;
  « lequel est mieux ? » → faits + « Tu préfères quoi ? », jamais un classement), DISTANCE (« je ne connais que la région »), DELIVERY / CERTIFICATION → **« Je n'ai pas cette information »**.
  Répondre ne mute rien (menu, choix, panier, attente intacts ; pas de retry).
- Références : `OTHER`, `REFINEMENT.objection` (PRICE → « Tu veux rester sous quel prix ? » puis un nombre = plafond ; DISTANCE → région), `NONE_OF_THESE`.
- Changement d'avis de producteur après le choix (étapes palier / paquets / quantité) : `SELECT_PRODUCER` validé contre le menu vivant.
- Anti-cible-arbitraire : un index/ordinal que le texte n'étaye pas n'est jamais exécuté.
- Lecteurs tolérants (bruit de format d'un vrai modèle ≠ réponse invalide).
- Observabilité : `relation_type=` (ANSWER/SELECTION/REFINEMENT/QUESTION/REJECTION/INTERRUPTION/AMBIGUOUS), `interaction_mode=CONTEXT_QUESTION`, `context_question=true`,
  `clarification_reason=` (`selection_not_evidenced`, `ordinal_not_said`, `unreadable_reply`), `refinement_type=price_ceiling_requested`.
- KPI préparés (`tests/field_corpus/metrics.py`) : `user_repetition_rate`, `conversation_repair_rate` — dérivables des journaux, pas de nouveau stockage.

## 4. Localités
Villes et fautes → 17 régions canoniques (`Ouaga`, `ouagadogou` → Kadiogo ; `Bobo` → Guiriko). Un quartier (`Tampouy`, `Patte d'Oie`, `Karpala`) est **UNKNOWN** : jamais rattaché arbitrairement
(le parcours demande la région sans prétendre connaître le quartier).

## 5. Limites assumées
- **Pas de correction de ligne de panier** : « je voulais dire 20 pas 10 » / « non 5 » après l'ajout au panier ne sont pas comprises — aucune capacité « modifier une ligne » n'existe dans le domaine
  (ni intention, ni service). Mesuré avec le vrai modèle, jamais scripté. Ajouter la capacité est un travail de domaine, pas de conversation.
- Correction de PRODUIT sur un récapitulatif vendeur (« en fait c'est oignon pas tomate ») : le système demande « annuler et recommencer la vente ? » (sûr, mais pas en un tour).
- « l'autre » : exact seulement si l'option rejetée est nommée ; il n'y a pas de suivi de « focus » entre tours.
- Corpus vendeur : 5 cas (récapitulatif / quantité manquante) ; récurrent, appels d'offres et profile gate sont couverts par les suites existantes (B27, multislot, gate), pas par ce corpus.
- « plus proche » : jamais calculé (aucune distance réelle) ; certification / livraison : jamais inventées.
- Le vrai modèle est **non déterministe** : le taux mesuré varie d'un run à l'autre de quelques cas ; le rapport donne un ordre de grandeur, pas une garantie.
