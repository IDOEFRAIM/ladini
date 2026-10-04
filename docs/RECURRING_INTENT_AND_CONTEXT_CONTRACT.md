# Contrat « intention et contexte » du domaine recurring (B23)

> EXPECTATION guides interpretation. SEMANTIC INTENT decides the task. DOMAIN SERVICES execute the task.

Le recurring suit la même chaîne que les autres domaines Ladini — aucun routeur, moteur conversationnel ni gestionnaire de
contexte propre au domaine :

```
message → arbitrage du contexte (pur, fermé) → input_interpreter (micro-prompts SELECTION / NEW_TASK)
        → memory_update / resolve_context → validator → flow métier → service / MCP / DB → réponse
```

Code : `interpreter/context_arbitration.py`, `interpreter/routing.py`, `interpreter/selection_micro.py`,
`flows/buyer/recurring_need.py`, `services/database/recurring_supply.py`.

## 1. Rôle de `intent_intelligence` (interpréteur + catalogue `INTENT_CONFIG`)
Il décide la **tâche** : intention + entités, rien d'autre. Un menu actif ne le remplace jamais. Le micro-prompt SELECTION
juge « réponse au menu / interruption / ambigu » ; en cas d'interruption, le micro-prompt NEW_TASK classe la demande
(même catalogue que hors menu). Aucune liste de phrases recurring n'est ajoutée : le modèle comprend, Python valide.

## 2. Rôle de `resolve_context` (`memory_update` + `context_resolver`)
Résout les références (index → identifiant, produit nommé → besoin). Un index de menu n'est jamais une cible : le menu
fournit un identifiant exact (`REFRESH:<recurring_need_id>`, `CONFIRM:<need>|<occurrence>|<version>`), vérifié ensuite par
le service (propriété de l'acheteur). Contexte insuffisant → clarification, jamais une devinette.

## 3. Rôle de `validate_node` (`validator` + garde de la couche domaine)
Les actions sensibles se valident hors du LLM : (a) une **sélection en langage libre** qui désigne une entrée mutante d'un
menu (accepter/refuser une proposition) est marquée `closed_reply_required` (`guard_free_text_selection`) et le flow
demande un numéro/alias fermé ; (b) une modification qui **nomme un produit** différent du besoin du contexte n'est pas
appliquée (`_resolve_target_need`) ; (c) accepter/refuser vérifie version exacte, identité de l'occurrence et stock (service).

## 4. Attente vs intention
Un menu actif est une **attente** : il publie ce qu'il propose (`working_memory.recurring_need_menu.{labels,title,actions}`,
lu par `context_arbitration.live_menu_view`) et sert de *prior* au micro-prompt SELECTION. Il n'est pas une obligation :
une nouvelle intention explicite le supersede (relation `NEW_TASK`). Relations reconnues, mêmes concepts que l'existant :
`ANSWER` (SELECTION), `NEW_TASK`/`INTERRUPTION`, `CORRECTION` (UPDATE dans le goal courant), `UNRELATED` (OUT_OF_SCOPE),
clarification (UNKNOWN). Journal : `INTENT_ARBITRATION current_goal expected_action semantic_intent
relation_to_expectation selected_route reason` (aucun texte utilisateur).

Ordre de décision réel (inchangé, B20 conservé) : 1. réponse sortante interactive la plus récente · 1b. réponse fermée au menu
récurrent vivant · 2. navigation / nouvelle demande explicite · 2b. « annuler » · 3. menu fantôme purgé · puis, hors
arbitrage : slot actif / micro-prompts SELECTION → NEW_TASK · fallback.

