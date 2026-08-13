"""`AuctionMixin.add_bid_photo` / `add_auction_photo` — même pattern que
`ProductMixin.add_product_photo` (test_product_photo.py), étendu au domaine
enchères/appels d'offres."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from tests.conftest import run

PHONE = "+22670000001"


class _FakeRow:
    def __init__(self, row_id, owner_id, images=None, extra=None):
        self.id = row_id
        self.images = images or []
        for k, v in (extra or {}).items():
            setattr(self, k, v)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, _stmt):
        return _FakeResult(self._row)

    async def flush(self):
        pass

    async def refresh(self, _obj):
        pass


def _bid_service(bid, producer_id):
    from agriconnect.services.database.auction import AuctionMixin

    class _Svc(AuctionMixin):
        def __init__(self, session):
            self._session = session

        @property
        def session(self):
            return self._session

        async def get_producer_profile(self, phone):
            producer = MagicMock()
            producer.id = producer_id
            return MagicMock(), producer

    return _Svc(_FakeSession(bid))


def _auction_service(auction, buyer_id):
    from agriconnect.services.database.auction import AuctionMixin

    class _Svc(AuctionMixin):
        def __init__(self, session):
            self._session = session

        @property
        def session(self):
            return self._session

        async def get_buyer_profile(self, phone):
            buyer = MagicMock()
            buyer.id = buyer_id
            return MagicMock(), buyer

    return _Svc(_FakeSession(auction))


class TestAddBidPhoto:
    def test_first_photo_is_appended(self):
        producer_id = uuid.uuid4()
        bid = _FakeRow(uuid.uuid4(), producer_id, images=[], extra={"producer_id": producer_id})
        svc = _bid_service(bid, producer_id)

        result = run(svc.add_bid_photo(phone=PHONE, bid_id=str(bid.id), image_url="https://x/a.jpg"))

        assert result["status"] == "success"
        assert bid.images == ["https://x/a.jpg"]

    def test_duplicate_url_is_a_no_op_success(self):
        producer_id = uuid.uuid4()
        bid = _FakeRow(uuid.uuid4(), producer_id, images=["https://x/a.jpg"])
        svc = _bid_service(bid, producer_id)

        result = run(svc.add_bid_photo(phone=PHONE, bid_id=str(bid.id), image_url="https://x/a.jpg"))

        assert result["status"] == "success"
        assert bid.images == ["https://x/a.jpg"]

    def test_replace_true_discards_previous_photos(self):
        producer_id = uuid.uuid4()
        bid = _FakeRow(uuid.uuid4(), producer_id, images=["https://x/old.jpg"])
        svc = _bid_service(bid, producer_id)

        result = run(svc.add_bid_photo(
            phone=PHONE, bid_id=str(bid.id), image_url="https://x/new.jpg", replace=True,
        ))

        assert result["status"] == "success"
        assert bid.images == ["https://x/new.jpg"]

    def test_photo_count_is_capped(self):
        producer_id = uuid.uuid4()
        existing = [f"https://x/{i}.jpg" for i in range(8)]
        bid = _FakeRow(uuid.uuid4(), producer_id, images=list(existing))
        svc = _bid_service(bid, producer_id)

        run(svc.add_bid_photo(phone=PHONE, bid_id=str(bid.id), image_url="https://x/new.jpg"))

        assert len(bid.images) == 8
        assert bid.images[-1] == "https://x/new.jpg"

    def test_a_bid_owned_by_someone_else_is_rejected(self):
        producer_id = uuid.uuid4()
        svc = _bid_service(None, producer_id)  # simule le WHERE producer_id=... ne matchant rien

        result = run(svc.add_bid_photo(phone=PHONE, bid_id=str(uuid.uuid4()), image_url="https://x/a.jpg"))

        assert result["status"] == "error"
        assert "introuvable" in result["message"].lower()


class TestAddAuctionPhoto:
    def test_first_photo_is_appended(self):
        buyer_id = uuid.uuid4()
        auction = _FakeRow(uuid.uuid4(), buyer_id, images=[])
        svc = _auction_service(auction, buyer_id)

        result = run(svc.add_auction_photo(phone=PHONE, auction_id=str(auction.id), image_url="https://x/a.jpg"))

        assert result["status"] == "success"
        assert auction.images == ["https://x/a.jpg"]

    def test_replace_true_discards_previous_photos(self):
        buyer_id = uuid.uuid4()
        auction = _FakeRow(uuid.uuid4(), buyer_id, images=["https://x/old.jpg"])
        svc = _auction_service(auction, buyer_id)

        result = run(svc.add_auction_photo(
            phone=PHONE, auction_id=str(auction.id), image_url="https://x/new.jpg", replace=True,
        ))

        assert result["status"] == "success"
        assert auction.images == ["https://x/new.jpg"]

    def test_an_auction_owned_by_someone_else_is_rejected(self):
        buyer_id = uuid.uuid4()
        svc = _auction_service(None, buyer_id)

        result = run(svc.add_auction_photo(phone=PHONE, auction_id=str(uuid.uuid4()), image_url="https://x/a.jpg"))

        assert result["status"] == "error"
        assert "introuvable" in result["message"].lower()
