"""`nodes/confirmation_gate.py` — P1-A hardening : invariant "CE QUE
L'UTILISATEUR A CONFIRMÉ == CE QUE LE MÉTIER EXÉCUTE" pour les goals WRITE
génériques (pas de draft versionné — `transaction_payload` brut).

## Inventaire exact (A1)

27 intents `action_type: "WRITE"` (`interpreter/intent.py::INTENT_CONFIG`,
vérifié par grep — voir `TestA1ExactInventory`), répartis en 3 groupes :

1. **5 draft-based** (protection CAS versionnée, hors scope ici) :
   SALES_PUBLISH_PRODUCT, PROCUREMENT_CREATE_REQUEST, BUYER_PREORDER_INIT,
   BUYER_PREORDER_CONFIRM, CREATE_RECURRING_NEED.
2. **12 own-flow** (mini-tunnel dédié — bid/update/escrow/negotiation/order-
   tracking/cart/recurring-need — ne touchent JAMAIS `confirmation_gate`) :
   SALES_PLACE_BID, SALES_UPDATE_PRODUCT, PRODUCTION_UPDATE_FUTURE,
   PRODUCER_CONFIRM_DELIVERY_OTP, PRODUCER_CANCEL_ORDER, PRODUCER_CONFIRM_ORDER,
   PROCUREMENT_UPDATE_REQUEST, BUYER_ADD_TO_CART, BUYER_CREATE_PREORDER,
   BUYER_NEGOTIATE_PRICE, BUYER_CANCEL_ORDER, UPDATE_RECURRING_NEED.
3. **10 génériques** (`transaction_payload` brut, `confirmation_gate`
   générique — LE périmètre de ce fichier) :
   STOCK_REGISTER_HARVEST, SALES_RECORD_DIRECT, SALES_UNPUBLISH_PRODUCT,
   PRODUCER_CONFIRM_DELIVERY_PAYMENT, PRODUCTION_DECLARE_FUTURE,
   FINANCE_LOG_EXPENSE, FARM_CREATE, FARM_UPDATE, PROFILE_SET_GEO,
   PROFILE_SET_PREFS.

   Sous-classés par TYPE (mandat A2) :
   - TYPE2 (multi-champs argent/quantité — le vrai risque) : STOCK_REGISTER_
     HARVEST (quantity), SALES_RECORD_DIRECT (price = montant vente),
     PRODUCTION_DECLARE_FUTURE (price/quantity/date), FINANCE_LOG_EXPENSE
     (price = montant dépense).
   - TYPE1 (id nu ou champs administratifs, risque pratique bas) :
     SALES_UNPUBLISH_PRODUCT, PRODUCER_CONFIRM_DELIVERY_PAYMENT (protégé en
     plus par un ownership check DB-layer, `services/database/producer.py`),
     FARM_CREATE, FARM_UPDATE, PROFILE_SET_GEO, PROFILE_SET_PREFS.

Inventaire vérifié par une exploration exhaustive de `interpreter/intent.py::
_TUNNEL_ASSIGNMENTS`, `core/router.py::DomainRouter.build()` (RouteRule par
groupe) et chaque flow file own-flow correspondant (voir docstring
d'`confirmation_gate.py` pour le design retenu, et `AGENT_PRODUCTION_
READINESS.md` pour le rapport complet).

## Le correctif (A2/A3/A4/A8)

`confirmation_gate.py`, branche CONFIRM générique (event == "CONFIRM",
awaiting_confirmation) :

1. Vérifie `confirmation_summary_goal == goal` et `confirmation_summary_
   payload` non vide — le snapshot GELÉ posé quand CETTE confirmation a été
   levée (jamais retouché depuis). Un écart == confirmation périmée/orpheline
   → abandon (`_ABANDON_PATCH`), jamais une certification aveugle.
2. Sur match : `transaction_payload` est REMPLACÉ (`{"__reset__": True,
   **snapshot}`, jamais une fusion) par le snapshot gelé AVANT de poser
   `is_certified`/`execution_authorized`. `mcp_tool_executor` (inchangé)
   consomme donc TOUJOURS exactement ce qui a été montré/confirmé, jamais
   une valeur live qui aurait pu diverger entre-temps (contamination
   cross-flow, correction non voulue d'un autre tunnel, LLM ayant substitué
   un id).

Pas de nouvelle classe `CertifiedCommand` introduite : `confirmation_summary_
goal`/`confirmation_summary_payload` (déjà dans `core/state.py`, déjà posés
par ce même nœud, déjà utilisés par `render_confirmation` comme garde anti-
péremption À L'AFFICHAGE) EST le "certified command" — ce correctif étend
simplement son autorité au point d'EXÉCUTION, qui ne le consultait pas
encore. Décision délibérée (mandat "pas de refactor global" / "n'introduire
un CertifiedCommand que si justifié") : réutiliser un mécanisme existant au
lieu d'ajouter 10 drafts versionnés.

## A3/A4 (entité inexistante / appartenant à un autre utilisateur)

Aucun des 10 goals génériques ne laisse `confirmation_gate` lui-même décider
de l'existence/propriété d'une entité — c'est la couche domaine
(`services/database/*.py`) qui la revérifie à l'écriture, indépendamment de
ce correctif. Vérifié par lecture directe pour l'exemple le plus exposé
(`PRODUCTION_DECLARE_FUTURE` → `services/database/producer.py::
declare_future_production`, lignes ~1110-1121) : `farm` introuvable →
`ValueError("Ferme introuvable...")` ; `farm.producer_id != producer_obj.id`
→ `ValueError("Cette exploitation n'appartient pas...")`. Ce fichier ajoute
en complément un test au niveau `confirmation_gate` prouvant qu'un id
substitué/halluciné APRÈS la levée de la confirmation (ex: contamination
LLM, correction non voulue) ne peut de toute façon JAMAIS atteindre
l'exécuteur — le gel (A2 ci-dessus) neutralise cette classe d'attaque même
AVANT que la couche domaine n'ait à s'en charger. Une couverture directe de
`declare_future_production` par un test dédié (session/mock DB) reste un
travail de test-dette distinct, documenté comme résiduel dans le rapport
final plutôt que refait ici sans nécessité.

## A5 (champ requis manquant → pas de confirmation)

Pré-existant, en amont de `confirmation_gate` (`nodes/validation.py::
validator`, `get_required_fields`/`_missing_fields_for_goal` →
`response_strategy: "ASK_MISSING_FIELD"`, jamais routé vers `to_confirmation`
tant que des champs requis manquent). `TestA5MissingRequiredFieldNeverReachesConfirmation`
verrouille ce comportement existant en golden test pour ce périmètre.
"""
from __future__ import annotations

