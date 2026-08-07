"""CHAOS 2 — Injection de données corrompues (Garbage In, Clean Rejection Out).

Prouve que les contrats Pydantic, le validator et la couche d'unwrap
d'enveloppe rejettent PROPREMENT toute donnée hostile : jamais d'exception
fatale, jamais de faux succès, toujours un chemin de re-demande utilisable.
"""
from __future__ import annotations

import math

import pytest

from conftest import run


# =====================================================================
# 1. enforce_contract — battery de payloads hostiles
# =====================================================================

_GARBAGE_PAYLOADS = [
    # (intent, payload, doit_rejeter, champ_attendu_si_rejet)
    # — Valeurs financières hostiles : tolérance ZÉRO —
    ("BUYER_ADD_TO_CART", {"product": "maïs", "quantity": -5, "phone": "+22670000000"}, True, "quantity"),
    ("BUYER_ADD_TO_CART", {"product": "maïs", "quantity": 0, "phone": "+22670000000"}, True, "quantity"),
    ("SALES_PUBLISH_PRODUCT", {"product": "maïs", "quantity": 10, "price": -250, "phone": "+22670000000"}, True, "price"),
    ("SALES_PUBLISH_PRODUCT", {"product": "maïs", "quantity": 10, "price": 0, "phone": "+22670000000"}, True, "price"),
    ("BUYER_NEGOTIATE_PRICE", {"product": "riz", "price": float("nan"), "phone": "+22670000000"}, True, "price"),
    # — Types invalides (hallucination LLM) —
    ("BUYER_ADD_TO_CART", {"product": "maïs", "quantity": "beaucoup", "phone": "+22670000000"}, True, "quantity"),
    ("BUYER_ADD_TO_CART", {"product": "maïs", "quantity": {"nested": "dict"}, "phone": "+22670000000"}, True, "quantity"),
    ("SALES_PUBLISH_PRODUCT", {"product": "x", "quantity": 10, "price": 100, "phone": "+22670000000"}, True, "product"),
    ("BUYER_CHECK_ORDER_STATUS", {"phone": "+22670000000", "order_id": "ab"}, True, "order_id"),
    # — Présence : TOLÉRÉE (responsabilité d'INTENT_CONFIG.required, pas des contrats)
    #   Un contrat qui exige la présence casserait le flux « liste → sélectionne ».
    ("BUYER_CHECK_ORDER_STATUS", {"phone": "+22670000000"}, False, None),
    ("BUYER_ADD_TO_CART", {}, False, None),
    ("BUYER_LIST_ORDERS", {"phone": "+22670000000"}, False, None),
    # — Valeurs saines : ne JAMAIS sur-rejeter (faux positifs = friction UX) —
    ("BUYER_ADD_TO_CART", {"product": "maïs blanc", "quantity": 50, "unit": "KG", "phone": "+22670000000"}, False, None),
    ("BUYER_ADD_TO_CART", {"product": "maïs", "quantity": "50", "phone": "+22670000000"}, False, None),  # coercition str→float
    ("SALES_PUBLISH_PRODUCT", {"product": "sésame", "quantity": 200, "price": 300, "phone": "+22670000000",
                                "extra_hallucinated_key": {"x": 1}}, False, None),  # clés inconnues ignorées
    # — Intent hors catalogue : pass-through neutre —
    ("INTENT_INVENTE_PAR_LE_LLM", {"anything": -999}, False, None),
]


@pytest.mark.parametrize("intent,payload,must_reject,bad_field", _GARBAGE_PAYLOADS)
def test_contract_garbage_battery(intent, payload, must_reject, bad_field):
    """Rupture prévenue : le LLM hallucine une valeur (quantité négative,
    prix nul, dict imbriqué) et elle atteint la base de données. Le contrat
    doit rejeter la VALEUR sans jamais lever, et tolérer l'ABSENCE."""
    from agriconnect.graphs.agents.market_coach.interpreter.contracts import (
        enforce_contract,
    )

    ok, msg, field = enforce_contract(intent, dict(payload))
    assert isinstance(ok, bool), "contrat: contrat de retour (bool, msg, field) violé"
    if must_reject:
        assert not ok, f"{intent} {payload} aurait dû être rejeté"
        assert field == bad_field, f"champ fautif attendu {bad_field}, obtenu {field}"
        assert msg, "message de rejet vide = re-demande impossible"
    else:
        assert ok, f"{intent} {payload} rejeté à tort: {msg} ({field})"