## 5. Fast-paths structurés recurring (seuls déterministes autorisés)
| Entrée acceptée | Contexte requis | Action | Repli | TTL |
|---|---|---|---|---|
| chiffre présent dans `actions` | menu récurrent vivant (`SELECTION_MENU`/`GET_MY_NEEDS`) | entrée du menu | liste (« je n'ai pas compris ») | 600 s |
| `rechercher maintenant`, `actualiser`, … (`RECURRING_MENU_TEXT_ALIASES`) | idem, l'action existe dans le menu | REFRESH/VIEW/ORDERS/LIST | micro-prompt | 600 s |
| `accepter…` / `refuser…` / `confirmer` / `je refuse` | idem, l'action CONFIRM/REJECT existe | accepter/refuser (version exacte) | clarification | 600 s |
| `retour`, `mes besoins` (phrase entière) | idem | liste | — | 600 s |
| `mes besoins`, `mes commandes` (phrases entières) | tout contexte interruptible | navigation | — | — |
| `voir les détails`, `mes besoins` | message sortant interactif (digest) le plus récent | détail / liste | — | TTL du digest |

Retiré en B23 : la sous-chaîne « besoin » (`_BACK_WORDS`) qui capturait « j'ai besoin de … » comme un retour.

## 6. Interruption par nouvelle tâche
Tout autre texte libre part au micro-prompt SELECTION, qui **voit les options affichées**. `INTERRUPTION` → NEW_TASK sur l'état
sans le menu (`goal_planner` purge l'ancien contexte). `UNKNOWN` ou LLM défaillant → clarification, jamais une action de menu
fabriquée. Les entrées mutantes ne s'obtiennent jamais en langage libre.

## 7. Besoin vs occurrence
| Objet | Nature | Se modifie par |
|---|---|---|
| `RecurringNeed` | contrat durable | `update_recurring_need` (quantité/fréquence permanentes, pause, reprise, annulation) |
| `RecurringNeedOccurrence` | une livraison particulière | skip / override d'occurrence (`update_recurring_need`), matching |
| `NeedAllocation` | proposition de sourcing | moteur de matching uniquement |
| `Order` | exécution après décision | `accept_match_proposal` (version + stock) |

Ne jamais mélanger « modifier le besoin » et « modifier une occurrence » : `_resolve_update_action` expose deux familles d'actions distinctes.

## 8. Intents recurring actuels
| Intent / capacité | Nom actuel | Routé vers | Service |
|---|---|---|---|
| CREATE_RECURRING_NEED | `CREATE_RECURRING_NEED` | `_create_flow` (draft + CAS) | `create_recurring_need(s)` |
| GET_MY_RECURRING_NEEDS / DETAIL | `GET_MY_NEEDS` (liste → détail par menu) | `_get_my_needs_flow` | `list_my_recurring_needs`, `get_recurring_need_detail`, `ensure_next_recurring_occurrence` |
| REFRESH_RECURRING_MATCHING | entrée de menu `REFRESH:<id>` | `_refresh_matching` | `refresh_recurring_need_matching` |
| ACCEPT / REJECT_RECURRING_PROPOSAL | entrées de menu `CONFIRM:` / `REJECT:` ; digest | `_respond_to_match` | `accept_match_proposal` |
| UPDATE / PAUSE / RESUME / CANCEL / SKIP / OVERRIDE | `UPDATE_RECURRING_NEED` + `action` | `_update_flow` | `update_recurring_need` |

## 9. Intents futurs (après B24)
Exécutables par message libre : `CREATE`, `UPDATE` (quantité et/ou fréquence permanentes, appliqué directement : réponse « ancien → nouveau »),
`GET_MY_NEEDS`, `REFRESH_RECURRING_MATCHING` (« cherche pour mes bœufs »). `PAUSE`, `RESUME`, `SKIP_OCCURRENCE`, `OVERRIDE_OCCURRENCE`, `CANCEL` sont
exécutables **uniquement après une confirmation fermée** (§12) : le message ne produit qu'un menu portant l'ordre exact.
Restent à faire : `ACCEPT/REJECT` en langage libre, `GET_RECURRING_HISTORY`, `GET_RECURRING_ORDERS`. Hors périmètre : multi-producteur, packages, paiement, frontend.

## 10. Invariants de sécurité
1. Le LLM ne modifie ni la base, ni une proposition, ni une commande : il produit intention + entités.
2. Aucune mutation recurring ne s'appuie sur un index de menu seul : identifiant exact + vérification de propriété par le service (même pour un ADMIN `can_buy`).
3. Langage libre ≠ réponse de menu mutante (accepter/refuser) : numéro ou alias fermé obligatoire.
4. Produit nommé ≠ besoin du contexte → clarification, pas de mutation.
5. Une mauvaise action destructive ou métier vaut moins qu'une clarification.
6. Les garanties B20 (menu périmé, dernier message interactif, navigation déterministe, TTL) restent en vigueur.
7. Les logs de décision ne contiennent aucune donnée personnelle ni le texte de l'utilisateur.

## 11. Semantic Arbitration Contract (B24)
1. Une attente (menu, écran, brouillon) **guide** l'interprétation ; elle ne la décide pas (menu ≠ routeur sémantique).
2. L'intention sémantique identifie la tâche ; la `relation_to_context` (ANSWER, CORRECTION, NEW_TASK, INTERRUPTION, UNRELATED, AMBIGUOUS) dit comment le message se rapporte à la tâche en cours.
3. Les entrées fermées (« 1 », « 2 », « confirmer », « retour ») restent déterministes et ne passent pas par le LLM.
4. Un texte libre n'est jamais exécuté comme SELECTION aveugle : une SELECTION issue du LLM sur du texte libre est requalifiée en NEW_TASK (garde `guard_free_text_selection`).
5. Cible : 0 candidat → introuvable ; 1 → identifiant exact ; N → NEED_SELECTION (menu des seuls candidats). Un identifiant venant du contexte, d'un menu périmé ou forgé n'est cible que s'il figure dans la liste de l'acheteur courant.
6. Le validateur et le service restent seuls juges des invariants (propriété par `buyer_id`, ADMIN `can_buy` via capacités, jamais `if role == ADMIN`) ; le LLM ne produit que l'intention et les entités.
7. Besoin ≠ occurrence : modifier le besoin (permanent) n'est pas modifier une livraison ; l'ambiguïté destructive est clarifiée.
8. Les clarifications sont ciblées (« quelle quantité pour votre besoin de Bœuf ? »), jamais le message générique.
9. Seul ce que l'utilisateur dit CE tour (`entities_said_this_turn`) alimente une modification ; `transaction_payload` n'est jamais réappliqué.
10. Chaque décision émet `INTENT_ARBITRATION` (relation_to_context, relation_to_expectation, target_type, target_resolution, selected_route, decision_reason) sans texte ni donnée personnelle.

## 12. Correction, réponse fermée, cible, portée, mutation (B24-correction)

**Correction vs New Task.** Une correction (« mets-en 3 », « finalement 3 », « plutôt chaque mois ») modifie la tâche/la cible affichée ; une
nouvelle tâche (« j'ai besoin de 2 chèvres chaque semaine ») la remplace et neutralise l'ancien menu. Un produit nommé différent n'est jamais une
correction du besoin affiché (nouvelle tâche ou clarification). Aucun mot-clé ne décide : l'interprétation sémantique + `derive_relation`.

**Free-text vs Closed Reply.** Réponse fermée = le message ENTIER est un index (`1`, `01`, `1.`, `1)`, `option 1`, `choix 1`, `numéro 1`) ou un
alias fermé du menu (`rechercher maintenant`, `actualiser`, `retour`, `confirmer`, `refuser`, `oui`, `non`) — `closed_menu_index`, zéro appel LLM.
`je veux 1 chèvre`, `mets-en 1`, `j'en veux 1 de plus` ne sont PAS fermés. Une SELECTION issue du LLM sur du texte libre : action d'écran ou
mutante → jamais exécutée (`free_text_selection_rejected` / `closed_reply_required`) ; choix d'entité de liste → seulement si le message l'étaye
(mot du libellé affiché ou ordinal, `selection_is_evidenced`) — un chiffre dans une phrase est une quantité, pas un numéro d'écran.

**Target Resolution.** Avant toute mutation : un `recurring_need_id` exact (0 → introuvable, 1 → exact, N → menu des candidats), et pour une
livraison une `occurrence_date` lue du service (prochaine occurrence `OPEN`). L'index d'un menu ne sert qu'à retrouver l'id ; il n'est jamais envoyé au service.

**Need vs Occurrence.** « je veux désormais 5 chaque semaine » → UPDATE du besoin ; « cette semaine mets-en 5 » → override d'UNE occurrence ;
« pas cette semaine » → skip d'UNE occurrence ; « arrête complètement » → CANCEL du besoin. « annule … » est ambigu : menu fermé
(1. ignorer la prochaine livraison · 2. arrêter complètement, avec seconde confirmation · 3. ne rien changer).

**Ambiguity and Clarification.** Message trop vague sur un écran vivant → question ciblée (quantité / fréquence / prochaine livraison) ;
deux besoins candidats → menu des seuls candidats ; ambiguïté destructive → menu fermé. Jamais le message générique tant qu'un écran récurrent est vivant.

**Mutation Safety (qui garantit quoi).** Flow : action permise, cible exacte, quantité > 0, fréquence connue, jours requis, portée besoin/occurrence,
ambiguïté levée, snapshot confirmé (menu = commande exacte, TTL 600 s, menu périmé = rien). Service `update_recurring_need` : propriété (`buyer_id`),
besoin existant, occurrence `OPEN`, idempotence naturelle (rejouer un skip échoue). Pas de `expected_version` sur ce service (hors périmètre ; le
snapshot daté et l'état `OPEN` en tiennent lieu). Le LLM n'est jamais l'autorité.