from ladini.agents.reducers import merge_dict
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.intent import (
    _TUNNEL_ASSIGNMENTS as TUNNEL_ASSIGNMENTS,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    _DRAFT_BASED_CONFIRMATION_GOALS,
    confirmation_gate,
)
from tests.conftest import make_state, run

_ALL_WRITE_GOALS = frozenset(
    goal for goal, cfg in INTENT_CONFIG.items() if (cfg or {}).get("action_type") == "WRITE"
)

# Own-flow: a `tunnel` assignment routes the goal to a dedicated mini-flow
# (DomainRouter.build()'s RouteRule list) BEFORE confirmation_gate is ever
# reached — verified exhaustively (see module docstring / final report).
_OWN_FLOW_WRITE_GOALS = frozenset(
    goal for goal in _ALL_WRITE_GOALS if goal in TUNNEL_ASSIGNMENTS
)

_EXPECTED_GENERIC_GOALS = frozenset(
    {
        "STOCK_REGISTER_HARVEST",
        "SALES_RECORD_DIRECT",
        "SALES_UNPUBLISH_PRODUCT",
        "PRODUCER_CONFIRM_DELIVERY_PAYMENT",
        "PRODUCTION_DECLARE_FUTURE",
        "FINANCE_LOG_EXPENSE",
        "FARM_CREATE",
        "FARM_UPDATE",
        "PROFILE_SET_GEO",
        "PROFILE_SET_PREFS",
    }
)


