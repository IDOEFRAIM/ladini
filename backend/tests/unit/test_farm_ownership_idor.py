"""IDOR sur ``farm_id`` — un identifiant de ressource n'est PAS une autorisation.

Vulnérabilité réelle trouvée à l'audit sécurité de l'agent (2026-09-10).
Quatre outils d'écriture recevaient ``farm_id`` comme SEUL critère de ciblage,
sans jamais vérifier à qui l'exploitation appartient :

  - ``add_stock``    : vérifiait l'EXISTENCE de la ferme, jamais son proprio ;
  - ``remove_stock`` : ne vérifiait même pas l'existence — ciblait directement
    ``Stock.farm_id == farm_id`` (destruction d'inventaire vendable d'autrui) ;
  - ``add_expense``  : aucune vérification du tout ;
  - ``update_farm``  : « prend farm_id en priorité » = ``select(Farm).where(
    Farm.id == farm_id)`` sans filtre de propriétaire.

Ce qui rendait la chaîne exploitable : ``farm_id`` est un slot DÉCLARÉ
(``core/slots.py``), donc une valeur qui peut atteindre
``transaction_payload`` par extraction LLM ou ``form_data`` — in fine, depuis
le texte de l'utilisateur. Et ``ensure_farm_node`` court-circuitait dès qu'un
``farm_id`` était présent, sans le valider ni l'écraser.

La leçon générale, verrouillée ici : dans un agent, TOUT identifiant de
ressource venu du payload est une entrée utilisateur. La seule identité digne
de confiance est celle du tour (``state["user_phone"]``, issue du webhook
signé).
"""

from __future__ import annotations

import uuid

import pytest

from ladini.services.database.errors import BusinessRuleException
from tests.conftest import run

OWNER_PHONE = "+22670000001"
ATTACKER_PHONE = "+22670000002"
VICTIM_FARM_ID = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _OwnershipSession:
    """Doublure : ne renvoie la ferme QUE si la requête filtre sur le bon
    téléphone. Reproduit fidèlement le point qui manquait en production —
    le join ``Farm -> Producer -> User`` avec ``User.phone``."""

    def __init__(self, *, owner_phone: str = OWNER_PHONE):
        self.owner_phone = owner_phone
        self.queries: list[str] = []

    async def execute(self, stmt):
        sql = str(stmt)
        self.queries.append(sql)
        params = getattr(stmt, "compile", None)
        # La garde compile un WHERE sur User.phone ; on lit la valeur liée.
        try:
            bound = stmt.compile().params
        except Exception:
            bound = {}
        phones = [v for k, v in bound.items() if isinstance(v, str) and v.startswith("+")]
        joins_user = "users" in sql.lower()
        if joins_user and phones and phones[0] != self.owner_phone:
            return _Result([])  # ferme non détenue par ce téléphone
        farm = object()
        return _Result([farm])

    async def flush(self):
        pass


def _service(session):
    from ladini.services.database.base import BaseMixin

    class _Svc(BaseMixin):
        @property
        def session(self):
            return session

    return _Svc()


class TestFarmOwnershipGuard:
    """``BaseMixin._assert_farm_owned_by`` — la barrière autoritaire."""

    def test_the_owner_is_allowed(self):
        svc = _service(_OwnershipSession())
        farm = run(svc._assert_farm_owned_by(VICTIM_FARM_ID, OWNER_PHONE))
        assert farm is not None

    def test_another_producer_is_refused(self):
        """Le coeur du correctif : un farm_id valide mais NON détenu."""
        svc = _service(_OwnershipSession())
        with pytest.raises(BusinessRuleException) as exc:
            run(svc._assert_farm_owned_by(VICTIM_FARM_ID, ATTACKER_PHONE))
        assert exc.value.reason == "farm_not_owned"

    def test_the_query_actually_filters_on_the_caller_phone(self):
        """Anti-régression structurelle : si quelqu'un retire le join sur
        ``User.phone``, la garde deviendrait un simple test d'existence — la
        faille d'origine. On vérifie donc la FORME de la requête, pas seulement
        son résultat."""
        session = _OwnershipSession()
        run(_service(session)._assert_farm_owned_by(VICTIM_FARM_ID, OWNER_PHONE))
        sql = " ".join(session.queries).lower()
        assert "users" in sql, "la garde ne joint plus User — plus de contrôle de propriété"
        assert "phone" in sql

    def test_a_missing_caller_identity_is_refused_not_ignored(self):
        """Pas d'identité = pas d'autorisation possible. Jamais « laisser
        passer parce qu'on ne sait pas »."""
        svc = _service(_OwnershipSession())
        for empty in (None, "", "   "):
            with pytest.raises(BusinessRuleException) as exc:
                run(svc._assert_farm_owned_by(VICTIM_FARM_ID, empty))
            assert exc.value.reason == "missing_caller_identity"

    def test_a_malformed_farm_id_is_refused(self):
        svc = _service(_OwnershipSession())
        with pytest.raises(BusinessRuleException) as exc:
            run(svc._assert_farm_owned_by("pas-un-uuid", OWNER_PHONE))
        assert exc.value.reason == "invalid_farm_id"

    def test_the_refusal_is_not_an_enumeration_oracle(self):
        """« N'existe pas » et « existe mais appartient à un autre » doivent
        donner le MÊME message : sinon le refus permet d'énumérer les fermes
        de la plateforme."""
        svc = _service(_OwnershipSession())
        with pytest.raises(BusinessRuleException) as not_owned:
            run(svc._assert_farm_owned_by(VICTIM_FARM_ID, ATTACKER_PHONE))
        with pytest.raises(BusinessRuleException) as absent:
            run(svc._assert_farm_owned_by(str(uuid.uuid4()), ATTACKER_PHONE))
        assert str(not_owned.value) == str(absent.value)


