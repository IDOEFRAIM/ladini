"""Idempotency-key wiring for sensitive MCP writes — follow-up pre-Hetzner
(2026-09-17). Complements `test_procurement_execution_pipeline_closure.py::
TestExecutionKey` (which only covers the pure PROCUREMENT function) with:

1. The SAME determinism proof for SALES_PUBLISH's `execution_key()` — never
   tested before because the function, while defined, was never actually
   wired into the executor (see item 2).
2. `nodes/executor.py::_derive_execution_idempotency_key` — the function
   that decides what key (if any) a given LangGraph turn's MCP call gets.
   Proves: PROCUREMENT draft in state -> its key; SALES_PUBLISH draft in
   state -> its key; NEITHER -> None (unchanged behavior for goals without
   a draft); a redelivered turn (same state re-processed from scratch, the
   real Celery-crash-redelivery scenario) derives the SAME key both times.
3. `select_winning_bid`'s idempotency key (accept-bid path,
   `flows/buyer/order_tracking.py`/`negotiation.py`) — same bid_id always
   produces the same key, different bid_id never collides.
"""

from __future__ import annotations

import re
from pathlib import Path

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    execution_key as procurement_execution_key,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    execution_key,
)
from ladini.graphs.agents.market_coach.nodes.executor import (
    _derive_execution_idempotency_key,
)


def _procurement_draft(**fields) -> ProcurementDraft:
    base = {
        "product": "tomates",
        "quantity": 2000.0,
        "unit": "KG",
        "price": 250.0,
        "price_unit": "KG",
    }
    base.update(fields)
    return ProcurementDraft.new(draft_id="idem-test", **base)


def _sales_draft(**fields) -> SalesPublishDraft:
    base = {"product": "maïs", "quantity": 100.0, "unit": "KG", "price": 200.0}
    base.update(fields)
    return SalesPublishDraft.new(draft_id="idem-sales-test", **base)


class TestSalesPublishExecutionKey:
    """Miroir de TestExecutionKey (procurement) — jamais testé avant que
    ce module ne soit réellement câblé dans l'exécuteur."""

    def test_stable_per_draft_and_version(self):
        d = _sales_draft()
        assert execution_key(d) == f"sales_publish:{d.draft_id}:{d.version}"

    def test_differs_across_versions(self):
        v1 = _sales_draft()
        v2 = SalesPublishDraft.new(draft_id=v1.draft_id, product="riz", quantity=50.0, unit="KG", price=300.0)
        object.__setattr__(v2, "version", v1.version + 1)
        assert execution_key(v1) != execution_key(v2)

    def test_differs_across_draft_ids(self):
        a = _sales_draft()
        b = SalesPublishDraft.new(draft_id="other-draft", product="maïs", quantity=100.0, unit="KG", price=200.0)
        assert execution_key(a) != execution_key(b)


class TestDeriveExecutionIdempotencyKey:
    """`nodes/executor.py::_derive_execution_idempotency_key` — QUI reçoit
    la clé, pour de vrai, pas seulement la fonction pure du domaine."""

    def test_no_draft_in_state_returns_none(self):
        assert _derive_execution_idempotency_key({}) is None
        assert _derive_execution_idempotency_key({"goal": "SEARCH_PRODUCTS"}) is None

    def test_procurement_draft_in_state_derives_procurement_key(self):
        d = _procurement_draft()
        state = {"procurement_draft": d.to_dict()}
        assert _derive_execution_idempotency_key(state) == procurement_execution_key(d)

    def test_sales_publish_draft_in_state_derives_sales_key(self):
        """Le gap réel de cette session : avant le correctif, cette assertion
        échouait (retournait None — `create_product` n'avait jamais de clé)."""
        d = _sales_draft()
        state = {"sales_publish_draft": d.to_dict()}
        assert _derive_execution_idempotency_key(state) == execution_key(d)

    def test_malformed_draft_dict_returns_none_not_crash(self):
        assert _derive_execution_idempotency_key({"procurement_draft": {"not": "a draft"}}) is None
        assert _derive_execution_idempotency_key({"sales_publish_draft": "garbage"}) is None

    def test_redelivery_of_the_same_turn_derives_the_same_key(self):
        """Le scénario RÉEL qu'on protège : un worker crash après avoir
        enregistré le draft mais avant d'avoir acquitté la tâche Celery — la
        redelivery relit EXACTEMENT le même état persisté (même draft_id,
        même version, puisque rien n'a bougé entre les deux tentatives) et
        doit donc dériver EXACTEMENT la même clé, pour que le serveur MCP la
        reconnaisse comme un rejeu (REPLAY) et non une nouvelle action."""
        d = _procurement_draft()
        state = {"procurement_draft": d.to_dict()}
        first_attempt_key = _derive_execution_idempotency_key(state)
        second_attempt_key = _derive_execution_idempotency_key(dict(state))  # nouvel objet, même contenu
        assert first_attempt_key == second_attempt_key
        assert first_attempt_key is not None

    def test_procurement_takes_precedence_when_both_present(self):
        """Cas pathologique (ne devrait jamais arriver en pratique — un seul
        draft actif par tour) : la priorité est documentée, pas accidentelle."""
        pd = _procurement_draft()
        sd = _sales_draft()
        state = {"procurement_draft": pd.to_dict(), "sales_publish_draft": sd.to_dict()}
        assert _derive_execution_idempotency_key(state) == procurement_execution_key(pd)


