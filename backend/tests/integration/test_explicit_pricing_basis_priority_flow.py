"""Replays réalistes (Étape 4, 2026-09-30) sur la VRAIE chaîne de nœuds
(input_interpreter -> cognitive_guard -> goal_planner -> memory_update ->
validator) — même méthode que les étapes 1-3.

Replay 1 (§14 du mandat) : product=miel, "50 pots de 4 litre" (déjà verrouillé
par l'Étape 3) puis, la question "Quel est votre prix par litre ?" posée,
"le pot de 4 litre coute 2750 fcfa" -> PER_PACKAGE, jamais 2750/L.

Replay 2 (§15) : "500 FCFA le sachet" (taille inconnue) -> clarification
ciblée, aucune publication ; puis "2 L" résout la taille sans perdre le prix
ni la disponibilité.

Replay 3 (§16) : multi-tier explicite bat le contexte de question — préserve
le comportement déjà mergé (Étape "négociation"/paliers), non touché ici.
"""
from __future__ import annotations

import typing
from typing import Any, Dict

from ladini.domain.commercial_offer_flow import CommercialQuestion
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run

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
        new_state[key] = value if reducer is None else reducer(state.get(key), value)
    return new_state


async def _run_turn(state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str) -> Dict[str, Any]:
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    cg = await cognitive_guard(state, runtime)
    state = apply_patch(state, cg)

    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    return state


def _price_question_state(
    *, product: str, payload_extra: Dict[str, Any], expected_basis_unit: str = "LITRE"
) -> Dict[str, Any]:
    """État seedé directement à "la question de prix par unité de base vient
    d'être posée" — même convention que les étapes 1-3 (le nœud qui pose
    normalement cette question, confirmation_gate/context_resolver, n'est
    délibérément pas inclus dans cette chaîne simplifiée, voir leur propre
    précédent)."""
    payload = {"product": product, **payload_extra}
    target = CommercialQuestion(
        requested_field="price", expected_basis_unit=expected_basis_unit
    ).to_target()
    state = make_state(
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload=payload,
        user_role="PRODUCER",
        user_phone="+22670000099",
        sales_publish_draft=None,
    )
    state.update(
        set_pending_interaction(
            InteractionKind.ENTER_FIELD,
            goal="SALES_PUBLISH_PRODUCT",
            field_name="price",
            target=target,
        )
    )
    return state


class TestReplay1PotDe4LitreNeverPerLitre:
    def test_le_pot_de_4_litre_coute_2750_is_per_package(self):
        # État post-Étape-3 : "j'ai 50 pots de 4 litre" déjà résolu (count/label/
        # size/unit + commercial_offer avec availability=200L) — voir
        # test_package_count_size_flow.py pour la preuve de CE tour lui-même,
        # non reproduite ici (hors périmètre de cette étape).
        offer_after_step3 = {
            "schema": 1,
            "product": "miel",
            "commercial_quantity": {"amount": 200.0, "unit": "LITRE", "source": "DOMAIN_DERIVED"},
            "inventory_quantity": {"amount": 200.0, "unit": "LITRE", "source": "DOMAIN_DERIVED"},
            "pricing": None,
            "package": {
                "package_type": "POT", "content_amount": 4.0, "content_unit": "LITRE",
                "count": 50, "status": "KNOWN", "source": "USER_EXPLICIT",
            },
            "normalized": None,
        }
        state = _price_question_state(
            product="miel",
            payload_extra={
                "package_count": 50, "package_label": "POT",
                "package_size": 4.0, "package_unit": "LITRE",
                "commercial_offer": offer_after_step3,
            },
        )
        runtime = StubRuntime()
        # "le pot de 4 litre coute 2750 fcfa" est IMPUR (mot "pot"/"coute" non
        # reconnus, Étape 1) -> le fast-path s'abstient, le micro-prompt
        # ACTIVE_SLOT est consulté pour extraire price=2750 (montant SEUL — le
        # texte brut, lui, atteint quand même `commercial_offer_flow.py` via
        # `state["normalized_text"]`, c'est LUI qui décide de la base).
        runtime.llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"price": 2750.0}, "confidence": 0.9}
        )
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="le pot de 4 litre coute 2750 fcfa"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None, f"aucune commercial_offer : payload={payload!r}"
        assert offer["pricing"]["basis"] == "PER_PACKAGE", offer["pricing"]
        assert offer["pricing"]["amount"] == 2750.0
        assert offer["package"]["package_type"] == "POT"
        assert offer["package"]["content_amount"] == 4.0
        assert offer["package"]["content_unit"] == "LITRE"
        # Interdiction centrale du mandat.
        assert offer["pricing"]["basis"] != "PER_BASE_UNIT"
        # Disponibilité de l'Étape 3 préservée, jamais recalculée en 2750×...
        assert offer["inventory_quantity"]["amount"] == 200.0

    def test_control_bare_500_with_question_context_stays_per_base_unit(self):
        # « 500 » nu (aucune unité/devise adjacente) est un cas GENUINEMENT
        # ambigu pour le moteur déterministe (voir `interpreter/routing.py`,
        # `_unambiguous_single`) — il défère au LLM quand celui-ci est
        # disponible, comportement PRÉ-EXISTANT et inchangé par cette étape
        # (voir `tests/interpreter/test_extraction_and_llm_primacy.py::
        # test_ambiguous_bare_number_is_deferred_to_llm`). C'est ensuite le
        # TEXTE BRUT du tour, relu par `commercial_offer_flow.py` via
        # `state["normalized_text"]`, qui décide réellement de la base — pas
        # l'extraction LLM du montant seul.
        state = _price_question_state(product="miel", payload_extra={})
        runtime = StubRuntime()
        runtime.llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"price": 500.0}, "confidence": 0.9}
        )
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="500"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None
        assert offer["pricing"]["basis"] == "PER_BASE_UNIT"
        assert offer["pricing"]["basis_unit"] == "LITRE"
        assert offer["pricing"]["amount"] == 500.0


