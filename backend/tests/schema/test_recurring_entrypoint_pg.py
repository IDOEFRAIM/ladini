"""B21.1 — « mes besoins » JUSTE APRÈS la création d'un besoin récurrent, sur le VRAI graphe et un VRAI PostgreSQL.

Bug de production (release sha-420fa6e) : un utilisateur dont le profil est ADMIN crée un besoin récurrent
(« Chevre : 3 UNITE, Chaque semaine »), puis écrit « mes besoins » et reçoit le repli générique de l'assistant
(« Salut Admin Ido ! … précise ce dont tu as besoin ») au lieu de sa liste.

Ici RIEN n'est doublé côté conversation ni côté données : le graphe LangGraph compilé réel, l'orchestrateur réel (même
décision de rôle qu'en production), `AgriDatabaseService` réel (une transaction COMMITÉE par appel MCP), le store de
drafts réel (PostgreSQL) et la lecture réelle du dernier message sortant. Seuls le LLM (aveugle : il ne sait classer
AUCUNE navigation — la route doit être déterministe), Redis et le dispatcher WhatsApp sont des doublures.
"""
from __future__ import annotations

import copy
import random
from contextlib import ExitStack
from typing import Any, Dict, List, Optional
from unittest import mock

import psycopg2
import pytest
from factories import insert, uniq

from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

UNKNOWN = {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}}
FALLBACK_MARKERS = ("peux-tu préciser", "peux-tu me préciser", "précise ce dont", "Je suis là pour t'aider")

#: Outils appelés par le graphe qu'on laisse atteindre le VRAI service de base de données (comme le fait le serveur MCP).
REAL_TOOLS = {
    "get_user_by_phone",
    "create_recurring_need",
    "create_recurring_needs",
    "list_my_recurring_needs",
    "get_recurring_need_detail",
    "get_last_interactive_outbound",
}


# ── infrastructure ──────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def real_db(pg_dsn, monkeypatch):
    """Branche `core.database` (donc `AgriDatabaseService` ET le store de drafts) sur la base de test."""
    from ladini.core import database
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "DATABASE_URL", pg_dsn)
    monkeypatch.setattr(settings, "DB_SSL_MODE", "disable")
    monkeypatch.setattr(database, "_async_engine", None)
    monkeypatch.setattr(database, "_AsyncSessionLocal", None)
    yield pg_dsn


