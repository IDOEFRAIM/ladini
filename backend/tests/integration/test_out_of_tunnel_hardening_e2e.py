"""Étape 9D — replays + hardening (2026-10-01). Clôture du chantier "ambiguïté
hors tunnel" (Étapes 9A/9B/9C) : ce fichier ne change AUCUN comportement, il
le REJOUE sur la chaîne RÉELle de nœuds (`input_interpreter -> cognitive_guard
-> goal_planner -> memory_update -> validator`, même méthode que
`test_active_slot_answer_priority_e2e.py`/`test_out_of_tunnel_intent_ambiguity_e2e.py`/
`test_intent_clarification_resolution_e2e.py`/
`test_sales_publish_cross_flow_state_leak.py`) pour prouver, sur des scénarios
MULTI-TOURS réels, les invariants déjà implémentés.

Décision de périmètre (mandat §2/§14, audit préalable) : la réplique SALES
(`TestFullSalesReplay`) s'arrête à `validator` pour le tour 3 ("confirmation
prête" : `missing_fields=[]`) et ne rejoue PAS `confirmation_gate` pour
SALES_PUBLISH_PRODUCT — `_resolve_sales_draft_based_confirmation` écrit
RÉELLEMENT dans Postgres via `sales_publish_draft_store.insert`, en
contournant `StubRuntime.call_db` (donc impossible à mocker proprement sans
DB réelle ou monkeypatch profond du module `services.database`), exactement
le type de "dépendance sans rapport avec le lifecycle transactionnel audité"
que `test_sales_publish_cross_flow_state_leak.py` exclut déjà explicitement
pour CE MÊME goal (voir sa docstring). Le "ok -> publication réussie" réel
pour SALES_PUBLISH_PRODUCT est déjà couvert par
`test_commercial_pricing_vertical_slice.py` (ConversationHarness, assertions
sur `create_product`) — chantier Phase B1, sans rapport avec l'ambiguïté hors
tunnel. `TestFullStockReplay`, en revanche, VA jusqu'à `confirmation_gate`
inclus pour STOCK_REGISTER_HARVEST (tour "ok") : ce goal n'est PAS dans
`DRAFT_BASED_CONFIRMATION_GOALS` (`core/goals.py`), son chemin de
confirmation est générique et 100% synchrone/pur (aucun I/O), donc rejouable
sans dépendance étrangère — voir ce test pour le détail."""
from __future__ import annotations

import json
import typing
from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.core.goals import (
    DRAFT_BASED_CONFIRMATION_GOALS,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, _Completion, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        if reducer is None:
            new_state[key] = value
            continue
        new_state[key] = reducer(state.get(key), value)
    return new_state


def _fresh_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "user_phone": "+22670000099",
        "user_role": "PRODUCER",
        "extracted_entities": {},
        "working_memory": {},
        "current_goal": None,
        "transaction_payload": {},
    }
    state.update(overrides)
    return state