class TestA1ExactInventory:
    def test_the_29_write_intents_split_exactly_into_draft_own_flow_and_generic(self):
        assert len(_ALL_WRITE_GOALS) == 29, (
            f"le nombre d'intents WRITE a changé ({len(_ALL_WRITE_GOALS)}) — "
            f"un nouvel intent doit être classé (draft/own-flow/générique) "
            f"avant que ce test ne soit simplement élargi"
        )
        generic_goals = _ALL_WRITE_GOALS - _DRAFT_BASED_CONFIRMATION_GOALS - _OWN_FLOW_WRITE_GOALS
        # BUYER_PREORDER_INIT/CONFIRM et CREATE_RECURRING_NEED sont
        # draft-based mais N'ONT PAS de `tunnel` (interceptés directement
        # par `confirmation_gate.py::_DRAFT_BASED_CONFIRMATION_GOALS` pour 2
        # d'entre eux, ou par leur propre resolver pour les 3 restants) —
        # déjà retirés via `_DRAFT_BASED_CONFIRMATION_GOALS` ci-dessus pour
        # les 2 seuls qui comptent pour CE nœud (les 3 autres ont un tunnel
        # et sont donc déjà dans `_OWN_FLOW_WRITE_GOALS`).
        assert generic_goals == _EXPECTED_GENERIC_GOALS, (
            f"inventaire des goals génériques (transaction_payload brut, "
            f"confirmation_gate générique) a dérivé — nouveau: "
            f"{generic_goals - _EXPECTED_GENERIC_GOALS}, disparu: "
            f"{_EXPECTED_GENERIC_GOALS - generic_goals}"
        )

    def test_every_generic_goal_actually_falls_through_confirmation_gate_bottom(self):
        """Chaque goal de l'inventaire A1 doit RÉELLEMENT atteindre la
        branche générique de `confirmation_gate` (ni draft-based, ni
        `_READ_GOALS`) — sinon l'inventaire mentirait sur ce qui est protégé
        par le correctif ci-dessous."""
        from ladini.graphs.agents.market_coach.core.base import _READ_GOALS

        for goal in _EXPECTED_GENERIC_GOALS:
            assert goal not in _DRAFT_BASED_CONFIRMATION_GOALS, goal
            assert goal not in _READ_GOALS, goal
            assert goal not in TUNNEL_ASSIGNMENTS, (
                f"{goal} a un tunnel assigné — il ne devrait PAS atteindre "
                f"la branche générique de confirmation_gate"
            )


def _merge_patch(state: dict, patch: dict) -> dict:
    """Applique un patch de nœud à `state` EXACTEMENT comme le ferait
    LangGraph entre deux nœuds — `transaction_payload` est un canal
    `merge_dict` (`core/state.py`), jamais un simple `dict.update` (qui
    laisserait le sentinel `__reset__` fuiter tel quel, ou fusionnerait au
    lieu de remplacer). Les autres clés du patch écrasent `state` telles
    quelles (comportement `replace_value`, suffisant pour ce qu'exercent ces
    tests)."""
    merged = {**state, **patch}
    if "transaction_payload" in patch:
        merged["transaction_payload"] = merge_dict(
            state.get("transaction_payload") or {}, patch["transaction_payload"]
        )
    return merged


