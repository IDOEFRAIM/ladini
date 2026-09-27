"""`EscrowMixin.verify_delivery_otp` — anti-force-brute du code de livraison.

Vulnérabilité réelle trouvée à l'audit sécurité du 2026-09-10 : ce
vérificateur, seul chemin qui fait passer une commande en
``PAID_OUT``/``DELIVERED`` (donc qui DÉBLOQUE LES FONDS séquestrés), n'avait
AUCUN compteur de tentatives sur un code de 4 chiffres — 10 000 possibilités.
Un producteur pouvait donc deviner un code et encaisser sans que l'acheteur
ait jamais confirmé la livraison.

Deux aggravations s'ajoutaient à l'absence de limite :
  - la requête cherchait le code parmi TOUTES les commandes ESCROWED du
    producteur à la fois, donc un seul essai testait N codes simultanément —
    l'espace de recherche effectif était divisé par le nombre de livraisons
    en cours ;
  - un code validé restait en base, donc rejouable.

Ces tests verrouillent le comportement corrigé. Ils utilisent une doublure de
session minimale (aucune base réelle, cf. tests/conftest.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ladini.domain.models import Order
from ladini.services.database.errors import BusinessRuleException
from tests.conftest import run

PRODUCER_ID = uuid.uuid4()


def _order(otp: str, *, attempts: int = 0, locked_until=None) -> Order:
    order = Order()
    order.id = uuid.uuid4()
    order.payment_status = "ESCROWED"
    order.delivery_status = "PENDING"
    order.delivery_otp = otp
    order.delivery_otp_attempts = attempts
    order.delivery_otp_locked_until = locked_until
    order.total_amount = 10000
    order.locked_amount = 10000
    order.currency = "FCFA"
    return order


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def unique(self):
        return list(self._rows)


class _Session:
    """Doublure : répond au lookup producteur puis à la requête des commandes."""

    def __init__(self, orders, *, producer_found: bool = True):
        self._orders = orders
        self._producer_found = producer_found
        self.flushes = 0

    async def execute(self, _stmt):
        found = self._producer_found

        class _Result:
            def first(self):
                return (PRODUCER_ID,) if found else None

        return _Result()

    async def scalars(self, _stmt):
        return _Scalars(self._orders)

    async def scalar(self, _stmt):
        # Producer Analytics Phase B: verify_delivery_otp's delivery emit now
        # resolves producer_id (this order has no `items` loaded in __dict__,
        # a real ORM Order() built directly rather than via a query, so the
        # resolver falls back to this one bounded query) — resolves to the
        # same producer this whole file's OTP flow is already scoped to.
        return PRODUCER_ID

    async def flush(self):
        self.flushes += 1


def _service(session):
    from ladini.services.database.escrow import EscrowMixin

    class _Svc(EscrowMixin):
        @property
        def session(self):
            return session

    return _Svc()


def _verify(session, code):
    return run(_service(session).verify_delivery_otp("+22670000001", code))


class TestVerifyDeliveryOtpBruteforce:
    def test_the_correct_code_releases_the_funds(self):
        order = _order("1234")
        session = _Session([order])
        result = _verify(session, "1234")
        assert result["status"] == "success"
        assert order.payment_status == "PAID_OUT"
        assert order.delivery_status == "DELIVERED"

    def test_a_consumed_code_is_cleared_so_it_cannot_be_replayed(self):
        """Un code validé ne doit plus rester en base : sinon le même code
        rejoué plus tard tomberait sur une autre commande du producteur."""
        order = _order("1234")
        _verify(_Session([order]), "1234")
        assert order.delivery_otp is None
        assert order.delivery_otp_attempts == 0
        assert order.delivery_otp_locked_until is None

    def test_a_wrong_code_increments_the_attempt_counter(self):
        """Le cœur du correctif : un essai raté doit LAISSER UNE TRACE.
        Sans compteur, rien ne distinguait le 1er essai du 5000e."""
        order = _order("1234")
        session = _Session([order])
        with pytest.raises(BusinessRuleException) as exc:
            _verify(session, "9999")
        assert exc.value.reason == "otp_mismatch"
        assert order.delivery_otp_attempts == 1

    def test_repeated_wrong_codes_eventually_lock_the_order(self):
        from ladini.services.database.escrow import _OTP_MAX_ATTEMPTS

        # Borne dure : ce test boucle jusqu'au seuil, il ne doit jamais pouvoir
        # tourner « indéfiniment » si quelqu'un desserre la constante (un
        # `_OTP_MAX_ATTEMPTS` énorme est justement la régression à attraper —
        # elle doit se voir comme un échec net, pas comme un test qui pend).
        assert _OTP_MAX_ATTEMPTS <= 5, (
            f"_OTP_MAX_ATTEMPTS={_OTP_MAX_ATTEMPTS} : seuil trop permissif, "
            "la force brute du code à 4 chiffres redevient praticable."
        )

        order = _order("1234")
        session = _Session([order])

        for attempt in range(1, _OTP_MAX_ATTEMPTS):
            with pytest.raises(BusinessRuleException) as exc:
                _verify(session, "0000")
            assert exc.value.reason == "otp_mismatch", attempt

        # L'essai qui atteint le seuil verrouille.
        with pytest.raises(BusinessRuleException) as exc:
            _verify(session, "0000")
        assert exc.value.reason == "otp_locked"
        assert order.delivery_otp_locked_until is not None

    def test_a_locked_order_refuses_even_the_correct_code(self):
        """Sinon le verrouillage n'en serait pas un : l'attaquant continuerait
        simplement à soumettre des codes pendant le blocage."""
        locked_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            minutes=10
        )
        order = _order("1234", locked_until=locked_until)
        session = _Session([order])
        with pytest.raises(BusinessRuleException) as exc:
            _verify(session, "1234")  # LE BON code
        assert exc.value.reason == "otp_locked"
        assert order.payment_status == "ESCROWED", "les fonds ne doivent pas bouger"

    def test_an_expired_lock_lets_the_correct_code_through_again(self):
        """Le verrou est temporaire, pas définitif — un producteur légitime qui
        s'est trompé 5 fois ne doit pas perdre l'accès à ses fonds."""
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        order = _order("1234", locked_until=past)
        result = _verify(_Session([order]), "1234")
        assert result["status"] == "success"

    def test_one_wrong_guess_counts_against_every_unlocked_order(self):
        """La requête d'origine testait le code contre TOUTES les commandes
        ESCROWED du producteur en même temps (donc N codes par essai). La
        comptabilité doit refléter ça : un essai raté compte pour chacune,
        sinon N livraisons en cours donneraient N fois plus d'essais gratuits.
        """
        orders = [_order("1111"), _order("2222"), _order("3333")]
        session = _Session(orders)
        with pytest.raises(BusinessRuleException):
            _verify(session, "9999")
        assert [o.delivery_otp_attempts for o in orders] == [1, 1, 1]

    def test_a_correct_code_does_not_penalize_the_other_orders(self):
        orders = [_order("1111"), _order("2222")]
        session = _Session(orders)
        result = _verify(session, "2222")
        assert result["status"] == "success"
        assert orders[0].delivery_otp_attempts == 0
        assert orders[1].payment_status == "PAID_OUT"

    def test_a_malformed_code_is_rejected_before_touching_the_database(self):
        session = _Session([_order("1234")])
        for bad in ("", "12", "123456", "abcd", None):
            with pytest.raises(BusinessRuleException):
                _verify(session, bad)
        assert session.flushes == 0

    def test_a_producer_with_no_escrowed_order_gets_a_plain_mismatch(self):
        """Aucune commande à deviner : pas de verrou à poser, pas de crash."""
        session = _Session([])
        with pytest.raises(BusinessRuleException) as exc:
            _verify(session, "1234")
        assert exc.value.reason == "otp_mismatch"


class TestOtpEntropyAndIssuance:
    def test_the_otp_is_generated_with_a_csprng(self):
        """Jamais `random` pour un secret qui débloque de l'argent."""
        import inspect

        from ladini.services.database import escrow

        source = inspect.getsource(escrow._generate_otp)
        assert "secrets." in source
        assert "random.rand" not in source

    def test_lockout_parameters_stay_restrictive(self):
        """Garde-fou anti-dérive : desserrer ces deux valeurs rouvre l'attaque
        (10 000 codes seulement). Les changer doit être un choix conscient."""
        from ladini.services.database.escrow import _OTP_LOCKOUT, _OTP_MAX_ATTEMPTS

        assert 1 <= _OTP_MAX_ATTEMPTS <= 5
        assert _OTP_LOCKOUT >= timedelta(minutes=10)
