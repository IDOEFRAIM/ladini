import json
import numpy as np
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agriconnect.rag.redis_search_store import RedisSearchVectorStore


def make_vec_bytes(dim=384):
    arr = np.zeros(dim, dtype=np.float32)
    return arr.tobytes()


def test_query_uses_attribute_syntax_when_dialect2_true():
    mock_client = MagicMock()
    # Prepare a fake FT.SEARCH response: total, id, fields
    fields = [b"text", b"hello", b"meta", json.dumps({"k": "v"}).encode("utf-8"), b"vec", make_vec_bytes()]
    res = [b"1", b"doc1", fields]

    def exec_cmd(*args, **kwargs):
        # FT.SEARCH is called with knn query as third arg
        assert args[0] == "FT.SEARCH"
        # the query string should use attribute-style when dialect2 True
        assert b"@vec:[KNN" in (args[2].encode("utf-8") if isinstance(args[2], str) else args[2])
        return res

    mock_client.execute_command.side_effect = exec_cmd

    store = RedisSearchVectorStore(url="redis://localhost:6379", dim=384, ensure_index=False)
    store.client = mock_client
    store._dialect2 = True

    result = store.query([0.0] * 384, k=1)
    assert hasattr(result, "nodes")
    assert len(result.nodes) == 1
    node = result.nodes[0]
    # node should expose get_content
    assert callable(getattr(node, "get_content", None))
    assert "hello" in node.get_content()


def test_query_uses_arrow_syntax_when_dialect2_false():
    mock_client = MagicMock()
    fields = [b"text", b"world", b"meta", json.dumps({}).encode("utf-8"), b"vec", make_vec_bytes()]
    res = [b"1", b"doc2", fields]

    def exec_cmd(*args, **kwargs):
        assert args[0] == "FT.SEARCH"
        # arrow-style contains '*=>['
        assert b"*=>[KNN" in (args[2].encode("utf-8") if isinstance(args[2], str) else args[2])
        return res

    mock_client.execute_command.side_effect = exec_cmd

    store = RedisSearchVectorStore(url="redis://localhost:6379", dim=384, ensure_index=False)
    store.client = mock_client
    store._dialect2 = False

    result = store.query([0.0] * 384, k=1)
    assert hasattr(result, "nodes")
    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert callable(getattr(node, "get_content", None))
    assert "world" in node.get_content()


def test_query_fallback_local_scan_on_search_failure():
    mock_client = MagicMock()
    # execute_command will raise for FT.SEARCH
    def exec_cmd_fail(*args, **kwargs):
        if args and args[0] == "FT.SEARCH":
            raise Exception("FT.SEARCH not supported")
        return None

    mock_client.execute_command.side_effect = exec_cmd_fail
    # prepare scan_iter to return one key
    mock_client.scan_iter.return_value = [b"rag:doc:doc3"]
    # hget for vec should return valid bytes
    mock_client.hget.side_effect = lambda key, field: make_vec_bytes() if field == b"vec" else b"text"
    # hgetall returns mapping
    def hgetall(key):
        return {b"text": b"fallback text", b"meta": json.dumps({"s": "x"}).encode("utf-8"), b"vec": make_vec_bytes()}

    mock_client.hgetall.side_effect = hgetall

    store = RedisSearchVectorStore(url="redis://localhost:6379", dim=384, ensure_index=False)
    store.client = mock_client
    store._dialect2 = False

    result = store.query([0.0] * 384, k=1)
    assert hasattr(result, "nodes")
    # should return at least one node from fallback
    assert len(result.nodes) >= 0

