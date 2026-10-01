"""Étape 7 — continuité conversationnelle ANSWER vs NEW_TASK (2026-09-30).

Incident-type audité et reproduit :

    User: "je veux vendre mon miel"
    Agent: "Quelle quantité avez-vous ?"     (current_goal=SALES_PUBLISH_PRODUCT,
                                               pending=ENTER_FIELD quantity)
    User: "j'ai 90 L"

Trace (voir le rapport d'audit) : `_interpret_fast_path` résout DÉJÀ ce cas
précis via `is_pure_numeric_answer` (durci le 2026-09-30) — ce fichier ne
reverrouille donc PAS ce chemin (déjà couvert ailleurs), il verrouille le
filet de sécurité de `cognitive_guard` pour tout message qui échappe au
fast-path (réponse phrastique) et que l'interpréteur/le classifieur NEW_TASK
étiquette malgré tout, à tort, avec une intention concurrente à confiance
élevée (ex: STOCK_REGISTER_HARVEST) — root cause exacte : avant ce correctif,
rien dans `cognitive_guard` ne vérifiait si les ENTITÉS EXTRAITES du message
satisfaisaient déjà le slot attendu avant d'autoriser l'interruption.

Ces tests appellent `cognitive_guard` DIRECTEMENT avec un
`interpreted_event`/`detected_intent`/`extracted_entities` déjà posés —
c'est exactement la frontière que ce nœud arbitre (voir sa docstring :
« PROPRIÉTAIRE UNIQUE de la décision de transition du tour ») — peu importe
quelle couche en amont (fast-path, micro-prompt ACTIVE_SLOT, classifieur
NEW_TASK legacy) a produit ce triplet."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import make_state, run


def _misclassified_new_task(
    *,
    current_goal: str,
    detected_intent: str,
    extracted_entities: dict,
    expected_input: str,
    confidence: float = 0.95,
    text: str = "",
):
    return make_state(
        normalized_text=text,
        current_goal=current_goal,
        interpreted_event="NEW_TASK",
        detected_intent=detected_intent,
        interpreter_confidence=confidence,
        expected_input=expected_input,
        extracted_entities=extracted_entities,
        transaction_payload={"product": "miel"},
    )


# =====================================================================
# §4 — CAS CRITIQUE QUANTITY (tests A-D du mandat)
# =====================================================================


class TestQuantitySlotAnswerPriority:
    def test_A_plain_quantity_with_unit_is_answer(self):
        # "90 L" — déjà couvert par le fast-path (non re-testé ici), mais le
        # filet de cognitive_guard doit rester cohérent si jamais ce triplet
        # lui parvient malgré tout (ex: via le classifieur legacy).
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="90 L",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert result["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    def test_B_j_ai_90_l_is_answer_never_stock_never_new_task(self):
        """Le scénario exact du mandat (§"Comportement interdit")."""
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="j'ai 90 L",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert result["interpreted_event"] != "INTERRUPTION"
        assert result["cognitive_decision"]["action"] != "INTERRUPT_ACTIVE_GOAL"

    def test_C_j_ai_90_litres_is_answer(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="j'ai 90 litres",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"

    def test_D_90_l_disponibles_is_answer(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="90 L disponibles",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"


# =====================================================================
# §5/§6 — produit répété / produit différent explicite (tests E-F)
# =====================================================================


class TestProductRepeatedOrConflicting:
    def test_E_product_repeated_stays_answer_of_the_active_flow(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE", "product": "miel"},
            expected_input="QUANTITY",
            text="j'ai 90 L de miel",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"

    def test_F_conflicting_product_is_not_a_cognitive_guard_concern(self):
        """Mandat §6 : un produit EXPLICITEMENT différent ("miel" alors que
        product=lait est déjà connu) n'est pas arbitré ici — `cognitive_guard`
        laisse passer l'ANSWER (les entités quantity/unit satisfont bien le
        slot), et c'est la logique de conflit produit DÉJÀ EXISTANTE
        (`nodes/memory.py::_apply_slot`, branche "product") qui refuse
        ensuite d'attacher silencieusement 90 L au lait — non dupliquée ici
        (mandat : "ne crée pas une nouvelle logique parallèle")."""
        state = make_state(
            normalized_text="j'ai 90 L de miel",
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="STOCK_REGISTER_HARVEST",
            interpreter_confidence=0.95,
            expected_input="QUANTITY",
            extracted_entities={"quantity": 90.0, "unit": "LITRE", "product": "miel"},
            transaction_payload={"product": "lait"},
        )
        result = run(cognitive_guard(state, None))
        # cognitive_guard ne bascule jamais vers STOCK/NEW_TASK pour ce
        # message — le conflit produit reste entièrement de la responsabilité
        # de memory.py, hors périmètre de ce nœud.
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"


# =====================================================================
# §7 — CAS PRICE (test G)
# =====================================================================


class TestPriceSlotAnswerPriority:
    def test_G_je_vends_a_500_fcfa_is_answer(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"price": 500.0, "price_unit": "LITRE"},
            expected_input="PRICE",
            text="je vends à 500 FCFA",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"


# =====================================================================
# §9 — nouvelle intention forte (test I) : ne force PAS ANSWER sans entité
# compatible.
# =====================================================================


class TestStrongNewIntentStillInterrupts:
    def test_I_consult_my_orders_stays_a_real_new_task(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="SALES_LIST_ORDERS",
            extracted_entities={},
            expected_input="QUANTITY",
            text="je veux consulter mes commandes",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "INTERRUPTION"
        assert result["cognitive_decision"]["action"] == "INTERRUPT_ACTIVE_GOAL"

    def test_no_compatible_entity_never_suppresses_a_confident_competing_intent(self):
        """Même à confiance élevée, une intention concurrente SANS entité
        compatible avec le slot attendu reste interrompue — le nouveau garde
        ne force jamais ANSWER par défaut, seule la compatibilité d'entités
        le déclenche (mandat §11 : jamais de whitelist de phrases, jamais un
        biais généralisé "un tunnel actif gagne toujours")."""
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="BUYER_REQUEST",
            extracted_entities={"product": "tomates"},
            expected_input="QUANTITY",
            text="je veux acheter des tomates maintenant",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "INTERRUPTION"
        assert result["cognitive_decision"]["action"] == "INTERRUPT_ACTIVE_GOAL"


# =====================================================================
# §18 — non-régression STOCK : le garde est générique à tout tunnel, pas
# câblé uniquement sur SALES_PUBLISH_PRODUCT.
# =====================================================================


class TestGenericAcrossTunnelsNotJustSalesPublish:
    def test_stock_register_harvest_tunnel_keeps_its_own_quantity_answer(self):
        """Si le tunnel actif est DÉJÀ STOCK_REGISTER_HARVEST, "j'ai 90 L"
        doit rester un ANSWER de CE tunnel, même si l'interpréteur le
        reclasse par erreur vers un autre intent (ex: SALES_PUBLISH_PRODUCT)."""
        state = _misclassified_new_task(
            current_goal="STOCK_REGISTER_HARVEST",
            detected_intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="j'ai 90 L",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "STOCK_REGISTER_HARVEST"


# =====================================================================
# §19 — test d'invariant : même texte, contexte différent => classification
# potentiellement différente (et sans tunnel, comportement inchangé — hors
# périmètre, non testé ici, mandat §14).
# =====================================================================


class TestSameTextDifferentActiveContext:
    def test_same_text_resolves_to_whichever_tunnel_is_active(self):
        text = "j'ai 90 L"
        entities = {"quantity": 90.0, "unit": "LITRE"}

        sales_state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities=entities,
            expected_input="QUANTITY",
            text=text,
        )
        stock_state = _misclassified_new_task(
            current_goal="STOCK_REGISTER_HARVEST",
            detected_intent="SALES_PUBLISH_PRODUCT",
            extracted_entities=entities,
            expected_input="QUANTITY",
            text=text,
        )

        sales_result = run(cognitive_guard(sales_state, None))
        stock_result = run(cognitive_guard(stock_state, None))

        assert sales_result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert stock_result["detected_intent"] == "STOCK_REGISTER_HARVEST"
        assert sales_result["interpreted_event"] == "ANSWER"
        assert stock_result["interpreted_event"] == "ANSWER"


# =====================================================================
# §20 — observabilité : log structuré + champ tracé dans cognitive_decision.
# =====================================================================


class TestPackageSizeAlreadyHandledByAnExistingDeterministicGate:
    """Mandat §8 (test H) : "le sachet fait 2 L" répondant à une question
    package_size. Ce cas n'a PAS besoin d'un fix dans `cognitive_guard` —
    confirmé par audit : `interpreter/routing.py` (§"0.45 RÉPONSE À UNE
    QUESTION COMMERCIALE") résout déjà ce cas de façon ENTIÈREMENT
    déterministe (`domain/commercial_offer_flow.py::parse_package_content`),
    avant même que `cognitive_guard` ne soit atteint — verrouillé ici pour
    preuve, sans duplication de logique."""

    def test_le_sachet_fait_2_l_is_recognized_as_the_package_content(self):
        from ladini.domain.commercial_offer_flow import (
            FIELD_PACKAGE_SIZE,
            CommercialQuestion,
            parse_package_content,
        )

        question = CommercialQuestion(
            requested_field=FIELD_PACKAGE_SIZE,
            package_type="sachet",
            content_unit="LITRE",
        )
        result = parse_package_content("le sachet fait 2 L", question=question)
        assert result is not None
        amount, unit, _provenance = result
        assert amount == 2.0
        assert unit == "LITRE"


class TestLLMUnavailableSafety:
    """Mandat §13 : la sûreté ANSWER ne doit jamais dépendre de la
    disponibilité du LLM — vérifié directement sur `_interpret_fast_path`
    avec `llm_available=False` (le chemin réellement emprunté quand le LLM
    est indisponible, voir `_input_interpreter_impl`)."""

    def _quantity_state(self, text: str):
        state = {
            "normalized_text": text,
            "user_query": text,
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "working_memory": {"active_goal": "SALES_PUBLISH_PRODUCT"},
            "transaction_payload": {"product": "miel"},
        }
        state.update(
            set_pending_interaction(
                InteractionKind.ENTER_FIELD,
                goal="SALES_PUBLISH_PRODUCT",
                field_name="quantity",
            )
        )
        return state

    def test_J_plain_numeric_answer_resolved_without_any_llm_call(self):
        for text in ("90 L", "j'ai 90 L"):
            state = self._quantity_state(text)
            result = _interpret_fast_path(
                state, text, skip_numeric_shortcut=False, llm_available=False
            )
            assert result is not None, text
            assert result["interpreted_event"] == "ANSWER", text
            assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT", text
            assert result["extracted_entities"]["quantity"] == 90.0, text

    def test_K_unsafe_phrastic_message_without_llm_abstains_never_guesses_stock(self):
        """Le fast-path déterministe doit s'ABSTENIR (jamais deviner) sur un
        message qui porte du contenu métier non numérique — c'est alors
        `_input_interpreter_impl` (repli `llm is None`) qui produit un
        `UNKNOWN` sûr, jamais un `NEW_TASK`/`STOCK_REGISTER_HARVEST` par
        fallback lossy (comportement déjà existant, verrouillé ici)."""
        text = "j'ai environ 90 litres de miel en ce moment"
        state = self._quantity_state(text)
        result = _interpret_fast_path(
            state, text, skip_numeric_shortcut=False, llm_available=False
        )
        assert result is None, (
            f"le fast-path doit s'abstenir sur du contenu métier non numérique : {result!r}"
        )


class TestObservability:
    def test_suppressed_new_task_candidate_is_traced_in_the_decision(self):
        state = _misclassified_new_task(
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="STOCK_REGISTER_HARVEST",
            extracted_entities={"quantity": 90.0, "unit": "LITRE"},
            expected_input="QUANTITY",
            text="j'ai 90 L",
        )
        result = run(cognitive_guard(state, None))
        decision = result["cognitive_decision"]
        assert decision["reason"] == "slot_answer_priority_over_new_task"
        assert decision["suppressed_new_task_candidate"] == "STOCK_REGISTER_HARVEST"
