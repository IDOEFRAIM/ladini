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

## 9. Intents futurs (non implémentés par B23)
`REFRESH_RECURRING_MATCHING` et `ACCEPT/REJECT` comme intents nommés en langage libre (« cherche pour mes bœufs »), `GET_RECURRING_HISTORY`,
`GET_RECURRING_ORDERS` en tant qu'intent, correction de quantité *dans l'écran détail* (« mets plutôt 5 »), clarification « annuler mes
tomates » (skip vs annulation), cible par nom dans la liste. Hors périmètre : multi-producteur, packages recurring, paiement, frontend.

## 10. Invariants de sécurité
1. Le LLM ne modifie ni la base, ni une proposition, ni une commande : il produit intention + entités.
2. Aucune mutation recurring ne s'appuie sur un index de menu seul : identifiant exact + vérification de propriété par le service (même pour un ADMIN `can_buy`).
3. Langage libre ≠ réponse de menu mutante (accepter/refuser) : numéro ou alias fermé obligatoire.
4. Produit nommé ≠ besoin du contexte → clarification, pas de mutation.
5. Une mauvaise action destructive ou métier vaut moins qu'une clarification.
6. Les garanties B20 (menu périmé, dernier message interactif, navigation déterministe, TTL) restent en vigueur.
7. Les logs de décision ne contiennent aucune donnée personnelle ni le texte de l'utilisateur.