class _ScriptedLLM:
    """LLM scripté générique, réutilisé par tous les scénarios de ce fichier.

    `new_task_payloads` : liste consommée dans l'ordre, UNE entrée par appel
    au micro-prompt NEW_TASK (détecté via la présence de `candidate_goals`/
    `AMBIGUOUS` dans le prompt système — même heuristique que
    `test_intent_clarification_resolution_e2e.py::_AmbiguousThenClarificationReplyLLM`).
    `active_slot_payload` : réponse FIXE pour tout appel au micro-prompt
    ACTIVE_SLOT (détecté via le marqueur `DEVIATION`, même heuristique que
    `test_sales_publish_cross_flow_state_leak.py::_DeviationThenNewTaskLLM`) —
    un seul scénario de ce fichier a besoin de plus d'une forme de réponse
    ACTIVE_SLOT par test ; quand c'est le cas, un test dédié construit sa
    propre sous-classe plutôt que de complexifier ce script générique."""

    def __init__(
        self,
        new_task_payloads: Optional[List[Dict[str, Any]]] = None,
        active_slot_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._new_task_payloads = list(new_task_payloads or [])
        self._active_slot_payload = active_slot_payload or {
            "disposition": "UNKNOWN",
            "extracted_entities": {},
            "confidence": 0.0,
        }
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages") or []
        blob = json.dumps(messages)
        if "DEVIATION" in blob:
            payload = self._active_slot_payload
        elif self._new_task_payloads:
            payload = self._new_task_payloads.pop(0)
        else:
            payload = {"disposition": "UNKNOWN", "confidence": 0.0, "entities": {}}
        return _Completion(json.dumps(payload), model=kwargs.get("model"))


def _ambiguous_miel_payload() -> Dict[str, Any]:
    return {
        "disposition": "AMBIGUOUS",
        "candidate_goals": ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        "confidence": 0.6,
        "entities": {"product": "miel", "quantity": 90.0, "unit": "LITRE"},
    }


_BUSINESS_PAYLOAD_KEYS = ("product", "quantity", "unit", "price", "price_unit")


def _has_no_real_values(payload: Optional[Dict[str, Any]]) -> bool:
    """`transaction_payload` purgé (`{"__reset__": True}`) garde des clés
    structurelles (`role`, `intent`...) re-dérivées au re-merge — ce n'est
    PAS une fuite métier. Seuls les champs business (produit/quantité/
    unité/prix) comptent ici."""
    payload = payload or {}
    return not any(
        payload.get(key) not in (None, "", [], {}) for key in _BUSINESS_PAYLOAD_KEYS
    )


def _reject_payload() -> Dict[str, Any]:
    """Résolution d'une clarification par "annuler" : le micro-prompt NEW_TASK
    classe explicitement REJECT (aucun fast-path déterministe ne couvre
    "annuler" hors contexte CONFIRMATION/SELECTION — vérifié empiriquement en
    construisant ce fichier)."""
    return {"disposition": "REJECT", "confidence": 0.9, "entities": {}}


def _ambiguous_tomates_payload() -> Dict[str, Any]:
    return {
        "disposition": "AMBIGUOUS",
        "candidate_goals": ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        "confidence": 0.6,
        "entities": {"product": "tomates", "quantity": 50.0, "unit": "KG"},
    }


async def _run_turn(
    state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str
) -> Dict[str, Any]:
    """Un tour complet : interpreter -> cognitive_guard -> goal_planner ->
    memory_update -> validator -> post_response_cleanup.

    IMPORTANT (trouvé en construisant ce fichier) : `post_response_cleanup`
    est inclus ICI, contrairement à la version jumelle (sans ce dernier
    nœud) de `test_intent_clarification_resolution_e2e.py`/
    `test_active_slot_answer_priority_e2e.py`/
    `test_sales_publish_cross_flow_state_leak.py` — ces fichiers-là ne
    rejouent JAMAIS plus de 2 tours consécutifs dans le même état, donc le
    gap ne les affecte jamais en pratique. Ce fichier-ci enchaîne
    systématiquement 3+ tours réels : sans `post_response_cleanup`,
    `extracted_entities` (canal `merge_dict`, voir `agents/reducers.py` —
    "`new == {}` préserve `old`, jamais un reset silencieux") ne redevient
    JAMAIS vide entre deux tours simulés, alors que le graphe RÉEL le
    réinitialise explicitement à chaque fin de tour
    (`nodes/cleanup.py::_EPHEMERAL_MERGE_DICT_FIELDS`). Sans ce nœud, un
    tour sans nouvelle extraction (ex: "annuler", "vendre") relit en
    silence les entités extraites 2 tours plus tôt — contamination
    purement due à la frontière de tour manquante dans le harnais de test,
    jamais reproductible dans le graphe compilé réel. Trouvé en écrivant
    ce chantier (§4/§7), corrigé ICI plutôt que dans les fichiers déjà
    mergés (hors périmètre, aucune régression constatée là où ils
    s'arrêtent avant que ça compte)."""
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    cg = await cognitive_guard(state, runtime)
    state = apply_patch(state, cg)

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    # Le DomainRouter réel continue, DANS LE MÊME TOUR, vers `confirmation_gate`
    # dès que `validator` déclare le goal complet (`missing_fields == []`) —
    # `post_response_cleanup` suppose cet enchaînement déjà fait (son
    # `keep_confirmation_channel`/`keep_field_channel` ne "voit" le canal de
    # confirmation que si `confirmation_gate` a déjà tourné CE tour). Sans ce
    # relais, `post_response_cleanup` effaçait `current_goal` juste après
    # qu'un tour l'ait complété — trouvé en construisant ce fichier (§3).
    # Exclusion délibérée des goals `DRAFT_BASED_CONFIRMATION_GOALS` (SALES/
    # PROCUREMENT) : voir docstring de module, dépendance Postgres directe.
    goal = str(state.get("current_goal") or "").upper()
    missing = state.get("missing_fields") or []
    ready_for_confirmation = bool(state.get("current_goal")) and not missing
    draft_based_and_ready = ready_for_confirmation and goal in DRAFT_BASED_CONFIRMATION_GOALS

    if ready_for_confirmation and not draft_based_and_ready:
        cg2 = await confirmation_gate(state, runtime)
        state = apply_patch(state, cg2)

    if not draft_based_and_ready:
        cleanup = await post_response_cleanup(state, runtime)
        state = apply_patch(state, cleanup)

    return state


# ──────────────────────────────────────────────────────────────────────────
# §2 — Replay E2E SALES complet
# ──────────────────────────────────────────────────────────────────────────


class TestFullSalesReplay:
    def test_turns_1_to_3_sales(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[_ambiguous_miel_payload()],
            active_slot_payload={
                "disposition": "ANSWER",
                "extracted_entities": {"price": 700.0, "price_unit": "LITRE"},
                "confidence": 0.95,
            },
        )

        # TURN 1 — "j'ai 90 L de miel" : aucun tunnel actif -> AMBIGUOUS,
        # CLARIFY_INTENT, faits conservés, aucun draft métier, aucun side effect.
        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert state.get("current_goal") is None
        assert state.get("sales_publish_draft") is None
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        assert pending["target"]["facts"]["product"] == "miel"
        assert pending["target"]["facts"]["quantity"] == 90.0
        assert runtime.calls == []  # aucun side effect MCP

        # TURN 2 — "vendre" : résolution lexicale (aucun appel LLM requis),
        # current_goal=SALES_PUBLISH_PRODUCT, produit/quantité repris,
        # pending consommé, prochaine question = PRICE.
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0
        assert payload.get("unit") == "LITRE"
        assert set(state.get("missing_fields") or []) == {"price"}
        assert (state.get("pending_interaction") or {}).get("kind") == "ENTER_FIELD"

        # TURN 3 — "700 FCFA par litre" : prix correctement enregistré, plus
        # aucun champ manquant (état prêt pour confirmation).
        state = run(
            _run_turn(state, interpreter, runtime, text="700 FCFA par litre")
        )
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0
        assert payload.get("price") == 700.0
        assert state.get("missing_fields") in ([], None)
        # Le flot SALES réel route maintenant vers confirmation_gate (DomainRouter) —
        # hors périmètre ici (voir docstring de module) ; la preuve "faits
        # corrects + plus rien à redemander" est ce que ce chantier audite.


# ──────────────────────────────────────────────────────────────────────────
# §3 — Replay E2E STOCK complet (va jusqu'à confirmation_gate : voir docstring)
# ──────────────────────────────────────────────────────────────────────────


class TestFullStockReplay:
    def test_turns_1_to_4_stock(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[_ambiguous_tomates_payload()],
            active_slot_payload={
                "disposition": "CONFIRM",
                "extracted_entities": {},
                "confidence": 0.95,
            },
        )

        # TURN 1
        state = run(
            _run_turn(state, interpreter, runtime, text="j'ai 50 kg de tomates")
        )
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        assert pending["target"]["facts"]["product"] == "tomates"
        assert pending["target"]["facts"]["quantity"] == 50.0

        # TURN 2 — "stock" : résolution lexicale, STOCK_REGISTER_HARVEST,
        # faits repris sans re-demander produit/quantité, aucun fait SALES
        # (ex. price) injecté.
        state = run(_run_turn(state, interpreter, runtime, text="stock"))
        assert state.get("current_goal") == "STOCK_REGISTER_HARVEST"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "tomates"
        assert payload.get("quantity") == 50.0
        assert payload.get("unit") == "KG"
        assert "price" not in {k for k, v in payload.items() if v not in (None, "")}

        # Avance jusqu'à confirmation si un champ reste à fournir (date de
        # récolte ou équivalent) ; sinon le draft est déjà complet. `_run_turn`
        # relaie automatiquement vers `confirmation_gate` DANS LE MÊME TOUR dès
        # que `missing_fields` devient vide pour un goal non draft-based (voir
        # sa docstring) — STOCK_REGISTER_HARVEST qualifie, donc ce tour-là pose
        # déjà la confirmation (summary + pending=CONFIRM_ACTION), exactement
        # comme le ferait le DomainRouter réel ; "ok" reste un tour SÉPARÉ
        # (event=CONFIRM) pour réellement exécuter.
        missing = state.get("missing_fields") or []
        if missing:
            state = run(
                _run_turn(state, interpreter, runtime, text="aujourd'hui")
            )
            missing = state.get("missing_fields") or []
        assert missing == []
        assert (state.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION"

        # TURN "ok" : confirmation réelle, chemin GÉNÉRIQUE (STOCK_REGISTER_
        # HARVEST n'est pas dans DRAFT_BASED_CONFIRMATION_GOALS) — 100%
        # synchrone, aucune dépendance Postgres/MCP, rejouable en toute
        # sécurité (voir docstring de module).
        # NB : `execution_authorized` N'EST PAS vérifié APRÈS ce tour complet —
        # c'est un signal strictement intra-tour, consommé par `mcp_tool_executor`
        # (hors périmètre ici, voir docstring de module) ENTRE `confirmation_gate`
        # et `post_response_cleanup` dans le graphe réel ; `post_response_cleanup`
        # le remet à `False` en fin de tour que ce signal ait été consommé ou
        # non (`_EPHEMERAL_REPLACE_FIELDS`, `nodes/cleanup.py`) — c'est `status`/
        # `pending_interaction`, qui SURVIVENT au tour, qui prouvent la
        # certification ici.
        runtime.llm = _ScriptedLLM(
            active_slot_payload={"disposition": "CONFIRM", "extracted_entities": {}, "confidence": 0.95}
        )
        state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert state.get("status") == "EXECUTING"
        assert state.get("pending_interaction") in (None, {})


# ──────────────────────────────────────────────────────────────────────────
# §4 — Replay SALES puis STOCK : contamination critique
# ──────────────────────────────────────────────────────────────────────────


class TestSalesStockNoContamination:
    """Mandat §4. Décision de périmètre (audit empirique, voir docstring de
    module) : "jusqu'à publication réussie (ok)" n'est PAS rejouable ici pour
    SALES_PUBLISH_PRODUCT sans dépendance Postgres (voir `TestFullSalesReplay`).
    On ferme donc le tunnel SALES par la voie RÉELLE, déjà prouvée par
    `TestCancelDuringSalesAfterClarification` (§7) — "annuler" PENDANT un
    ENTER_FIELD actif (prix pas encore donné) déclenche réellement la RÈGLE 1
    de `goal_planner` (`event=="REJECT"`, `expected_input != "CONFIRMATION"`)
    qui efface `current_goal`/purge `transaction_payload` — plutôt qu'un état
    fabriqué à la main. Ce qui compte pour CE test (la non-contamination
    produit/quantité d'un tunnel fermé vers une déclaration fraîche) est
    identique quelle que soit la façon dont le tunnel s'est fermé."""

    def test_fresh_stock_after_closed_sales_never_leaks_honey_facts(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[_ambiguous_miel_payload()],
            active_slot_payload={
                "disposition": "REJECT",
                "extracted_entities": {},
                "confidence": 0.9,
            },
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0

        state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert state.get("current_goal") in (None, "")
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") in (None, "")

        # Rupture nette : nouvelle déclaration hors tunnel, produit différent.
        # Nouvelle instance de LLM scripté (même principe que
        # `TestCancelDuringClarification`/`TestCancelDuringSalesAfterClarification`) :
        # la file précédente est épuisée volontairement, une déclaration hors
        # tunnel fraîche mérite son propre script, jamais une queue partagée
        # qui pourrait fuiter un payload destiné à un autre tour.
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_tomates_payload()])
        state = run(
            _run_turn(state, interpreter, runtime, text="j'ai 50 kg de tomates")
        )
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        facts = pending["target"]["facts"]
        assert facts.get("product") == "tomates"
        assert facts.get("quantity") == 50.0
        assert "price" not in facts
        assert facts.get("quantity") != 90.0

        state = run(_run_turn(state, interpreter, runtime, text="stock"))
        assert state.get("current_goal") == "STOCK_REGISTER_HARVEST"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "tomates"
        assert payload.get("quantity") == 50.0
        assert payload.get("price") in (None, "")
        assert payload.get("unit") == "KG"
        stable = state.get("stable_entities") or {}
        assert stable.get("product") != "miel"


# ──────────────────────────────────────────────────────────────────────────
# §5 — Matrice de contexte : même phrase, 5 contextes
# ──────────────────────────────────────────────────────────────────────────


class TestContextMatrixSamePhrase:
    _TEXT = "j'ai 90 L de miel"

    def test_a_no_tunnel_is_ambiguous(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text=self._TEXT))
        assert (state.get("pending_interaction") or {}).get("kind") == "CLARIFY_INTENT"
        assert state.get("current_goal") is None

    def test_b_sales_active_quantity_expected_is_answer(self):
        state = _fresh_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "miel"},
        )
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            active_slot_payload={
                "disposition": "ANSWER",
                "extracted_entities": {"quantity": 90.0, "unit": "LITRE"},
                "confidence": 0.9,
            }
        )
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state.update(
            set_pending_interaction(
                InteractionKind.ENTER_FIELD, goal="SALES_PUBLISH_PRODUCT", field_name="quantity"
            )
        )
        state = run(_run_turn(state, interpreter, runtime, text=self._TEXT))
        # Réponse du tunnel actif : jamais une interruption/ambiguïté hors tunnel.
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0
        assert (state.get("pending_interaction") or {}).get("kind") != "CLARIFY_INTENT"

    def test_c_stock_active_quantity_expected_is_answer(self):
        state = _fresh_state(
            current_goal="STOCK_REGISTER_HARVEST",
            transaction_payload={"product": "miel"},
        )
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            active_slot_payload={
                "disposition": "ANSWER",
                "extracted_entities": {"quantity": 90.0, "unit": "LITRE"},
                "confidence": 0.9,
            }
        )
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state.update(
            set_pending_interaction(
                InteractionKind.ENTER_FIELD, goal="STOCK_REGISTER_HARVEST", field_name="quantity"
            )
        )
        state = run(_run_turn(state, interpreter, runtime, text=self._TEXT))
        assert state.get("current_goal") == "STOCK_REGISTER_HARVEST"
        payload = state.get("transaction_payload") or {}
        assert payload.get("quantity") == 90.0
        assert (state.get("pending_interaction") or {}).get("kind") != "CLARIFY_INTENT"

    def test_e_post_cancel_is_fresh_ambiguity(self):
        """Invariant critique (mandat §5.E) : après annulation complète, la
        MÊME phrase redevient une ambiguïté neuve — aucun état résiduel ne la
        fait retomber dans un ancien tunnel."""
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                _ambiguous_miel_payload(),
                _reject_payload(),
                _ambiguous_miel_payload(),
            ]
        )

        state = run(_run_turn(state, interpreter, runtime, text=self._TEXT))
        assert (state.get("pending_interaction") or {}).get("kind") == "CLARIFY_INTENT"

        state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert state.get("pending_interaction") in (None, {})
        assert state.get("current_goal") is None

        state = run(_run_turn(state, interpreter, runtime, text=self._TEXT))
        assert (state.get("pending_interaction") or {}).get("kind") == "CLARIFY_INTENT"
        assert state.get("current_goal") is None