class TestReplay2PackageWithoutSizeThenSizeArrives:
    def test_500_le_sachet_size_unknown_no_premature_publication(self):
        state = _price_question_state(
            product="lait",
            payload_extra={},
            expected_basis_unit="LITRE",
        )
        state["transaction_payload"] = {
            **state["transaction_payload"],
            "quantity": 60.0,
            "unit": "LITRE",
        }
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="500 FCFA le sachet"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["package"]["package_type"] == "SACHET"
        assert offer["package"]["content_amount"] is None, "aucune taille inventée"
        assert offer["pricing"]["basis"] != "PER_BASE_UNIT"
        # Aucune publication prématurée : la disponibilité (60L) survit, la
        # taille manquante bloque la validation (INCOMPLETE côté domaine).
        assert offer["inventory_quantity"]["amount"] == 60.0
        draft = state.get("sales_publish_draft")
        assert draft is None or draft.get("status") not in ("EXECUTING", "PUBLISHED")

    def test_then_2L_resolves_the_size_price_and_availability_survive(self):
        # État après "500 FCFA le sachet" : basis connu, taille inconnue.
        offer_after_price = {
            "schema": 1,
            "product": "lait",
            "commercial_quantity": {"amount": 60.0, "unit": "LITRE", "source": "USER_EXPLICIT"},
            "inventory_quantity": {"amount": 60.0, "unit": "LITRE", "source": "USER_EXPLICIT"},
            "pricing": {
                "amount": 500.0, "basis": "PER_PACKAGE", "basis_unit": None, "currency": "FCFA",
                "source": "USER_EXPLICIT", "basis_source": "USER_EXPLICIT",
            },
            "package": {
                "package_type": "SACHET", "content_amount": None, "content_unit": "LITRE",
                "status": "UNKNOWN", "source": "UNKNOWN",
            },
            "normalized": None,
        }
        target = CommercialQuestion(
            requested_field="package_size", package_type="SACHET", content_unit="LITRE"
        ).to_target()
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={
                "product": "lait", "quantity": 60.0, "unit": "LITRE",
                "price": 500.0, "commercial_offer": offer_after_price,
            },
            user_role="PRODUCER",
            user_phone="+22670000099",
        )
        state.update(
            set_pending_interaction(
                InteractionKind.ENTER_FIELD, goal="SALES_PUBLISH_PRODUCT",
                field_name="package_size", target=target,
            )
        )
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="2 L"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None
        assert offer["package"]["content_amount"] == 2.0
        assert offer["package"]["content_unit"] == "LITRE"
        assert offer["pricing"]["amount"] == 500.0
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["inventory_quantity"]["amount"] == 60.0


class TestReplay3MultiTierBeatsQuestionContext:
    def test_multi_tier_explicit_beats_question_context_litre(self):
        """§7/§16 : préserve le comportement multi-tier déjà mergé — non
        refactoré ici. Le texte multi-tier passe par le fast-path déterministe
        de paliers (`fast_path_deterministic_pricing_tiers`,
        `interpreter/routing.py`, inchangé), qui produit `pricing_tiers`
        directement ; `validator.py` bascule alors sur `evaluate_sales_tier_
        state`, jamais sur `build_commercial_offer_from_sales_state` (les deux
        chemins sont mutuellement exclusifs par construction, voir
        `nodes/validation.py` : `if ... and tier_result is None`) — un contexte
        de question PER_BASE_UNIT ne peut donc structurellement pas
        l'atteindre."""
        state = _price_question_state(product="miel", payload_extra={})
        state["transaction_payload"] = {
            **state["transaction_payload"],
            "quantity": 60.0,
            "unit": "LITRE",
        }
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(
            _run_turn(
                state, interpreter, runtime,
                text="bidon de 5 L a 700 FCFA et celui de 9 L a 1000 FCFA",
            )
        )

        payload = state.get("transaction_payload") or {}
        tiers = payload.get("pricing_tiers")
        assert tiers, f"aucun pricing_tiers produit : payload={payload!r}"
        assert len(tiers) == 2
        # Aucun prix par litre fabriqué à partir des paliers.
        assert payload.get("price") in (None, 0)
        assert payload.get("commercial_offer") is None
        # La disponibilité (60L) reste indépendante des tailles de paliers (I7).
        assert payload.get("quantity") == 60.0