class TestWriteToolsRequireCallerIdentity:
    """Les 4 outils vulnérables doivent EXIGER l'identité de l'appelant.

    Vérifié sur la SIGNATURE : c'est elle qui produit le schéma JSON exposé
    par MCP, donc c'est elle qui fait rejeter un appel sans identité en amont
    du handler (constaté par le schéma MCP dans
    ``tests/unit/test_mcp_hardening.py``).
    """

    @pytest.mark.parametrize(
        "method",
        [
            "add_stock",
            "remove_stock",
            "adjust_stock",
            "add_expense",
            # Lecture détaillée par ferme (goal STOCK_GET_DETAIL) — même classe
            # d'IDOR, ajoutée quand l'outil a enfin été implémenté (2026-09-10).
            "get_farm_stocks",
        ],
    )
    def test_the_caller_phone_is_a_required_parameter(self, method):
        import inspect

        from ladini.services.database.marketplace import MarketplaceMixin

        sig = inspect.signature(getattr(MarketplaceMixin, method))
        assert "producer_phone" in sig.parameters, (
            f"{method} n'exige plus l'identité de l'appelant — `farm_id` "
            "redevient le seul critère de ciblage (IDOR)."
        )
        param = sig.parameters["producer_phone"]
        assert param.default is inspect.Parameter.empty, (
            f"{method}: `producer_phone` a un défaut — un appelant qui l'oublie "
            "repasserait silencieusement sans contrôle de propriété (fail-open)."
        )

    @pytest.mark.parametrize(
        "method", ["add_stock", "remove_stock", "add_expense", "get_farm_stocks"]
    )
    def test_the_ownership_guard_is_actually_invoked(self, method):
        """La présence du paramètre ne suffit pas : il doit être VÉRIFIÉ."""
        import inspect

        from ladini.services.database.marketplace import MarketplaceMixin

        src = inspect.getsource(getattr(MarketplaceMixin, method))
        assert "_assert_farm_owned_by" in src, (
            f"{method} accepte `producer_phone` mais ne le vérifie pas — "
            "paramètre décoratif."
        )

    def test_adjust_stock_forwards_the_identity_to_both_delegates(self):
        """``adjust_stock`` délègue à add_stock/remove_stock : sans propagation
        il serait un contournement complet des deux gardes."""
        import inspect

        from ladini.services.database.marketplace import MarketplaceMixin

        src = inspect.getsource(MarketplaceMixin.adjust_stock)
        assert src.count("producer_phone=producer_phone") == 2

    def test_update_farm_resolves_identity_before_selecting_by_id(self):
        import inspect

        from ladini.services.database.producer import ProducerMgmtMixin

        src = inspect.getsource(ProducerMgmtMixin.update_farm)
        assert "_assert_farm_owned_by" in src, (
            "update_farm sélectionne de nouveau une ferme par id sans contrôle "
            "de propriétaire."
        )