# ──────────────────────────────────────────────────────────────────────────
# §6 — CANCEL pendant clarification
# ──────────────────────────────────────────────────────────────────────────


class TestCancelDuringClarification:
    def test_cancel_clears_pending_then_fresh_declaration_is_clean(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                _ambiguous_miel_payload(),
                _reject_payload(),
                _ambiguous_tomates_payload(),
            ]
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert (state.get("pending_interaction") or {}).get("kind") == "CLARIFY_INTENT"

        state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert state.get("pending_interaction") in (None, {})
        assert state.get("current_goal") is None
        assert _has_no_real_values(state.get("transaction_payload"))

        state = run(
            _run_turn(state, interpreter, runtime, text="j'ai 50 kg de tomates")
        )
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        facts = pending["target"]["facts"]
        assert facts.get("product") == "tomates"
        assert facts.get("quantity") != 90.0


# ──────────────────────────────────────────────────────────────────────────
# §7 — CANCEL pendant SALES après clarification
# ──────────────────────────────────────────────────────────────────────────


class TestCancelDuringSalesAfterClarification:
    def test_cancel_mid_sales_after_resolution_then_fresh_declaration_is_clean(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[_ambiguous_miel_payload()],
            active_slot_payload={
                "disposition": "REJECT",
                "extracted_entities": {},
                "confidence": 0.9,
            },
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        assert (state.get("pending_interaction") or {}).get("field") == "price"

        # "annuler" pendant un ENTER_FIELD actif (pas une clarification) —
        # chemin d'abandon du tunnel existant, déjà couvert par le repli
        # générique de `goal_planner` (RÈGLE 1) ; on vérifie seulement
        # l'intégration ici (mandat §7 : "pas de données mortes").
        state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert state.get("current_goal") in (None, "")
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") in (None, "")

        runtime.llm = _ScriptedLLM(new_task_payloads=[
            {
                "disposition": "AMBIGUOUS",
                "candidate_goals": ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
                "confidence": 0.6,
                "entities": {"product": "lait", "quantity": 30.0, "unit": "LITRE"},
            }
        ])
        state = run(
            _run_turn(state, interpreter, runtime, text="j'ai 30 L de lait")
        )
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        facts = pending["target"]["facts"]
        assert facts.get("product") == "lait"
        assert facts.get("quantity") == 30.0


# ──────────────────────────────────────────────────────────────────────────
# §9 — Breakout pendant clarification (réplique multi-tours explicite)
# ──────────────────────────────────────────────────────────────────────────


class TestBreakoutDuringClarificationReplay:
    def test_unrelated_new_task_breaks_out_cleanly(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                _ambiguous_miel_payload(),
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_LIST_ORDERS",
                    "confidence": 0.9,
                    "entities": {},
                },
            ]
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert (state.get("pending_interaction") or {}).get("kind") == "CLARIFY_INTENT"

        state = run(_run_turn(state, interpreter, runtime, text="voir mes commandes"))
        assert state.get("current_goal") not in ("SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST")
        assert (state.get("pending_interaction") or {}).get("kind") != "CLARIFY_INTENT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") != "miel"


