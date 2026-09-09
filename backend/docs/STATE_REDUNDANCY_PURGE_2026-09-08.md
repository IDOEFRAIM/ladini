# PURGE DES REDONDANCES D'ÉTAT — 2026-09-08

## Le retour utilisateur

> « une premiere grande erreur que je note ; dans les state on a des redondances ; comme role or user_role... Dans la façon de gérer le state, on finit par faire des or comme role or user_role ; c'est comme si on sait pas vraiment ce qu'on veut. Corrige ; purifie le state ; pour une information précise dans le state ; évite les redondances ; ce qui augmente le risque d'erreur. »

Diagnostic confirmé par audit exhaustif — deux redondances réelles trouvées, une bug de correction découverte au passage.

## 1. `state["role"]` vs `state["user_role"]`

### Audit (avant fix)

| Site | Écrit `role` | Écrit `user_role` |
|---|---|---|
| `adapter.py::_build_initial_state` | non | oui (`context.get("user_role") or "PRODUCER"`) |
| `nodes/role_guard.py` | oui, **seulement si absent** | oui, seulement si absent |
| `services/profile_loader.py::load_user_profile` | non | oui, à chaque chargement de profil réel |
| `nodes/input_normalizer.py` | non | oui |

**Root cause** : `role_guard` s'exécute en TOUT PREMIER (`role_guard → input_normalizer → ...`). Au premier tour, il pose `role`/`user_role` au même défaut. Ensuite, `input_normalizer`/`profile_loader` **rafraîchissent `user_role`** dès que le vrai profil est chargé (rôle réel de l'utilisateur) — mais **rien ne rafraîchit jamais `role`** : `role_guard` ne le repose que "si absent", et `role` DURABLE reste donc posé une fois pour toutes.

**Conséquence réelle** : `domain/model.py::DomainContext.from_state` lisait `state.get("role") or state.get("user_role")`. Un `or` privilégie TOUJOURS la première valeur truthy — donc la valeur **figée** de `role` gagnait systématiquement contre la valeur **à jour** de `user_role`, silencieusement. Un buyer dont le rôle réel n'est confirmé qu'après le tour d'entrée (le cas normal) aurait vu `DomainContext.role` rester bloqué sur le défaut initial pour toute la conversation. Aucun test ne le détectait : les deux valeurs sont identiques au 1er tour, l'écart n'apparaît qu'ensuite.

### Fix

