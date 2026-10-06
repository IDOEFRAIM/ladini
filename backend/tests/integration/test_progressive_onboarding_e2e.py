"""Onboarding PROGRESSIF — vrai graphe, vrai orchestrateur (harnais), service d'identité simulé.

Un nouveau numéro n'est plus forcé de remplir un profil : son intention est comprise d'abord ; on ne demande nom/région
que si l'ACTION l'exige (`domain/profile_requirements.py`), une question à la fois, et l'action reprend ensuite
sans que l'utilisateur la répète.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration


class IdentityWorld:
    """Simule `identify_or_create_user` / `get_user_by_phone` / `complete_user_profile` (une ligne `users` en mémoire)."""

    def __init__(self, *, existing: Dict[str, Any] | None = None) -> None:
        self.user: Dict[str, Any] | None = existing
        self.completions: list[Dict[str, Any]] = []
        self.created = 0

    def install(self, runtime) -> None:
        runtime.responses["get_user_by_phone"] = self._get
        runtime.responses["identify_or_create_user"] = self._create
        runtime.responses["complete_user_profile"] = self._complete

    def _profile(self) -> Dict[str, Any]:
        u = self.user or {}
        return {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": u.get("name") or "User_0001",
            "role": u.get("role", "USER"),
            "zone": {"id": u.get("zone_id"), "name": u.get("zone_name") or "Zone inconnue"},
            "declared_location": u.get("declared_location"),
            "permissions": {"can_buy": bool(u.get("buyer")), "can_sell": bool(u.get("producer")), "is_admin": False},
            "status": {"producer": "PENDING" if u.get("producer") else None, "identity_verified": False},
        }

    def _get(self, **_: Any) -> Dict[str, Any]:
        if self.user is None:
            return {"status": "NEW_USER", "phone": "+22670000001"}
        return {"status": "SUCCESS", "data": self._profile()}

    def _create(self, **_: Any) -> Dict[str, Any]:
        if self.user is None:
            self.user = {"role": "USER", "name": None}
            self.created += 1
        return {"status": "success", "data": self._profile()}

    def _complete(self, **kw: Any) -> Dict[str, Any]:
        self.completions.append(dict(kw))
        u = self.user
        assert u is not None, "complete_user_profile sur un utilisateur inexistant"
        if kw.get("name"):
            u["name"] = kw["name"]
        if kw.get("declared_location"):
            u["declared_location"] = kw["declared_location"]
        if kw.get("zone_id"):
            u["zone_id"] = kw["zone_id"]
        cap = str(kw.get("capability") or "").upper()
        if cap == "SELL":
            u["producer"] = True
        if cap == "BUY":
            u["buyer"] = True
        return {"status": "success", "data": {"id": "22222222-2222-2222-2222-222222222222"}}


@pytest.fixture()
def world():
    return IdentityWorld()


@pytest.fixture()
def conv(world):
    with ConversationHarness(role="BUYER", channel="whatsapp") as harness:
        world.install(harness.runtime)
        yield harness


def _tools(turn) -> list[str]:
    return [tool for tool, _ in turn.mcp_calls]


class TestFirstContact:
    def test_hello_creates_a_bare_contact_and_asks_nothing_personal(self, conv, world):
        t = conv.send("bonjour", llm={"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}})
        assert world.created == 1 and world.user["name"] is None and world.user["role"] == "USER"
        assert "create_user_profile" not in _tools(t)
        low = t.response.lower()
        assert not any(w in low for w in ("votre nom", "ton nom", "comment t'appelles", "quelle région", "producteur ou acheteur"))
        assert t.after.get("is_onboarding") in (False, None)

    def test_a_returning_contact_is_not_created_twice(self, conv, world):
        conv.send("bonjour", llm=None)
        conv.send("bonjour", llm=None)
        assert world.created == 1


class TestSellerGate:
    def _sell(self, **extra):
        return new_task("SALES_PUBLISH_PRODUCT", product="oignon", quantity=300.0, unit="KG", price=250.0, **extra)

    @staticmethod
    def _reach_the_publish_step(conv):
        """L'intention est comprise et complète (produit, quantité, prix par kg) : la SEULE question qui reste est celle
        du profil minimum — posée au moment de PUBLIER, jamais à l'arrivée."""
        return conv.send("j'ai 300 kg d'oignons à vendre à 250 FCFA le kg", llm=TestSellerGate._sell(None))

    def test_sell_intent_is_preserved_while_the_minimum_producer_profile_is_collected(self, conv, world):
        t = self._reach_the_publish_step(conv)
        # l'explication dit POURQUOI, puis UNE seule question (le nom)
        assert "identifier ton exploitation" in t.response and "Comment t'appelles-tu" in t.response
        assert "région" not in t.response.lower().split("comment t'appelles-tu")[1]
        assert t.after.get("profile_gate", {}).get("goal") == "SALES_PUBLISH_PRODUCT"
        assert not world.user.get("producer"), "aucun droit producteur avant le profil minimum"

        t2 = conv.send("Moussa", llm=None)
        assert world.user["name"] == "Moussa"
        assert "région" in t2.response.lower()
        assert not world.user.get("producer")

        t3 = conv.send("je suis à Bobo", llm=None)
        assert world.user["declared_location"] == "Guiriko"  # canonique, jamais « Bobo »
        assert world.user["producer"] is True  # capacité vendeur ajoutée, statut PENDING (jamais vérifié)
        assert not t3.after.get("profile_gate")
        # l'action reprend dans le MÊME tour : le flow de publication répond, pas un retour au menu
        assert "Que voulez-vous faire" not in t3.response and "Comment t'appelles-tu" not in t3.response

    def test_name_is_not_asked_again_when_known_and_only_region_is_missing(self, conv, world):
        world.user = {"role": "USER", "name": "Moussa", "buyer": True}
        t = self._reach_the_publish_step(conv)
        assert "Comment t'appelles-tu" not in t.response
        assert "région" in t.response.lower()

    def test_a_complete_producer_has_no_new_friction(self, conv, world):
        world.user = {"role": "PRODUCER", "name": "Moussa", "declared_location": "Kadiogo", "producer": True}
        t = self._reach_the_publish_step(conv)
        assert not t.after.get("profile_gate")
        assert "Comment t'appelles-tu" not in t.response and "région" not in t.response.lower()

    def test_a_buyer_becomes_a_producer_without_a_second_account(self, conv, world):
        world.user = {"role": "USER", "name": "Awa", "declared_location": "Kadiogo", "buyer": True}
        self._reach_the_publish_step(conv)
        assert world.created == 0
        assert world.user["producer"] is True and world.user["buyer"] is True  # capacités cumulatives
        assert world.user["name"] == "Awa"

    def test_the_user_is_never_trapped_in_the_profile_questions(self, conv, world):
        self._reach_the_publish_step(conv)
        t = conv.send("montre-moi d'abord les prix du marché", llm={"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}})
        assert not t.after.get("profile_gate")
        assert world.user.get("name") is None and not world.user.get("producer")


class TestBuyerFreeDiscovery:
    def test_buying_never_asks_for_a_name_first(self, conv, world):
        t = conv.send("je cherche 100 kg de tomates", llm=new_task("BUYER_REQUEST", product="tomate", quantity=100.0, unit="KG"))
        low = t.response.lower()
        assert "ton nom" not in low and "comment t'appelles" not in low and "votre nom" not in low


class TestBuyerRegionJustInTime:
    def test_region_is_asked_once_then_the_request_resumes_and_no_name_is_ever_asked(self, conv, world):
        t1 = conv.send("je cherche 100 kg de tomates", llm=new_task("BUYER_REQUEST", product="tomate", quantity=100.0, unit="KG"))
        assert "région" in t1.response.lower() and "livré" in t1.response.lower()
        assert world.user["name"] is None and t1.after.get("profile_gate", {}).get("missing") == ["region"]

        t2 = conv.send("Kadiogo", llm=None)
        assert world.user["declared_location"] == "Kadiogo"
        assert world.user["name"] is None, "un acheteur qui explore n'a pas à donner son nom"
        assert not t2.after.get("profile_gate")
        assert "région" not in t2.response.lower()  # la recherche reprend, la question n'est pas reposée

    def test_a_region_given_in_the_request_itself_is_learned_not_asked(self, conv, world):
        t = conv.send(
            "je cherche 50 kg de tomates à Bobo",
            llm=new_task("BUYER_REQUEST", product="tomate", quantity=50.0, unit="KG", zone="Bobo"),
        )
        assert world.user["declared_location"] == "Guiriko"
        assert not t.after.get("profile_gate")
        assert "dans quelle région" not in t.response.lower()

    def test_the_known_region_is_never_asked_twice(self, conv, world):
        world.user = {"role": "USER", "name": None, "declared_location": "Kadiogo", "buyer": True}
        t = conv.send("je cherche 100 kg de tomates", llm=new_task("BUYER_REQUEST", product="tomate", quantity=100.0, unit="KG"))
        assert not t.after.get("profile_gate") and "dans quelle région" not in t.response.lower()