# ──────────────────────────────────────────────────────────────────────────
# §10 — Réponse ambiguë pendant clarification ("oui"/"d'accord")
# ──────────────────────────────────────────────────────────────────────────


class TestAmbiguousReplyDuringClarification:
    def test_oui_does_not_select_a_goal_and_reasks(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                _ambiguous_miel_payload(),
                {"disposition": "UNKNOWN", "confidence": 0.0, "entities": {}},
            ]
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="oui"))

        assert state.get("current_goal") is None
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        assert pending["target"]["facts"]["product"] == "miel"
        assert state.get("final_response")


# ──────────────────────────────────────────────────────────────────────────
# §11/§12 — Réponse enrichie (quantité corrigée / prix inclus)
# ──────────────────────────────────────────────────────────────────────────


class TestEnrichedClarificationReplies:
    def test_corrected_quantity_is_used_not_the_original(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(
            _run_turn(
                state, interpreter, runtime, text="je veux en vendre seulement 50 L"
            )
        )
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 50.0

    def test_enriched_reply_with_price_is_not_discarded(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))

        # Réponse LLM réaliste pour ce tour (validée empiriquement en
        # construisant ce fichier) : le repli déterministe de l'interpréteur
        # ajoute TOUJOURS un candidat quantité=700/unit=LITRE à partir du
        # motif "700 ... litre", QUELLE QUE SOIT la réponse LLM scriptée —
        # c'est exactement le garde ajouté à `_merge_clarification_facts`
        # (cognitive.py, mandat §12) qui est sous test ici.
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.85,
                    "entities": {"price": 700.0, "price_unit": "LITRE"},
                }
            ]
        )
        state = run(
            _run_turn(
                state,
                interpreter,
                runtime,
                text="je veux les vendre à 700 FCFA par litre",
            )
        )
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0
        assert payload.get("price") == 700.0
        assert "price" not in (state.get("missing_fields") or [])