def _sql(dsn: str, query: str, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows


def _phone() -> str:
    return "+2267" + "".join(random.choice("0123456789") for _ in range(7))


def _user(dsn: str, *, role: str, name: str, producer: bool = False, buyer: bool = False) -> Dict[str, Any]:
    """Un compte réel. `role` = colonne `auth.users.role` ; `producer`/`buyer` = profils métier rattachés."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        zone = insert(cur, "governance.zones", name=uniq("zone"), code=uniq("Z"), climatic_region_id=region)
        phone = _phone()
        user = insert(cur, "auth.users", phone=phone, name=name, role=role, zone_id=zone, onboarding_completed=True)
        prod = insert(cur, "marketplace.producers", user_id=user, zone_id=zone) if producer else None
        buy = insert(cur, "marketplace.buyer_profiles", user_id=user) if buyer else None
    conn.close()
    return {"phone": phone, "user": str(user), "producer": prod, "buyer": str(buy) if buy else None}


def _needs_of(dsn: str, phone: str) -> List[tuple]:
    """Besoins (produit, quantité, unité, buyer_id) du compte `phone`, lus directement en SQL."""
    return _sql(
        dsn,
        "select sc.name, n.quantity, n.unit, n.buyer_id::text, n.status, n.recurrence_type "
        "from marketplace.recurring_needs n join governance.sub_categories sc on sc.id = n.sub_category_id "
        "join marketplace.buyer_profiles b on b.id = n.buyer_id join auth.users u on u.id = b.user_id "
        "where u.phone = %s order by n.created_at",
        (phone,),
    )


class Conv:
    """`ConversationHarness` dont les outils DB passent par le vrai `AgriDatabaseService` (PostgreSQL)."""

    def __init__(self, dsn: str, account: Dict[str, Any], *, profile_role: Optional[str] = None) -> None:
        from ladini.services.database import recurring_need_draft_store as store
        from ladini.services.database.d import AgriDatabaseService

        self.account = account
        self.dsn = dsn
        self._real_store = {n: getattr(store, n) for n in ("load", "insert", "compare_and_swap", "find_in_doubt", "find_abandoned")}
        self.harness = ConversationHarness(role=profile_role or "ADMIN", phone=account["phone"])
        self._svc = AgriDatabaseService()
        self._stack = ExitStack()

    def __enter__(self) -> "Conv":
        from ladini.services.database import recurring_need_draft_store as store

        h = self.harness.__enter__()
        # Le harnais branche un store de drafts en mémoire : on remet le VRAI (PostgreSQL) — la garde d'exécution du
        # service lit la même ligne de draft que le graphe.
        for name, fn in self._real_store.items():
            self._stack.enter_context(mock.patch.object(store, name, fn))
        original = h.runtime.call_db

        async def call_db(tool_name: str, **kwargs: Any) -> Any:
            if tool_name not in REAL_TOOLS:
                return await original(tool_name, **kwargs)
            h.runtime.calls.append((tool_name, copy.deepcopy(kwargs)))
            kwargs.pop("idempotency_key", None)  # clé de la couche MCP (`AgriMCPClient`), jamais un argument du service
            try:
                return await getattr(self._svc, tool_name)(**kwargs)
            except Exception as exc:  # même contrat que la couche MCP : l'erreur devient une réponse
                return {"status": "error", "message": str(exc)}

        h.runtime.call_db = call_db
        return self

    def __exit__(self, *exc: Any) -> bool:
        from ladini.core import database

        self.harness._run(database.close_db())  # le pool vit dans la boucle du harnais : on le ferme dedans
        self._stack.close()
        return self.harness.__exit__(*exc)

    def send(self, text: str, **kw: Any):
        return self.harness.send(text, **kw)

    def state(self) -> Dict[str, Any]:
        return self.harness.state()

    @property
    def runtime(self):
        return self.harness.runtime

    @property
    def store(self):
        return self.harness.store

    def tools(self, turn) -> List[str]:
        return turn.mcp_tools()


def _create_chevre(conv: Conv):
    """Le vrai parcours : demande -> récapitulatif -> confirmation -> persistance -> « C'est noté ! »."""
    t1 = conv.send(
        "chevre 3 unite chaque semaine",
        llm=new_task("CREATE_RECURRING_NEED", product="Chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY"),
    )
    assert t1.error is None and "Confirmez-vous" in t1.response, t1.response
    t2 = conv.send("Confirmer")
    assert t2.error is None and "C'est noté" in t2.response, t2.response
    return t1, t2


def _is_fallback(text: str) -> bool:
    return any(m.lower() in text.lower() for m in FALLBACK_MARKERS)


def _assert_list_with_chevre(turn) -> None:
    r = turn.response
    assert turn.error is None, turn.error
    assert not _is_fallback(r), r
    assert "Mes besoins récurrents" in r, r
    low = r.lower()
    assert "chevre" in low and "3 unite" in low and "chaque semaine" in low, r


# ═══════════════════════════ 1. LE test principal : création réelle -> « mes besoins » immédiat ═══════════════════════
def test_admin_creates_a_recurring_need_then_mes_besoins_lists_it_from_the_database(real_db):
    admin = _user(real_db, role="ADMIN", name="Admin Ido")  # AUCUN profil acheteur au départ (cas prod : Admin pur)
    with Conv(real_db, admin) as conv:
        _t1, t2 = _create_chevre(conv)

        # (3, 11) l'état laissé par la création est terminal : rien du tunnel ne reste vivant.
        st = t2.after
        assert not st.get("current_goal") and not st.get("locked_goal")
        assert not st.get("pending_interaction") and not st.get("recurring_need_draft")
        assert not st.get("missing_fields") and not st.get("expected_candidates") and not st.get("available_mapping")
        assert (st.get("goal_status") or None) is None

        # (13, 26) le besoin est COMMITÉ en base, au nom de l'identité acheteur de cet utilisateur.
        rows = _needs_of(real_db, admin["phone"])
        assert [(r[0].lower(), float(r[1]), r[2], r[4], r[5]) for r in rows] == [("chevre", 3.0, "UNITE", "ACTIVE", "WEEKLY")]
        created_owner = rows[0][3]

        # « mes besoins » IMMÉDIATEMENT, avec un LLM qui ne sait rien classer : la route est déterministe.
        t3 = conv.send("mes besoins", llm=UNKNOWN)
        _assert_list_with_chevre(t3)
        assert t3.llm_calls == 0, "navigation déterministe : aucun appel LLM"
        assert t3.intent == "GET_MY_NEEDS"
        assert "list_my_recurring_needs" in t3.mcp_tools()
        # (25) créateur et lecteur partagent la MÊME identité acheteur.
        assert _sql(real_db, "select id::text from marketplace.buyer_profiles where user_id = %s", (admin["user"],)) == [(created_owner,)]

        # (30) « 1 » ouvre le détail du besoin.
        t4 = conv.send("1", llm=UNKNOWN)
        assert t4.error is None and not _is_fallback(t4.response), t4.response
        assert "get_recurring_need_detail" in t4.mcp_tools()
        assert "chevre" in t4.response.lower()


def test_the_list_comes_from_the_database_not_from_the_creation_turn(real_db):
    """(14) Un besoin AJOUTÉ en base entre-temps apparaît : la liste n'est pas un cache du tour de création."""
    admin = _user(real_db, role="ADMIN", name="Admin Ido")
    with Conv(real_db, admin) as conv:
        _create_chevre(conv)
        buyer_id = _needs_of(real_db, admin["phone"])[0][3]
        conn = psycopg2.connect(real_db)
        with conn, conn.cursor() as cur:
            cat = insert(cur, "governance.categories", name=uniq("cat"))
            sub = insert(cur, "governance.sub_categories", category_id=cat, name="Mil")
            insert(cur, "marketplace.recurring_needs", buyer_id=buyer_id, sub_category_id=sub, quantity=50, unit="KG",
                   recurrence_type="DAILY", starts_at=__import__("datetime").datetime.utcnow())
        conn.close()
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert "chevre" in t.response.lower() and "mil" in t.response.lower(), t.response


def test_after_a_logical_restart_the_need_is_still_listed(real_db):
    """(15, 21) Conversation neuve (aucun checkpoint, aucun état transitoire, autre Redis) : même utilisateur."""
    admin = _user(real_db, role="ADMIN", name="Admin Ido")
    with Conv(real_db, admin) as first:
        _create_chevre(first)
    with Conv(real_db, admin) as fresh:
        assert fresh.state() == {}
        t = fresh.send("mes besoins", llm=UNKNOWN)
        _assert_list_with_chevre(t)


@pytest.mark.parametrize(
    "alias", ["mes besoins", "mes besoins récurrents", "voir mes besoins", "mes approvisionnements récurrents"]
)
def test_the_four_aliases_reach_the_list_for_the_admin_after_creation(real_db, alias):
    admin = _user(real_db, role="ADMIN", name="Admin Ido")
    with Conv(real_db, admin) as conv:
        _create_chevre(conv)
        t = conv.send(alias, llm=UNKNOWN)
        _assert_list_with_chevre(t)
        assert t.llm_calls == 0


# ═══════════════════════════ 2. la capacité (et non le rôle) ouvre l'accès ═══════════════════════════════════════════
def _seed_need(dsn: str, buyer_id: str, product: str = "Tomate", quantity: float = 20, unit: str = "KG") -> None:
    import datetime as dt

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cat = insert(cur, "governance.categories", name=uniq("cat"))
        sub = insert(cur, "governance.sub_categories", category_id=cat, name=uniq(product))
        insert(cur, "marketplace.recurring_needs", buyer_id=buyer_id, sub_category_id=sub, quantity=quantity, unit=unit,
               recurrence_type="WEEKLY", starts_at=dt.datetime.utcnow())
    conn.close()


def test_buyer_role_classic_still_gets_the_list(real_db):
    """(9) Régression : profil BUYER."""
    buyer = _user(real_db, role="BUYER", name="Awa", buyer=True)
    _seed_need(real_db, buyer["buyer"], "Tomate")
    with Conv(real_db, buyer, profile_role="BUYER") as conv:
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert t.error is None and "Mes besoins récurrents" in t.response and "tomate" in t.response.lower(), t.response
        assert t.llm_calls == 0


def test_multi_role_producer_with_a_buyer_profile_gets_the_list(real_db):
    """(27) Producteur ET acheteur (deux profils) : la capacité acheteur suffit, quel que soit le rôle de compte."""
    both = _user(real_db, role="PRODUCER", name="Moussa", producer=True, buyer=True)
    _seed_need(real_db, both["buyer"], "Oignon")
    with Conv(real_db, both, profile_role="PRODUCER") as conv:
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert t.error is None and "Mes besoins récurrents" in t.response and "oignon" in t.response.lower(), t.response
        assert t.llm_calls == 0


def test_pure_producer_without_buyer_capability_is_not_diverted(real_db):
    """(10, 35) Aucune capacité acheteur : le contrat actuel est conservé (jamais la liste d'un autre, jamais d'appel
    au service des besoins) — la décision revient au pipeline libre."""
    other = _user(real_db, role="BUYER", name="Awa", buyer=True)
    _seed_need(real_db, other["buyer"], "Tomate")
    prod = _user(real_db, role="PRODUCER", name="Moussa", producer=True)
    with Conv(real_db, prod, profile_role="PRODUCER") as conv:
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert "list_my_recurring_needs" not in t.mcp_tools()
        assert "tomate" not in t.response.lower()
        assert t.llm_calls >= 1, "le pipeline libre (LLM) reste seul juge pour un producteur pur"


def test_admin_without_buyer_context_never_sees_other_buyers_needs(real_db):
    """(28) Admin pur : il n'obtient que SES besoins (ici aucun), jamais ceux des acheteurs."""
    other = _user(real_db, role="BUYER", name="Awa", buyer=True)
    _seed_need(real_db, other["buyer"], "Tomate")
    admin = _user(real_db, role="ADMIN", name="Admin Ido")
    with Conv(real_db, admin) as conv:
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert t.error is None
        assert "tomate" not in t.response.lower()
        assert "pas encore de besoin" in t.response.lower(), t.response
        assert t.mcp_tools().count("list_my_recurring_needs") == 1