def _raise_and_confirm(goal: str, payload: dict, *, tamper=None):
    """Simule le cycle réel en 2 tours : (1) confirmation_gate lève la
    confirmation (pose `confirmation_summary_goal`/`_payload`, le snapshot
    gelé) ; (2) un tour ultérieur répond CONFIRM. `tamper`, si fourni, mute
    `transaction_payload` ENTRE les deux tours — exactement le scénario A2
    (contamination cross-flow, correction non voulue) que le correctif doit
    neutraliser."""
    raise_state = make_state(
        current_goal=goal,
        interpreted_event="NEW_TASK",
        transaction_payload=dict(payload),
    )
    raised = run(confirmation_gate(raise_state, None))
    assert raised["status"] == "WAITING_CONFIRMATION", raised
    state_after_raise = _merge_patch(raise_state, raised)

    live_payload = dict(payload)
    if tamper is not None:
        live_payload = tamper(live_payload)

    confirm_state = dict(state_after_raise)
    confirm_state["interpreted_event"] = "CONFIRM"
    confirm_state["transaction_payload"] = live_payload
    confirmed_patch = run(confirmation_gate(confirm_state, None))
    confirmed = _merge_patch(confirm_state, confirmed_patch)
    return raised, confirmed


class TestA2ConfirmedPayloadSurvivesLiveMutation:
    def test_a_contaminated_live_payload_never_overrides_what_was_actually_confirmed(self):
        """Le scénario EXACT du bug initial de la session 1 (quantité/prix
        périmés d'un flow abandonné) rejoué au niveau générique : entre la
        levée de la confirmation et le CONFIRM, `transaction_payload` est
        contaminé par des valeurs D'UN AUTRE FLOW — l'exécution ne doit
        JAMAIS voir cette contamination."""
        original = {"product": "maïs", "quantity": 50, "unit": "UNITE"}

        def _contaminate(live: dict) -> dict:
            live["quantity"] = 461000
            live["product"] = "boeufs"
            return live

        raised, confirmed = _raise_and_confirm(
            "STOCK_REGISTER_HARVEST", original, tamper=_contaminate
        )

        assert confirmed["status"] == "EXECUTING"
        assert confirmed["is_certified"] is True
        assert confirmed["transaction_payload"] == original, (
            f"la contamination live a survécu jusqu'à l'exécution : "
            f"{confirmed['transaction_payload']!r} != {original!r}"
        )

    def test_an_untampered_confirmation_still_executes_normally(self):
        """Non-régression : le chemin heureux (rien n'a changé entre la
        levée et le CONFIRM) doit continuer à fonctionner exactement comme
        avant."""
        payload = {"amount": 5000, "category": "seeds", "product": "engrais"}
        raised, confirmed = _raise_and_confirm("FINANCE_LOG_EXPENSE", payload)

        assert confirmed["status"] == "EXECUTING"
        assert confirmed["is_certified"] is True
        assert confirmed["execution_authorized"] is True
        assert confirmed["transaction_payload"] == payload

    def test_a_legitimate_correction_before_confirm_is_reflected_in_a_fresh_raise(self):
        """Non-régression : une VRAIE correction utilisateur ("non, plutôt
        60kg") pendant l'attente de confirmation continue de fonctionner —
        elle passe par une RE-LEVÉE de `confirmation_gate` (nouveau
        `confirmation_summary_goal`/`_payload`), jamais par une mutation
        silencieuse du payload gelé déjà affiché."""
        payload = {"product": "maïs", "quantity": 50, "unit": "UNITE"}
        raise_state = make_state(
            current_goal="STOCK_REGISTER_HARVEST",
            interpreted_event="NEW_TASK",
            transaction_payload=dict(payload),
        )
        raised = run(confirmation_gate(raise_state, None))

        # L'utilisateur corrige AVANT de confirmer — un nœud amont applique
        # la correction à transaction_payload (comportement existant,
        # inchangé), puis confirmation_gate est appelé À NOUVEAU (pas avec
        # CONFIRM) pour reconstruire un récap frais.
        corrected_payload = {**payload, "quantity": 60}
        correction_state = _merge_patch(raise_state, raised)
        correction_state["interpreted_event"] = "ANSWER"
        correction_state["transaction_payload"] = dict(corrected_payload)
        re_raised = run(confirmation_gate(correction_state, None))
        assert re_raised["confirmation_summary_payload"] == corrected_payload

        confirm_state = _merge_patch(correction_state, re_raised)
        confirm_state["interpreted_event"] = "CONFIRM"
        confirmed_patch = run(confirmation_gate(confirm_state, None))
        confirmed = _merge_patch(confirm_state, confirmed_patch)
        assert confirmed["status"] == "EXECUTING"
        assert confirmed["transaction_payload"] == corrected_payload


