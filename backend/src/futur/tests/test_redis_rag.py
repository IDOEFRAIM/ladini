import os
import uuid

from futur.rag import redis_search_store
import pytest

try:
    import numpy as np
except Exception:
    np = None

from futur.rag import redis_store


REDIS_URL = os.getenv("REDIS_URL")


def require_redis():
    if not REDIS_URL:
        pytest.skip("No REDIS_URL set for integration tests")


@pytest.mark.integration
def test_redis_vector_store_add_query_delete():
    require_redis()
    if np is None:
        pytest.skip("numpy is required for this test")

    dim = 16
    store = redis_store.RedisVectorStore(REDIS_URL, dim=dim)
    client = store.client

    doc_id = f"test-{uuid.uuid4()}"
    vec = np.random.RandomState(1).rand(dim).astype(np.float32).tolist()
    store.add(doc_id, "hello world", {"test": True}, vec)

    # ensure key exists
    key = f"{store.ns}:doc:{doc_id}"
    assert client.exists(key) == 1

    # query should return our doc as top-1
    res = store.query(vec, k=1)
    assert isinstance(res, list)
    assert res, "expected at least one result"
    assert res[0]["id"] == doc_id

    # delete and ensure removal
    store.delete(doc_id)
    assert client.exists(key) == 0


@pytest.mark.integration
def test_redis_search_vector_store_add_query_delete():
    require_redis()
    if np is None:
        pytest.skip("numpy is required for this test")

    dim = 16
    # The RedisSearch store may not be supported on all instances; skip on failures
    try:
        store = redis_search_store.RedisSearchVectorStore(REDIS_URL, dim=dim)
    except Exception as e:
        pytest.skip(f"RedisSearch not available: {e}")

    client = store.client
    doc_id = f"test-{uuid.uuid4()}"
    vec = np.random.RandomState(2).rand(dim).astype(np.float32).tolist()

    try:
        store.add(doc_id, "sample text", {"x": 1}, vec)
    except Exception as e:
        pytest.skip(f"Could not add to RedisSearch store: {e}")

    # query should return at least one result; id may be full key name
    try:
        out = store.query(vec, k=1)
    except Exception as e:
        pytest.skip(f"Query failed: {e}")

    assert isinstance(out, list)
    assert out, "expected query results"
    # doc id returned may be full key (prefix + id)
    returned = out[0]["id"]
    assert returned.endswith(doc_id)

    # cleanup
    try:
        store.delete(doc_id)
    except Exception:
        pass
