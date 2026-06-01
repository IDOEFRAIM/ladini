import types
import pytest
from agriconnect.domain.ingestion import worker as worker_mod


class FakeConn:
    def __init__(self):
        self.calls = []

    def in_transaction(self):
        return False

    def begin(self):
        class Tx:
            def commit(self):
                pass

            def rollback(self):
                pass

        return Tx()

    def begin_nested(self):
        return self.begin()

    def execute(self, statement, params=None):
        s = str(statement)
        self.calls.append(s)
        # Simulate failure when trying ON CONFLICT (chunk_id)
        if "ON CONFLICT (chunk_id)" in s:
            raise Exception("column \"chunk_id\" does not exist")
        # otherwise "succeeds" by returning a dummy result
        class R:
            rowcount = 1

        return R()


class FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    class _cx:
        def __init__(self, rows):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, q, params=None):
            # Return an object with fetchall() for the rows query
            class Res:
                def __init__(self, rows):
                    self._rows = rows

                def fetchall(self):
                    return list(self._rows)

                def first(self):
                    return self._rows[0] if self._rows else None

            return Res(self._rows)

    def connect(self):
        return FakeEngine._cx(self._rows)


class DummyChunk:
    def __init__(self):
        self.embedding = [0.0] * 8
        self.chunk_index = 0
        self.text_content = "hello"
        self.metadata = {"document_id": "doc-1"}
        self.content_hash = "h1"
        self.chunk_id = "c1"


def make_worker_instance():
    # Bypass __init__ by creating empty instance and setting required attrs
    w = object.__new__(worker_mod.IngestionWorker)
    w.chunks_table = "ingestion.document_chunks"
    w.expected_embedding_dim = 8
    return w


def test_detect_unique_constraints_prefers_chunk_id():
    w = make_worker_instance()
    # Fake engine returns a unique constraint on chunk_id
    w.state_engine = FakeEngine(rows=[("chunk_id",)])
    w._detect_chunks_unique_constraints()
    assert getattr(w, "_preferred_conflict_target") == "chunk_id"


def test_upsert_falls_back_from_chunk_id_to_content_hash():
    w = make_worker_instance()
    # ensure state_engine is non-None so method proceeds (we pass conn explicitly)
    w.state_engine = object()
    # set preferred to chunk_id so first attempt will fail
    w._preferred_conflict_target = "chunk_id"
    # Prepare a single dummy chunk
    chunk = DummyChunk()
    chunks = [chunk]
    fake_conn = FakeConn()

    # Call the upsert with conn=FakeConn() and ensure it does not raise
    # and that it attempted the chunk_id candidate first and then content_hash
    w._upsert_chunks_postgres(chunks, s3_key="k", file_hash="f", conn=fake_conn, with_embeddings=False)

    joined = "\n".join(fake_conn.calls)
    assert "ON CONFLICT (chunk_id)" in joined
    assert "ON CONFLICT (content_hash)" in joined