- `state["role"]` retiré de `MarketAgentState` (`core/state.py`) et de `core/state_profile.py`.
- `role_guard.py` ne pose plus que `user_role`.
- `domain/model.py::DomainContext.from_state` lit uniquement `user_role`.
- Testé : `tests/unit/test_state_redundancy_purge.py::TestRoleFieldNoLongerShadowsUserRole` — reproduit littéralement le scénario (un `role` périmé dans l'état, `user_role` à jour) et vérifie que `user_role` gagne désormais.

## 2. `working_memory["active_goal"]` vs `working_memory["locked_intent"]`

### Audit (avant fix)

Recherché **chaque** site d'écriture de `locked_intent` dans tout le repo (~16 occurrences, 5 fichiers : `interpreter/goal_planner.py`, `nodes/memory.py`, `flows/producer/flow.py` ×9, `flows/buyer/procurement.py`, `flows/buyer/helpers.py`). **Sans exception**, `active_goal` et `locked_intent` sont posés à la **valeur strictement identique** dans la même opération, ou effacés ensemble. Aucune divergence, nulle part — un doublon pur, sans second sens.

Pourtant, la chaîne de lecture `state.get("current_goal") or working_memory.get("active_goal") or working_memory.get("locked_intent")` était **recopiée indépendamment dans 9 fichiers** (`core/router.py` ×2, `interpreter/strategy.py`, `interpreter/routing.py` ×2, `interpreter/goal_planner.py`, `nodes/memory.py`, `nodes/validation.py`, `nodes/rendering/common.py`, `nodes/cleaner.py`) — exactement le symptôme signalé : « on refait des OR partout ». Une chaîne dupliquée 9 fois est un risque de dérive structurel : une future correction qui n'en touche que 8 introduit une incohérence invisible jusqu'au bug suivant.

### Fix

- `working_memory["locked_intent"]` retiré partout (les ~16 sites d'écriture, plus les listes de clés à réinitialiser en fin de tour dans `nodes/cleaner.py`/`nodes/executor.py`/`interpreter/goal_planner.py::_TUNNEL_LOCK_KEYS`).
- Nouveau point de résolution **unique** : `core/state.py::resolve_current_goal(state)` — `current_goal` si présent, sinon `working_memory.active_goal`, sinon `None`.
- Les 6 sites qui faisaient la résolution complète (`current_goal or active_goal`, avec parfois un 3ᵉ repli `detected_intent` préservé tel quel) appellent désormais cette fonction. Les 2 sites qui ne faisaient qu'un repli partiel (`nodes/memory.py`, `nodes/validation.py` — `active_goal` seul, sans `current_goal`) ont juste perdu la clause `locked_intent` morte. `nodes/rendering/common.py::resolve_goal_for_ui` (fonction distincte, délibérément plus tolérante pour l'affichage — garde `suspended_goal`/`detected_intent` en plus) a aussi juste perdu la clause `locked_intent`.
- Testé : `tests/unit/test_state_redundancy_purge.py::TestResolveCurrentGoalCentralization` (5 cas : priorité `current_goal`, repli `active_goal`, absence des deux, `working_memory` corrompu, `state` corrompu).

## 3. Bug additionnel découvert en testant le fix — `DomainContext.from_state`

En écrivant le test de non-régression pour `role`, un second bug est apparu : `str(state.get(champ)) or None` **ne renvoie jamais `None`** pour un champ absent — `str(None)` est la chaîne `"None"`, non vide, donc **truthy**. Chaque champ optionnel de `DomainContext` (`user_id`, `phone`, `role`, `language`, `region`, `organization`, `tenant`, `timezone`) prenait silencieusement la valeur texte `"None"` plutôt que la valeur Python `None` quand la donnée source était absente. Un `if context.phone:` en aval aurait vu une identité **présente** (la chaîne `"None"`) là où elle est en réalité manquante.

**Fix** : nouvel helper `_opt_str(value)` (`domain/model.py`) — `None` reste `None`, jamais la chaîne littérale. Appliqué à tous les champs de `from_state`, pas seulement `role`.

## Résultat

- 2 champs redondants retirés du contrat d'état, 1 bug de conversion corrigé.
- 1 point de résolution centralisé (`resolve_current_goal`) remplaçant 9 copies indépendantes.
- 9 tests de non-régression ajoutés (`tests/unit/test_state_redundancy_purge.py`).
- Suite complète re-vérifiée : mêmes 6 échecs préexistants et sans rapport (4 `test_create_auction_catalog_gate.py`, date codée en dur ; 2 `test_order_mutations_require_ownership.py`, faux positifs de grep sur `infrastructure/mcp/exposure.py`, déjà signalés séparément) — **0 nouvelle régression**.

## Fichiers modifiés

```
core/state.py                          — retrait champ `role` + ajout resolve_current_goal()
core/state_profile.py                  — retrait FieldSpec("role", ...)
core/router.py                         — 2 sites → resolve_current_goal()
domain/model.py                        — DomainContext.from_state : user_role seul + fix str(x) or None
interpreter/strategy.py                — resolve_current_goal()
interpreter/routing.py                 — 2 sites → resolve_current_goal()
interpreter/goal_planner.py            — resolve_current_goal() + _TUNNEL_LOCK_KEYS + _lock() + _with_goal_metadata()
nodes/role_guard.py                    — ne pose plus `role`
nodes/memory.py                        — retrait clause locked_intent (lecture + écriture)
nodes/validation.py                    — retrait clause locked_intent
nodes/rendering/common.py              — retrait candidat locked_intent
nodes/cleaner.py                       — resolve_current_goal() + retrait locked_intent des clés reset
nodes/executor.py                      — retrait locked_intent des clés reset
nodes/rendering/success.py             — commentaire mis à jour
core/graph_builder.py                  — commentaire mis à jour
flows/producer/flow.py                 — 16 sites (write) — locked_intent retiré
flows/buyer/procurement.py             — 1 site
flows/buyer/helpers.py                 — 1 site
tests/unit/test_state_redundancy_purge.py       — nouveau, 9 tests
tests/chaos/test_state_machine_invariants.py    — fixture/assertion mises à jour
tests/nodes/test_confirmation_gate_reject.py    — fixtures nettoyées (3 sites)
docs/MARKET_COACH_ARCHITECTURE_MAP_2026-09-08.md — addendum §16
```