def test_nan_never_passes_gt_zero():
    """Rupture prévenue : float('nan') > 0 est False en Python mais certains
    chemins de coercition le laissent filtrer. NaN dans un montant = ligne
    comptable corrompue. Preuve dédiée, indépendante de la battery."""
    from agriconnect.graphs.agents.market_coach.interpreter.contracts import (
        enforce_contract,
    )
    for bad in (float("nan"), float("inf"), float("-inf")):
        ok, _, _ = enforce_contract(
            "SALES_PUBLISH_PRODUCT",
            {"product": "maïs", "quantity": bad, "price": 100, "phone": "+22670000000"},
        )
        # inf est > 0 : c'est le garde-fou de bornes du validator (100 000)
        # qui le coupe — ici on exige seulement : jamais d'exception, et NaN rejeté.
        if math.isnan(bad):
            assert not ok, "NaN accepté dans quantity = corruption comptable"


# =====================================================================
# 2. validator — payloads hostiles de bout en bout
# =====================================================================

@pytest.mark.parametrize("payload,expect_error_fragment", [
    ({"product": "maïs", "quantity": -10, "unit": "KG", "price": 250}, "quantité"),
    ({"product": "maïs", "quantity": 50, "unit": "KG", "price": "gratuit"}, "prix"),
    ({"product": "maïs", "quantity": "abc", "unit": "KG", "price": 250}, "quantité"),
    ({"product": "x", "quantity": 50, "unit": "KG", "price": 250}, None),  # contrat: product min 2
])
def test_validator_rejects_garbage_without_crashing(payload, expect_error_fragment):
    """Rupture prévenue : un payload corrompu traverse le validator et part
    en écriture. Attendu : WAITING_INPUT + validation_errors peuplés +
    aucune exception. Le tour suivant peut re-demander le champ."""
    from agriconnect.graphs.agents.market_coach.nodes.validation import validator

    state = {
        "current_goal": "SALES_PUBLISH_PRODUCT",
        "user_phone": "+22670000000",
        "transaction_payload": dict(payload),
        "working_memory": {},
    }
    out = run(validator(state, None))
    assert out["status"] == "WAITING_INPUT", f"garbage accepté: {out['status']}"
    errors = " ".join(str(e) for e in (out.get("validation_errors") or []))
    assert errors, "rejet silencieux = utilisateur perdu"
    if expect_error_fragment:
        assert expect_error_fragment in errors.lower(), errors


def test_validator_handles_completely_empty_state():
    """Rupture prévenue : état vide (workspace corrompu/tronqué à 480KB).
    Le validator doit rendre une demande de clarification, pas un KeyError."""
    from agriconnect.graphs.agents.market_coach.nodes.validation import validator

    out = run(validator({}, None))
    assert out["status"] == "WAITING_INPUT"
    assert out["response_strategy"] == "CLARIFICATION"


# =====================================================================
# 3. unwrap_tool_envelope / is_success_response — formes hostiles
# =====================================================================

