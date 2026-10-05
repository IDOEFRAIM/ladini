# Contrat de conversation naturelle (B27)

> L'utilisateur ne suit pas la machine à états de Ladini. Ladini suit l'intention de l'utilisateur.

Ce document complète `RECURRING_INTENT_AND_CONTEXT_CONTRACT.md` (B23/B24) et `RECURRING_MUTATION_CONSISTENCY_CONTRACT.md` (B25/B26).
Aucun nouveau routeur, orchestrateur ou framework : B27 étend l'interpréteur d'entrée existant
(`interpreter/routing.py`, `interpreter/context_arbitration.py`, micro-prompts SELECTION / NEW_TASK).

## 1. Principes

1. **Un menu est un raccourci**, pas un protocole : il porte une attente, des choix visibles et des cibles candidates.
2. **Le contexte est un a priori**, pas une prison : un message sans rapport avec l'écran en sort (`RelationToContext`).
3. **Quatre responsabilités distinctes**, jamais confondues :
   - *intention sémantique* (le modèle comprend : intention, relation, entités, référence) ;
   - *résolution de cible* (le contexte et le domaine : 0 résultat = introuvable, 1 = exact, N = une seule clarification ; le modèle ne choisit jamais un identifiant) ;
   - *validation* (invariants du domaine) ;
   - *autorisation / exécution* (le service : propriété, `expected_version`, `expected_occurrence_version`, double confirmation B24-B26).
4. **« 1 » et le langage libre produisent la même commande** (même snapshot d'occurrence + version) : B26 s'applique donc à l'identique.

## 2. Pipeline

message → interprétation sémantique → relation au contexte (ANSWER / CORRECTION / NEW_TASK / INTERRUPTION / AMBIGUOUS / UNRELATED)
→ extraction d'entités → résolution de référence → résolution de cible → validation → clarification minimale
→ commande de domaine → service → réponse ancrée dans le résultat du service.

## 3. Confirmation / refus en langage naturel

Une confirmation libre (« je prends les 75 kg », « ok vas-y », « pas celui-là ») n'est acceptée que si **toutes** les conditions sont réunies :

- deux lectures indépendantes s'accordent (micro-prompt SELECTION `selection_v4` **et** NEW_TASK `new_task_v12` avec l'écran en indice) ;
- confiance ≥ 0,9 sur la sélection ; message ≤ 10 mots ; aucun nombre absent de l'écran (sinon c'est une **correction**, ex. « oui mais mets 100 ») ;
- la commande passe ensuite par le même mapping `CONFIRM:/REJECT:/EXEC:` que le chiffre (snapshot occurrence + version).

Au moindre doute : une phrase demandant le numéro (« tapez le numéro … ») ; jamais de mutation.

## 4. Références par date / nom / ordinal

Le modèle extrait `date_offset_days`, `date_day`, `date_month`, `date_role` ; **le domaine** calcule contre les faits affichés
(`facts` de chaque option : débuts et livraisons ISO). Un mot partagé par plusieurs options ne prouve rien (`selection_is_evidenced`) :
0 candidat = introuvable, 1 = exact, N = clarification ciblée qui ne liste que les candidats restants.

## 5. Contexte de récupération après échec

Quand une livraison récurrente échoue (producteur sans confirmation), la notification sortante porte `recovery_candidate` et
l'occurrence concernée (identifiants et date uniquement). `get_last_interactive_outbound` l'expose (`recovery`), l'interpréteur le
transforme en `state["recovery_context"]` (vue dérivée) ; « trouve-moi quelqu'un d'autre » vise alors cette livraison. Le **service** décide si
la relance est encore possible (`NOT_MATCHABLE` → « ne peut plus être relancée »). Un identifiant qui n'appartient pas à l'acheteur courant
n'est jamais une cible.

## 6. Liste / détail

Un détail dont les faits contredisent la liste affichée est signalé (`RECURRING_LIST_DETAIL_MISMATCH`) ; les libellés de liste incluent les dates
de démarrage / prochaine livraison pour distinguer des besoins jumeaux.

## 7. Observabilité (sans PII)

`INTENT_ARBITRATION` expose chemin, raison de décision, `target_resolution`, `relation_to_context`, `deterministic_path`, clarification ;
jamais le texte utilisateur, le téléphone ou des identifiants.

## 8. Limites connues

- Pas de primitive de fuseau métier : « aujourd'hui/demain » = date serveur.
- La compréhension (fautes, paraphrases) appartient au modèle ; prouvée avec un LLM scripté (capable / imparfait / hostile), pas évaluée sur un vrai modèle.
- Liste d'ordinaux français (`_ORDINALS`) : dépendance linguistique connue.
- Relance réelle d'une occurrence expirée non implémentée dans le service (seul le rafraîchissement de la prochaine occurrence).
- Lecture DB sortante par message sans contexte (coût d'une lecture).
- Chemin de récupération couvert : expiration de confirmation producteur (pas l'annulation explicite par le producteur).