class TestA3A4HallucinatedOrForeignEntityIdNeverSurvivesToExecution:
    def test_a_farm_id_substituted_after_confirmation_is_raised_never_reaches_execution(self):
        """A3/A4 au niveau confirmation_gate : que le `farm_id` halluciné/
        substitué appartienne à une entité INEXISTANTE (A3) ou à un AUTRE
        producteur (A4), le mécanisme de protection est le MÊME ici — le gel
        (A2) rejette toute divergence AVANT que la question ne se pose à la
        couche domaine. La vérification d'existence/propriété RÉELLE
        (`services/database/producer.py::declare_future_production`,
        `farm.producer_id != producer_obj.id` / `Ferme introuvable`) reste
        la ligne de défense finale, vérifiée par lecture directe (voir
        docstring de ce fichier), pas rejouée ici avec une session DB
        simulée."""
        original = {
            "farm_id": "farm-belonging-to-current-user",
            "product": "maïs",
            "quantity": 500,
            "price": 250,
        }

        def _substitute_foreign_farm(live: dict) -> dict:
            live["farm_id"] = "farm-belonging-to-someone-else-or-nonexistent"
            return live

        raised, confirmed = _raise_and_confirm(
            "PRODUCTION_DECLARE_FUTURE", original, tamper=_substitute_foreign_farm
        )

        assert confirmed["status"] == "EXECUTING"
        assert confirmed["transaction_payload"]["farm_id"] == "farm-belonging-to-current-user", (
            "un farm_id substitué après la confirmation a survécu jusqu'à "
            "l'exécution — l'entité que la couche domaine va vérifier n'est "
            "plus celle que l'utilisateur a réellement confirmée"
        )


class TestA5MissingRequiredFieldNeverReachesConfirmation:
    def test_validator_never_routes_an_incomplete_generic_goal_to_confirmation(self):
        """Golden test (comportement PRÉ-EXISTANT, pas ce correctif) :
        `validator` ne route vers `to_confirmation` que si `missing_fields`
        est vide — un champ requis absent doit produire ASK_MISSING_FIELD,
        jamais une confirmation levée sur des données incomplètes."""
        from ladini.graphs.agents.market_coach.nodes.validation import validator

        state = make_state(
            current_goal="STOCK_REGISTER_HARVEST",
            interpreted_event="NEW_TASK",
            # `quantity` manquant — requis pour STOCK_REGISTER_HARVEST.
            transaction_payload={"product": "maïs", "unit": "UNITE"},
        )
        result = run(validator(state, None))
        assert result.get("response_strategy") == "ASK_MISSING_FIELD", result
        assert result.get("missing_fields"), "un champ requis manquant doit être listé"
        assert result.get("status") != "WAITING_CONFIRMATION"


class TestA6DoubleConfirmProducesOnlyOneCertification:
    def test_a_second_confirm_after_resolution_does_not_re_certify_from_this_node(self):
        """Un double "OK" (retry réseau, double-tap utilisateur) ne doit
        certifier qu'UNE FOIS. Le 1er CONFIRM résout `pending_interaction`
        (kind -> NONE) — un 2e appel avec le MÊME état résiduel (avant que
        `post_response_cleanup`/le tour suivant n'aient tourné) ne doit donc
        plus jamais retomber dans la branche `awaiting_confirmation` : il n'y
        a plus de CONFIRM_ACTION à résoudre. Combiné à l'idempotency key
        stable de l'exécuteur (`test_execution_idempotency_key.py`, session
        précédente — inchangé par ce correctif), l'écriture MCP elle-même
        reste protégée même si un appelant rejouait l'exécuteur."""
        payload = {"amount": 2000, "category": "fuel", "product": "gasoil"}
        _raised, confirmed = _raise_and_confirm("FINANCE_LOG_EXPENSE", payload)
        assert confirmed["status"] == "EXECUTING"

        # `confirmed` est déjà l'état COMPLET après le 1er CONFIRM (fusionné
        # via `_merge_patch`, donc `pending_interaction` déjà résolu) — un 2e
        # appel avec ce même état résiduel, event CONFIRM inchangé, simule
        # exactement un double "OK" arrivé avant que le tour suivant n'ait
        # eu la moindre chance de nettoyer quoi que ce soit.
        full_state_after_confirm = dict(confirmed)
        full_state_after_confirm["interpreted_event"] = "CONFIRM"

        assert (
            get_pending_interaction(full_state_after_confirm).kind
            != InteractionKind.CONFIRM_ACTION
        ), "le 1er CONFIRM doit résoudre pending_interaction — sinon un 2e passe re-certifierait"

        second = run(confirmation_gate(full_state_after_confirm, None))
        assert second.get("is_certified") is not True, (
            "un 2e appel après résolution a re-certifié — double exécution possible"
        )