class TestAgentPassesTheCallerIdentity:
    """Le correctif base ne protège que si l'agent transmet réellement le
    téléphone du tour — sinon tout appel échoue (panne), au lieu d'être
    autorisé correctement."""

    def test_register_harvest_forwards_the_turn_phone(self):
        from ladini.graphs.agents.market_coach.domain.model import DomainContext
        from ladini.graphs.agents.market_coach.domain.stock import StockService

        service = StockService(context=DomainContext.from_state({"user_phone": OWNER_PHONE}))
        result = service.register_harvest(
            {"user_phone": OWNER_PHONE},
            {"farm_id": VICTIM_FARM_ID, "product": "tomates", "quantity": 10},
        )
        assert result.tool_args["producer_phone"] == OWNER_PHONE

    def test_log_expense_forwards_the_turn_phone(self):
        from ladini.graphs.agents.market_coach.domain.finance import (
            FinanceLogExpenseCommand,
            FinanceService,
        )
        from ladini.graphs.agents.market_coach.domain.model import DomainContext

        service = FinanceService(
            context=DomainContext.from_state({"user_phone": OWNER_PHONE})
        )
        result = service.log_expense(
            FinanceLogExpenseCommand(
                phone=OWNER_PHONE, farm_id=VICTIM_FARM_ID, amount=1500.0, label="engrais"
            )
        )
        assert result.tool_args["producer_phone"] == OWNER_PHONE

    def test_stock_get_detail_forwards_the_turn_phone(self):
        """``get_farm_stocks`` a longtemps été appelé sans identité
        (« Phone n'est pas attendu ») — l'implémenter sans épingler
        `producer_phone` aurait rouvert l'IDOR en lecture."""
        from ladini.graphs.agents.market_coach.domain.model import DomainContext
        from ladini.graphs.agents.market_coach.domain.stock import StockService

        service = StockService(
            context=DomainContext.from_state({"user_phone": OWNER_PHONE})
        )
        result = service.get_detail(
            {"user_phone": OWNER_PHONE}, {"farm_id": VICTIM_FARM_ID}
        )
        assert result.tool_args["producer_phone"] == OWNER_PHONE
        assert result.tool_args["farm_id"] == VICTIM_FARM_ID


class TestGetFarmStocksIsWiredAndSafe:
    """``get_farm_stocks`` — l'outil qui était référencé partout sans jamais
    exister (goal ``STOCK_GET_DETAIL`` mort). Implémenté le 2026-09-10."""

    def test_the_intent_is_no_longer_deprecated(self):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _DEPRECATED_INTENTS,
            allowed_intents_for_role,
        )

        assert "STOCK_GET_DETAIL" not in _DEPRECATED_INTENTS
        # Et donc de nouveau classable depuis un message utilisateur.
        assert "STOCK_GET_DETAIL" in allowed_intents_for_role("PRODUCER")

    def test_the_tool_now_exists_and_is_exposed(self):
        from ladini.infrastructure.mcp.exposure import MCP_EXPOSED_TOOLS
        from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP
        from ladini.protocols.mcp.servers.h import EXPOSED_METHODS, TOOL_HANDLERS

        for container, label in (
            (EXPOSED_METHODS, "EXPOSED_METHODS"),
            (TOOL_HANDLERS, "TOOL_HANDLERS"),
            (MCP_EXPOSED_TOOLS, "MCP_EXPOSED_TOOLS"),
            (TOOL_SCOPE_MAP, "TOOL_SCOPE_MAP"),
        ):
            assert "get_farm_stocks" in container, f"absent de {label}"

    def _marketplace_service(self, session):
        from ladini.services.database.marketplace import MarketplaceMixin

        class _Svc(MarketplaceMixin):
            @property
            def session(self):
                return session

        return _Svc()

    def test_a_foreign_farm_is_refused(self):
        svc = self._marketplace_service(_OwnershipSession())
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.get_farm_stocks(VICTIM_FARM_ID, ATTACKER_PHONE))
        assert exc.value.reason == "farm_not_owned"

    def test_the_owner_gets_a_structured_detail_payload(self):
        import uuid as _uuid

        farm_id = _uuid.uuid4()

        class _Farm:
            id = farm_id
            name = "Ferme principale"
            location = "Bobo-Dioulasso"

        class _Stock:
            def __init__(self, name, qty):
                self.id = _uuid.uuid4()
                self.item_name = name
                self.quantity = qty
                self.unit = "KG"
                self.type = "HARVEST"
                self.updated_at = None

        stocks = [_Stock("mais", 120.0), _Stock("tomates", 40.0)]

        class _Res:
            def __init__(self, rows):
                self._rows = list(rows)

            def scalars(self):
                return self

            def all(self):
                return list(self._rows)

            def first(self):
                return self._rows[0] if self._rows else None

        class _Session:
            def __init__(self):
                self._calls = 0

            async def execute(self, _stmt):
                self._calls += 1
                # 1er execute = _assert_farm_owned_by (renvoie le Farm),
                # 2e = les Stock, 3e = les StockMovement (aucun ici).
                if self._calls == 1:
                    return _Res([_Farm()])
                if self._calls == 2:
                    return _Res(stocks)
                return _Res([])

        out = run(self._marketplace_service(_Session()).get_farm_stocks(
            str(farm_id), OWNER_PHONE
        ))
        assert out["status"] == "success"
        data = out["data"]
        assert data["farm_id"] == str(farm_id)
        assert data["farm_name"] == "Ferme principale"
        assert data["count"] == 2
        assert {s["item_name"] for s in data["stocks"]} == {"mais", "tomates"}
        # Chaque ligne porte le champ « dernier mouvement » (ce qui distingue
        # cette vue de STOCK_GET_SUMMARY), ici None faute de mouvement.
        assert all("last_movement_at" in s for s in data["stocks"])