# ──────────────────────────────────────────────────────────────────────────
# §13 — Product conflict (réplique multi-tours explicite)
# ──────────────────────────────────────────────────────────────────────────


class TestProductConflictReplay:
    def test_switching_product_never_carries_over_the_old_quantity(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))

        runtime.llm = _ScriptedLLM(
            active_slot_payload={
                "disposition": "NEW_TASK",
                "extracted_entities": {},
                "confidence": 0.3,
            },
            new_task_payloads=[
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.8,
                    "entities": {"product": "lait"},
                }
            ],
        )
        state = run(
            _run_turn(state, interpreter, runtime, text="je veux vendre le lait")
        )
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "lait"
        assert payload.get("quantity") != 90.0
        assert not (payload.get("quantity") and payload.get("product") == "lait" and payload.get("quantity") == 90.0)


# ──────────────────────────────────────────────────────────────────────────
# §15/§16 — LLM indisponible
# ──────────────────────────────────────────────────────────────────────────


class _RaisingLLM:
    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        raise RuntimeError("LLM indisponible (simulation §15/§16)")


class TestLLMUnavailable:
    def test_hors_tunnel_llm_down_never_auto_picks_a_goal(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _RaisingLLM()

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert state.get("current_goal") is None
        assert state.get("sales_publish_draft") is None

    def test_clarification_vendre_resolves_deterministically_without_llm(self):
        """§16.A : "vendre" est résolu par le radical lexical — AUCUN appel
        LLM n'est requis pour CHOISIR le goal pendant une clarification
        active, donc le LLM peut être totalement indisponible à cette
        étape précise sans dégrader la résolution."""
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))

        runtime.llm = _RaisingLLM()
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0


# ──────────────────────────────────────────────────────────────────────────
# §19 — Pas de contamination des pending facts (invariant générique)
# ──────────────────────────────────────────────────────────────────────────


class TestNoPendingFactLeakage:
    def test_pending_cleared_after_cancel(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[_ambiguous_miel_payload(), _reject_payload()]
        )

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert state.get("pending_interaction") in (None, {})

    def test_pending_cleared_after_resolution(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(new_task_payloads=[_ambiguous_miel_payload()])

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") != "CLARIFY_INTENT"

    def test_pending_cleared_after_breakout(self):
        state = _fresh_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _ScriptedLLM(
            new_task_payloads=[
                _ambiguous_miel_payload(),
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_LIST_ORDERS",
                    "confidence": 0.9,
                    "entities": {},
                },
            ]
        )
        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        state = run(_run_turn(state, interpreter, runtime, text="voir mes commandes"))
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") != "CLARIFY_INTENT"