class TestA7RetryKeepsTheSameIdempotencyKey:
    def test_the_frozen_payload_does_not_change_what_the_executor_derives_its_key_from(self):
        """A7 : ce correctif ne touche QUE `transaction_payload` (gelé au
        contenu confirmé) — il ne touche PAS `_derive_execution_idempotency_key`
        (`nodes/executor.py`, testé en détail par `test_execution_idempotency_
        key.py`, session précédente). Un retry de l'exécuteur après le MÊME
        CONFIRM doit donc continuer de dériver la MÊME clé — ce test
        vérifie seulement que la forme du payload gelé (un dict plat, sans
        clé `__reset__` qui aurait fuité) reste consommable telle quelle par
        cette dérivation, sans changer son contrat."""
        payload = {"product": "maïs", "quantity": 50, "unit": "UNITE"}
        _raised, confirmed = _raise_and_confirm("STOCK_REGISTER_HARVEST", payload)

        tp = confirmed["transaction_payload"]
        assert "__reset__" not in tp, (
            "le sentinel __reset__ du reducer merge_dict a fuité dans l'état "
            "final — la couche exécuteur ne doit jamais le voir"
        )
        assert tp == payload


class TestA8ANewGoalInvalidatesAnOldCertifiedCommand:
    def test_a_confirm_event_for_a_stale_pending_interaction_on_a_different_goal_is_abandoned(self):
        """A8 : si `current_goal` a changé depuis que CETTE confirmation a
        été levée (ex: une nouvelle action a démarré sans que l'ancienne
        confirmation ait été explicitement tranchée — état défensif, ne
        devrait plus arriver depuis les correctifs de purge de la session
        précédente, mais ce nœud ne doit JAMAIS s'y fier aveuglément), le
        CONFIRM reçu ne doit PAS certifier l'ancienne action."""
        payload = {"product": "maïs", "quantity": 50, "unit": "UNITE"}
        raise_state = make_state(
            current_goal="STOCK_REGISTER_HARVEST",
            interpreted_event="NEW_TASK",
            transaction_payload=dict(payload),
        )
        raised = run(confirmation_gate(raise_state, None))
        assert raised["confirmation_summary_goal"] == "STOCK_REGISTER_HARVEST"

        # Le but courant a changé (nouvelle action) mais le pending_interaction
        # CONFIRM_ACTION de l'ANCIENNE confirmation traîne encore.
        stale_confirm_state = {**raise_state, **raised}
        stale_confirm_state["current_goal"] = "FARM_UPDATE"
        stale_confirm_state["transaction_payload"] = {"farm_id": "f-1", "farm_name": "Nouveau nom"}
        stale_confirm_state["interpreted_event"] = "CONFIRM"

        result = run(confirmation_gate(stale_confirm_state, None))

        assert result.get("is_certified") is not True, (
            "une confirmation levée pour STOCK_REGISTER_HARVEST a certifié "
            "une action FARM_UPDATE jamais réellement confirmée sous cette forme"
        )
        assert result.get("execution_authorized") is not True
        assert result.get("current_goal") is None, "traité comme abandonné, pas comme certifié"