@pytest.mark.parametrize("raw,expected_status,expected_success", [
    # Enveloppe ko + data vide (LE bug historique) → erreur domaine explicite
    ({"ok": False, "data": {}, "error": "timeout", "meta": {}}, "error", False),
    # Enveloppe ok + data dict → merge, succès
    ({"ok": True, "data": {"available_quantity": 875}, "meta": {}}, "success", True),
    # Enveloppe ok + data None → succès sans contenu (pas un crash)
    ({"ok": True, "data": None}, "success", True),
    # Enveloppe ko sans error → erreur muette mais erreur quand même
    ({"ok": False}, "error", False),
])
def test_envelope_unwrap_hostile_shapes(raw, expected_status, expected_success):
    """Rupture prévenue : chaque forme d'enveloppe mal normalisée devient
    soit un faux succès (transaction fantôme) soit un crash de renderer."""
    from agriconnect.graphs.agents.market_coach.utils import (
        is_success_response,
        unwrap_tool_envelope,
    )
    out = unwrap_tool_envelope(raw)
    assert isinstance(out, dict)
    assert out.get("status") == expected_status, out
    assert is_success_response(out) is expected_success


@pytest.mark.parametrize("untouched", [
    {"status": "success", "data": [1, 2, 3]},          # dict domaine avec data LISTE
    {"status": "error", "message": "x"},
    {"data": [{"product": "maïs"}]},                    # data liste SANS ok ni status
    {"ok": "yes", "data": {}},                          # ok non booléen = pas une enveloppe
    "une chaîne brute",
    None,
    42,
])
def test_envelope_unwrap_never_corrupts_domain_shapes(untouched):
    """Rupture prévenue : ~20 consommateurs lisent result['data'] (listes
    domaine). Un unwrap trop zélé qui « déballe » un dict domaine détruirait
    ces réponses. Détection STRICTE exigée : identité préservée."""
    from agriconnect.graphs.agents.market_coach.utils import unwrap_tool_envelope

    out = unwrap_tool_envelope(untouched)
    assert out is untouched, f"forme domaine corrompue: {untouched!r} → {out!r}"


@pytest.mark.parametrize("res,expected", [
    (None, False),
    ({}, False),
    ({"status": "PENDING"}, False),          # statut inconnu ≠ succès
    ({"status": "SUCCESS"}, True),
    ({"status": "not_found"}, False),
    ({"ok": True}, True),                     # filet enveloppe : ok fait foi
    ({"ok": False, "data": {"x": 1}}, False), # data présent mais ok=False → JAMAIS succès
    ({"error": "boom"}, False),
    ({"data": [1]}, True),                    # forme legacy sans status
    ({"_exception": "x"}, False),
])
def test_is_success_response_battery(res, expected):
    """Rupture prévenue : un faux positif ici déclenche un message de succès
    pour une transaction qui n'a PAS eu lieu — la pire trahison de confiance
    possible pour un producteur qui croit son produit publié."""
    from agriconnect.graphs.agents.market_coach.utils import is_success_response

    assert bool(is_success_response(res)) is expected, f"{res!r}"


# =====================================================================
# 4. ensure_dict — réponses transport corrompues
# =====================================================================

@pytest.mark.parametrize("raw", [
    "{'status': 'error', 'message': \"Pas d'offres pour l'instant\"}",  # littéral python + apostrophes FR
    '{"status": "success", "data": []}',                                 # JSON propre
    "garbage non structuré %%%",
    "",
    None,
    b"bytes inattendus".decode(),
])
def test_ensure_dict_never_raises(raw):
    """Rupture prévenue : un transport MCP qui sérialise en littéral Python
    (apostrophes françaises « d'offres ») cassait le parsing naïf par
    remplacement de quotes — le message disparaissait silencieusement.
    Contrat : toujours un dict, jamais une exception."""
    from agriconnect.graphs.agents.market_coach.utils import ensure_dict

    out = ensure_dict(raw)
    assert isinstance(out, dict), f"{raw!r} → {type(out)}"


def test_ensure_dict_preserves_french_apostrophes():
    """Preuve dédiée du piège apostrophe : le message français doit survivre
    intact au dépliage (ast.literal_eval AVANT le nettoyage destructif)."""
    from agriconnect.graphs.agents.market_coach.utils import ensure_dict

    out = ensure_dict("{'status': 'error', 'message': \"Pas d'appel d'offres trouvé\"}")
    assert out.get("message") == "Pas d'appel d'offres trouvé", out