class TestMessageSidFallbackForNonDraftGoals:
    """(2026-09-28, audit fiabilité agent) : gap réel confirmé — les ~15
    goals WRITE sans draft versionné (SALES_PLACE_BID, STOCK_REGISTER_
    HARVEST, PRODUCER_CONFIRM_ORDER, FARM_CREATE...) recevaient TOUJOURS
    `None` ici, donc chaque tentative de la boucle de retry transitoire de
    `mcp_tool_executor` (`_MCP_MAX_TRANSIENT_RETRIES`) — ou une
    redélivraison webhook/Celery du MÊME message — repartait avec un UUID
    aléatoire côté `AgriMCPClient.call_tool`, rendant le serveur MCP
    incapable de reconnaître un rejeu : une écriture (bid, mouvement de
    stock, confirmation de commande...) pouvait s'exécuter deux fois pour
    un seul message utilisateur. `message_sid` (identifiant stable de
    l'événement WhatsApp entrant, jamais réécrit en cours de tour) ferme ce
    trou sans jamais confondre deux messages distincts."""

    def test_no_draft_and_no_tool_name_still_returns_none(self):
        """Comportement historique inchangé quand l'appelant ne fournit pas
        `tool_name` (ancien contrat, toujours honoré)."""
        assert _derive_execution_idempotency_key({"message_sid": "wamid.abc"}) is None

    def test_no_draft_but_tool_name_and_message_sid_derives_a_stable_key(self):
        state = {"message_sid": "wamid.abc123"}
        assert (
            _derive_execution_idempotency_key(state, tool_name="place_bid")
            == "place_bid:msg:wamid.abc123"
        )

    def test_no_draft_and_no_message_sid_returns_none_even_with_tool_name(self):
        assert _derive_execution_idempotency_key({}, tool_name="place_bid") is None

    def test_redelivery_of_the_same_message_derives_the_same_key(self):
        """Le scénario réel protégé : un timeout transitoire (ou une
        redélivraison webhook/Celery) rejoue EXACTEMENT le même état pour
        CE message — la clé doit être identique aux deux tentatives pour
        que le serveur MCP renvoie REPLAY plutôt que de réexécuter."""
        state = {"message_sid": "wamid.same-message"}
        first = _derive_execution_idempotency_key(state, tool_name="add_stock")
        second = _derive_execution_idempotency_key(dict(state), tool_name="add_stock")
        assert first == second is not None

    def test_two_distinct_messages_never_collide(self):
        """Deux demandes légitimes IDENTIQUES envoyées séparément par
        l'utilisateur (deux SID distincts) ne doivent JAMAIS être
        confondues — sinon la seconde serait silencieusement absorbée
        comme un rejeu de la première."""
        key_a = _derive_execution_idempotency_key({"message_sid": "wamid.A"}, tool_name="place_bid")
        key_b = _derive_execution_idempotency_key({"message_sid": "wamid.B"}, tool_name="place_bid")
        assert key_a != key_b

    def test_a_draft_still_takes_precedence_over_the_message_sid_fallback(self):
        """Un goal drafté garde sa clé stable par draft_id+version — le
        repli message_sid ne doit jamais la masquer."""
        d = _procurement_draft()
        state = {"procurement_draft": d.to_dict(), "message_sid": "wamid.irrelevant"}
        assert _derive_execution_idempotency_key(
            state, tool_name="create_auction"
        ) == procurement_execution_key(d)


class TestSelectWinningBidIdempotencyKey:
    """`bid_id` est l'identifiant métier stable de l'action "accepter cette
    offre" — un bid n'est sélectionné gagnant qu'une fois, donc pas besoin
    d'un objet draft/version comme PROCUREMENT/SALES_PUBLISH."""

    def test_same_terms_same_key(self):
        """Phase B2b : la clé est celle de la DÉCISION certifiée (mêmes termes = même clé)."""
        from tests.unit.test_bid_award_decision import decision_for

        assert decision_for(450000).idempotency_key == decision_for(450000).idempotency_key

    def test_changed_terms_change_the_key(self):
        from tests.unit.test_bid_award_decision import decision_for

        assert decision_for(450000).idempotency_key != decision_for(430000).idempotency_key

    def test_the_single_call_site_uses_the_decision_key(self):
        """Le SEUL site d'appel (`award_decision.execute_award`) passe la clé de la décision ; les deux tunnels
        (order_tracking, negotiation) y passent obligatoirement."""
        from ladini.graphs.agents.market_coach.flows.buyer import (
            award_decision,
            negotiation,
            order_tracking,
        )

        aw_src = Path(award_decision.__file__).read_text(encoding="utf-8")
        assert "idempotency_key=decision.idempotency_key" in aw_src
        assert "expected_award={\"fingerprint\": decision.fingerprint}" in aw_src
        for module in (order_tracking, negotiation):
            assert "execute_award(" in Path(module.__file__).read_text(encoding="utf-8")
