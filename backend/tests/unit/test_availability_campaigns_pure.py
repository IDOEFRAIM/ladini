"""Campagnes de disponibilités — règles PURES (aucune base) : éligibilité, message, ciblage, planification, consentement,
comparaison offre présentée / vivante."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from ladini.services.availability_campaigns import consent_text, targeting
from ladini.services.availability_campaigns import eligibility as el
from ladini.services.availability_campaigns import interest_service as it
from ladini.services.availability_campaigns import message as msg
from ladini.services.availability_campaigns.campaign_service import (
    CampaignError,
    make_run_key,
    next_run_after,
    normalize_definition,
)
from ladini.workers.outbox import templates

NOW = datetime(2026, 10, 8, 9, 0, 0)


def cand(**kw) -> el.OfferCandidate:
    base = dict(
        product_id="p1", producer_id="pr1", name="Tomate", unit="KG", quantity=500.0,
        updated_at=NOW - timedelta(hours=5), is_available=True, producer_status="APPROVED",
        region="Guiriko", price_label="450 FCFA par kg", price_status="CERTIFIED",
    )
    base.update(kw)
    return el.OfferCandidate(**base)


RULES = el.EligibilityRules()


# ── Éligibilité ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_valid_offer_is_eligible():
    assert el.evaluate_offer(cand(), NOW, RULES) == el.Verdict(True, ())


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"is_available": False}, el.INACTIVE),
        ({"expires_at": NOW - timedelta(minutes=1)}, el.EXPIRED),
        ({"quantity": 0.0}, el.ZERO_QUANTITY),
        ({"reserved_quantity": 500.0}, el.ZERO_QUANTITY),  # tout est réservé
        ({"updated_at": NOW - timedelta(hours=200)}, el.STALE),
        ({"updated_at": None}, el.STALE),  # non vérifiable = exclue, jamais « présumée fraîche »
        ({"producer_status": "PENDING"}, el.PRODUCER_NOT_APPROVED),
        ({"producer_status": None}, el.PRODUCER_NOT_APPROVED),
        ({"name": ""}, el.MISSING_FIELD),
        ({"unit": ""}, el.MISSING_FIELD),
        ({"unit": "zorglub"}, el.INCOMPATIBLE_UNIT),
    ],
)
def test_each_exclusion_has_a_stable_reason(overrides, reason):
    v = el.evaluate_offer(cand(**overrides), NOW, RULES)
    assert not v.ok and reason in v.reasons


def test_delivery_date_in_the_past_excludes_everything():
    rules = el.EligibilityRules(delivery_date=date(2026, 10, 1))
    assert el.PAST_DELIVERY_DATE in el.evaluate_offer(cand(), NOW, rules).reasons


def test_require_price_excludes_offers_without_a_reliable_price_but_default_tolerates_them():
    no_price = cand(price_label=None, price_status="LEGACY_PARTIAL")
    assert el.evaluate_offer(no_price, NOW, RULES).ok
    assert el.PRICE_REQUIRED in el.evaluate_offer(no_price, NOW, el.EligibilityRules(require_price=True)).reasons


def test_all_applicable_reasons_are_listed():
    v = el.evaluate_offer(cand(is_available=False, quantity=0.0, producer_status="PENDING"), NOW, RULES)
    assert {el.INACTIVE, el.ZERO_QUANTITY, el.PRODUCER_NOT_APPROVED} <= set(v.reasons)


def test_freshness_boundary_is_the_configured_age():
    just_in = cand(updated_at=NOW - timedelta(hours=96))
    just_out = cand(updated_at=NOW - timedelta(hours=96, seconds=1))
    assert el.evaluate_offer(just_in, NOW, RULES).ok
    assert el.STALE in el.evaluate_offer(just_out, NOW, RULES).reasons


def test_selection_caps_at_five_is_deterministic_and_reports_exclusions():
    pool = [cand(product_id=f"p{i}", name=f"Prod{i}", updated_at=NOW - timedelta(hours=i + 1)) for i in range(8)]
    pool.append(cand(product_id="bad", name="Mauvais", quantity=0.0))
    chosen, excluded = el.select_offers(pool, NOW, RULES, max_offers=5)
    assert [c.product_id for c in chosen] == ["p0", "p1", "p2", "p3", "p4"]
    again, _ = el.select_offers(list(reversed(pool)), NOW, RULES, max_offers=5)
    assert [c.product_id for c in again] == [c.product_id for c in chosen]  # ordre d'entrée sans effet
    by_id = {e["product_id"]: e["reasons"] for e in excluded}
    assert by_id["bad"] == [el.ZERO_QUANTITY] and by_id["p7"] == ["OVER_LIMIT"]
    assert el.select_offers(pool, NOW, RULES, max_offers=99)[0].__len__() == 5  # jamais plus de 5


def test_no_eligible_offer_gives_empty_selection():
    chosen, excluded = el.select_offers([cand(quantity=0.0)], NOW, RULES)
    assert chosen == [] and excluded[0]["reasons"] == [el.ZERO_QUANTITY]


# ── Message ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_message_is_short_explicit_and_honest_about_missing_prices():
    offers = [
        msg.offer_snapshot(cand()),
        msg.offer_snapshot(cand(product_id="p2", name="Oignon", unit="SAC", quantity=40, price_label=None, region=None)),
    ]
    text = msg.render_campaign_message(offers, delivery_date=date(2026, 10, 12), seen_on=NOW.date())
    assert "1. Tomate — 500 kg (Guiriko) · 450 FCFA par kg" in text
    assert "2. Oignon — 40 sac · prix à confirmer" in text
    assert "12 octobre" in text and "8 octobre" in text
    assert text.splitlines()[-1] == msg.STOP_LINE
    assert len(text) < 600 and text.count("\n") <= 8


def test_message_never_exceeds_five_offers_and_refuses_an_empty_campaign():
    offers = [msg.offer_snapshot(cand(product_id=f"p{i}", name=f"P{i}")) for i in range(9)]
    assert msg.render_campaign_message(offers).count(" · ") == 5
    with pytest.raises(ValueError):
        msg.render_campaign_message([])


def test_content_hash_detects_any_change():
    offers = [msg.offer_snapshot(cand())]
    base = msg.content_hash("x", offers)
    assert base == msg.content_hash("x", offers)
    assert base != msg.content_hash("y", offers)
    assert base != msg.content_hash("x", [msg.offer_snapshot(cand(quantity=499.0))])


def test_campaign_templates_render_from_the_payload_and_consent_template_is_registered():
    assert templates.render(templates.AVAILABILITY_CAMPAIGN_BUYER, {"body": "corps"}) == "corps"
    assert templates.render(templates.CONSENT_REQUEST_BUYER, {"body": "q ?"}) == "q ?"
    text = templates.render(
        templates.AVAILABILITY_INTEREST_PRODUCER,
        {"product": "Tomate", "quantity": 100, "unit": "KG", "buyer_label": "Resto Bobo", "zone": "Guiriko"},
    )
    assert "100 kg de Tomate" in text and "pas encore une commande" in text and "+226" not in text
    # volontairement absent des propriétaires de réponse nue : la campagne ne vole pas le menu d'un autre message
    assert templates.AVAILABILITY_CAMPAIGN_BUYER not in templates.INTERACTIVE_TEMPLATE_OWNERS


# ── Ciblage ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def row(phone, consent="OPTED_IN", **kw):
    r = {"phone": phone, "consent_status": consent, "user_id": None}
    r.update(kw)
    return r


def test_targeting_requires_explicit_consent_and_dedups_by_phone():
    rows = [
        row("+22670000001"), row("+226 70 00 00 01"),  # même numéro écrit autrement
        row("+22670000002", consent="OPTED_OUT"),
        row("+22670000003"),
    ]
    kept, skipped = targeting.select_recipients(rows, {})
    assert [r.phone for r in kept] == ["+22670000001", "+22670000003"]
    reasons = {s["phone"]: s["reason"] for s in skipped}
    assert reasons["+22670000002"] == targeting.NO_CONSENT
    assert targeting.DUPLICATE in reasons.values()


def test_targeting_is_reproducible_whatever_the_input_order():
    rows = [row(f"+2267000000{i}") for i in range(1, 6)]
    a, _ = targeting.select_recipients(rows, {})
    b, _ = targeting.select_recipients(list(reversed(rows)), {})
    assert a == b


def test_targeting_excludes_inactive_accounts_and_disabled_whatsapp():
    rows = [
        row("+22670000001", user_id="u1", account_status="BLOCKED"),
        row("+22670000002", user_id="u2", account_status="ACTIVE", whatsapp_enabled=False),
        row("+22670000003", user_id="u3", account_status="ACTIVE", deleted_at=NOW),
        row("+22670000004", user_id="u4", account_status="ACTIVE", whatsapp_enabled=True),
    ]
    kept, skipped = targeting.select_recipients(rows, {})
    assert [r.phone for r in kept] == ["+22670000004"]
    assert {s["reason"] for s in skipped} == {
        targeting.ACCOUNT_NOT_ACTIVE, targeting.WHATSAPP_DISABLED}


def test_targeting_region_known_unknown_and_new_buyers():
    rows = [
        row("+22670000001", user_id="u1", account_status="ACTIVE", declared_location="Bobo-Dioulasso"),
        row("+22670000002", user_id="u2", account_status="ACTIVE", declared_location="Ouagadougou"),
        row("+22670000003", user_id="u3", account_status="ACTIVE", declared_location=None),  # région inconnue
        row("+22670000004"),  # nouvel acheteur sans compte
    ]
    kept, skipped = targeting.select_recipients(rows, {"regions": ["Guiriko"]})
    assert [r.phone for r in kept] == ["+22670000001"]
    assert {s["reason"] for s in skipped} == {targeting.REGION_MISMATCH, targeting.REGION_UNKNOWN}
    kept, _ = targeting.select_recipients(rows, {"regions": ["Guiriko"], "include_unknown_region": True})
    assert [r.phone for r in kept] == ["+22670000001", "+22670000003", "+22670000004"]
    new_only, _ = targeting.select_recipients(rows, {"audience_kind": "NEW"})
    assert [r.phone for r in new_only] == ["+22670000004"]
    known_only, _ = targeting.select_recipients(rows, {"audience_kind": "KNOWN"})
    assert len(known_only) == 3


def test_invalid_phone_is_skipped():
    kept, skipped = targeting.select_recipients([row("abc"), row("123")], {})
    assert not kept and {s["reason"] for s in skipped} == {targeting.INVALID_PHONE}


# ── Planification & définition ─────────────────────────────────────────────────────────────────────────────────────


def test_next_run_is_never_a_backfill_of_missed_occurrences():
    sched = datetime(2026, 9, 1, 8, 0)
    nxt = next_run_after("WEEKLY", 7, sched, NOW)
    assert nxt > NOW and (nxt - sched).days % 7 == 0
    assert next_run_after("ONCE", None, sched, NOW) is None
    assert next_run_after("CUSTOM_DAYS", 3, NOW, NOW) == NOW + timedelta(days=3)


def test_run_key_is_stable_for_the_same_scheduled_time():
    assert make_run_key(datetime(2026, 10, 8, 9, 0, 0)) == "20261008T090000"


def test_definition_validation():
    ok = normalize_definition({"name": "Hebdo", "frequency": "weekly", "offer_filter": {"products": ["tomate"]}})
    assert ok["frequency"] == "WEEKLY" and ok["interval_days"] == 7 and ok["offer_filter"]["max_offers"] == 5
    for bad in (
        {"name": ""},
        {"name": "x", "frequency": "DAILY"},
        {"name": "x", "frequency": "CUSTOM_DAYS"},
        {"name": "x", "offer_filter": {"max_offers": 6}},
        {"name": "x", "offer_filter": {"max_offers": 0}},
        {"name": "x", "offer_filter": {"products": "tomate"}},
        {"name": "x", "response_window_hours": 0},
        {"name": "x", "audience": {"audience_kind": "EVERYONE"}},
        {"name": "x", "delivery_date": "pas-une-date"},
    ):
        with pytest.raises(CampaignError):
            normalize_definition(bad)


# ── Consentement : détection déterministe ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "STOP", "stop", "Stop.", "arrêt", "désinscription", "Arrêtez de m'envoyer des messages",
    "je ne veux plus recevoir vos offres", "ne m'envoyez plus de notifications", "unsubscribe",
])
def test_stop_formulations_are_an_opt_out(text):
    assert consent_text.classify(text) == consent_text.OPT_OUT


@pytest.mark.parametrize("text", [
    "arrête la commande", "stop la livraison de demain", "annule ma commande", "non merci", "100 kg de tomates",
    "combien coûte le maïs", "", "arrêtez le paiement",
])
def test_ordinary_messages_are_never_an_opt_out(text):
    assert consent_text.classify(text) != consent_text.OPT_OUT


def test_explicit_subscription_and_bare_yes():
    assert consent_text.classify("je veux recevoir les disponibilités") == consent_text.OPT_IN_REQUEST
    assert consent_text.classify("inscrivez-moi") == consent_text.OPT_IN_REQUEST
    assert consent_text.classify("oui") == consent_text.YES
    assert consent_text.classify("Oui merci") == consent_text.YES
    assert consent_text.classify("oui je veux 200 kg de tomates") is None  # pas un consentement


# ── Offre présentée vs offre vivante ───────────────────────────────────────────────────────────────────────────────


PRESENTED = {"product_id": "p1", "name": "Tomate", "quantity": 500.0, "price_label": "450 FCFA par kg"}
LIVE = {"is_available": True, "quantity": 500.0, "price_label": "450 FCFA par kg"}


def test_unchanged_offer_has_no_change_and_no_note():
    assert it.compare_offer(PRESENTED, LIVE, 100) == {}
    assert it.buyer_note("tomate", {}, "kg") is None


def test_changed_quantity_price_and_oversized_request_are_reported_not_hidden():
    live = {**LIVE, "quantity": 120.0, "price_label": "500 FCFA par kg"}
    changes = it.compare_offer(PRESENTED, live, 200)
    assert changes["quantity"] == {"presented": 500.0, "live": 120.0}
    assert changes["price"]["live"] == "500 FCFA par kg"
    assert changes["requested_exceeds_live"] == {"requested": 200.0, "live": 120.0}
    note = it.buyer_note("tomate", changes, "KG")
    assert "il reste 120 kg (au lieu de 500 kg)" in note and "500 FCFA par kg" in note


def test_sold_withdrawn_or_missing_offer_is_unavailable():
    assert it.compare_offer(PRESENTED, {**LIVE, "is_available": False}, None) == {"unavailable": True}
    assert it.compare_offer(PRESENTED, {**LIVE, "quantity": 0.0}, None) == {"unavailable": True}
    assert it.compare_offer(PRESENTED, None, None) == {"unavailable": True}
    assert "n'est plus disponible" in it.buyer_note("tomate", {"unavailable": True}, None)


def test_offer_matching_is_accent_and_case_insensitive_and_exposes_ambiguity():
    offers = [{"product_id": "a", "name": "Tomate"}, {"product_id": "b", "name": "Tomates cerises"},
              {"product_id": "c", "name": "Oignon"}]
    assert [o["product_id"] for o in it.match_offers("tomates", offers)] == ["a", "b"]  # deux offres : jamais un choix arbitraire
    assert [o["product_id"] for o in it.match_offers("ÔIGNON", offers)] == ["c"]  # l'accent n'y change rien
    assert [o["product_id"] for o in it.match_offers("oignon", offers)] == ["c"]
    assert it.match_offers("", offers) == []
