"""`ProductMixin.add_product_photo` — liaison d'une photo Supabase à un
produit du catalogue (feature "photo produit par WhatsApp").

Session SQLAlchemy simulée (pas de DB réelle) : ces tests verrouillent la
LOGIQUE (append/dedup/cap/replace/ownership), pas la requête SQL elle-même.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from tests.conftest import run


class _FakeProduct:
    def __init__(self, product_id, producer_id, images=None, name="Tomates"):
        self.id = product_id
        self.producer_id = producer_id
        self.images = images or []
        self.name = name
        self.price = 100

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "images": list(self.images),
            "price": self.price,
        }


class _FakeResult:
    def __init__(self, product):
        self._product = product

    def scalar_one_or_none(self):
        return self._product


class _FakeSession:
    def __init__(self, product):
        self._product = product
        self.flushed = False
        self.refreshed = False

    async def execute(self, _stmt):
        return _FakeResult(self._product)

    async def flush(self):
        self.flushed = True

    async def refresh(self, _obj):
        self.refreshed = True


def _service(product, producer_id):
    from agriconnect.services.database.product import ProductMixin

    class _Svc(ProductMixin):
        def __init__(self, session, producer_id):
            self._session = session
            self._producer_id = producer_id

        @property
        def session(self):
            return self._session

        async def get_producer_profile(self, phone):
            producer = MagicMock()
            producer.id = self._producer_id
            return MagicMock(), producer

    return _Svc(_FakeSession(product), producer_id)


PHONE = "+22670000001"


class TestAddProductPhotoAppend:
    def test_first_photo_is_appended_to_an_empty_list(self):
        producer_id = uuid.uuid4()
        product = _FakeProduct(uuid.uuid4(), producer_id, images=[])
        svc = _service(product, producer_id)

        result = run(svc.add_product_photo(phone=PHONE, product_id=str(product.id), image_url="https://x/a.jpg"))

        assert result["status"] == "success"
        assert product.images == ["https://x/a.jpg"]

    def test_second_photo_is_appended_not_replacing_the_first(self):
        producer_id = uuid.uuid4()
        product = _FakeProduct(uuid.uuid4(), producer_id, images=["https://x/a.jpg"])
        svc = _service(product, producer_id)

        run(svc.add_product_photo(phone=PHONE, product_id=str(product.id), image_url="https://x/b.jpg"))

        assert product.images == ["https://x/a.jpg", "https://x/b.jpg"]

    def test_the_same_url_twice_is_a_no_op_success_not_a_duplicate(self):
        producer_id = uuid.uuid4()
        product = _FakeProduct(uuid.uuid4(), producer_id, images=["https://x/a.jpg"])
        svc = _service(product, producer_id)

        result = run(svc.add_product_photo(phone=PHONE, product_id=str(product.id), image_url="https://x/a.jpg"))

        assert result["status"] == "success"
        assert product.images == ["https://x/a.jpg"]

    def test_photo_count_is_capped_and_keeps_the_most_recent(self):
        producer_id = uuid.uuid4()
        existing = [f"https://x/{i}.jpg" for i in range(8)]
        product = _FakeProduct(uuid.uuid4(), producer_id, images=list(existing))
        svc = _service(product, producer_id)

        run(svc.add_product_photo(phone=PHONE, product_id=str(product.id), image_url="https://x/new.jpg"))

        assert len(product.images) == 8
        assert product.images[-1] == "https://x/new.jpg"
        assert product.images[0] == existing[1]  # le plus ancien a été éjecté


class TestAddProductPhotoReplace:
    def test_replace_true_discards_all_previous_photos(self):
        producer_id = uuid.uuid4()
        product = _FakeProduct(uuid.uuid4(), producer_id, images=["https://x/old1.jpg", "https://x/old2.jpg"])
        svc = _service(product, producer_id)

        result = run(svc.add_product_photo(
            phone=PHONE, product_id=str(product.id), image_url="https://x/new.jpg", replace=True,
        ))

        assert result["status"] == "success"
        assert product.images == ["https://x/new.jpg"]


class TestAddProductPhotoOwnership:
    def test_a_product_owned_by_someone_else_is_rejected(self):
        producer_id = uuid.uuid4()
        other_producer_id = uuid.uuid4()
        product = _FakeProduct(uuid.uuid4(), other_producer_id, images=[])
        svc = _service(product, producer_id)
        # Simule le WHERE producer_id=... ne matchant rien côté SQL réel :
        svc._session._product = None

        result = run(svc.add_product_photo(phone=PHONE, product_id=str(product.id), image_url="https://x/a.jpg"))

        assert result["status"] == "error"
        assert "introuvable" in result["message"].lower()

    def test_no_producer_profile_returns_a_business_error_not_a_crash(self):
        from agriconnect.services.database.product import ProductMixin

        class _NoProducerSvc(ProductMixin):
            @property
            def session(self):
                return _FakeSession(None)

            async def get_producer_profile(self, phone):
                raise ValueError(f"Aucun compte utilisateur trouvé pour le numéro : {phone}")

        result = run(_NoProducerSvc().add_product_photo(
            phone=PHONE, product_id=str(uuid.uuid4()), image_url="https://x/a.jpg",
        ))

        assert result["status"] == "error"
