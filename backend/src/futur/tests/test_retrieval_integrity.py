import os
import json
import pytest
import numpy as np

from futur.rag.providers.redis_search_provider import RedisSearchProvider
from futur.rag.components import get_embedding_model
from futur.rag.errors import DimensionMismatchError
from agriconnect.core.settings import settings


def _decode_bytes(b):
    if b is None:
        return ""
    if isinstance(b, (bytes, bytearray)):
        return b.decode("utf-8", errors="ignore")
    return str(b)


def _read_doc_text(provider, doc_id: str) -> str:
    key = provider._doc_key(doc_id)
    # Ingestion writes both `content` and `text` depending on the store/provider path.
    content = _decode_bytes(provider.client.hget(key, "content"))
    if content:
        return content
    return _decode_bytes(provider.client.hget(key, "text"))


@pytest.mark.integration
def test_retrieval_integrity_top3():
    redis_url = os.getenv("AGRICONNECT_REDIS_URL", settings.REDIS_URL)
    provider = RedisSearchProvider(url=redis_url, dim=int(settings.RAG_EMBEDDING_DIM), ensure_index=False, allow_degraded_mode=True, decode_responses=False)

    # Find a candidate document in Redis
    ids = list(provider.client.smembers(provider._docs_key))
    assert ids, "No documents found in Redis to validate retrieval integrity"

    gold_id = None
    gold_text = None
    for id_raw in ids:
        doc_id = id_raw.decode("utf-8", errors="ignore") if isinstance(id_raw, (bytes, bytearray)) else str(id_raw)
        key = provider._doc_key(doc_id)
        content = _read_doc_text(provider, doc_id)
        metadata_raw = provider.client.hget(key, "metadata")
        try:
            metadata = json.loads(_decode_bytes(metadata_raw) or "{}")
        except Exception:
            metadata = {}
        if metadata.get("is_indexed") and content:
            gold_id = doc_id
            gold_text = content
            break

    # Fallback: use first doc if none marked is_indexed
    if gold_id is None:
        for id_raw in ids:
            doc_id = id_raw.decode("utf-8", errors="ignore") if isinstance(id_raw, (bytes, bytearray)) else str(id_raw)
            content = _read_doc_text(provider, doc_id)
            if content:
                gold_id = doc_id
                gold_text = content
                break

    assert gold_text, "Gold document has no content"

    # Extract a complex-ish sentence (first 200 chars)
    query_sentence = gold_text.strip().split(".")[0][:200]
    assert query_sentence, "Could not extract a query sentence from gold document"

    emb_model = get_embedding_model()
    try:
        q_vec = np.asarray(emb_model.get_query_embedding(query_sentence) or [], dtype=np.float32)
    except DimensionMismatchError:
        pytest.skip("Embedding model returned wrong dimension; check RAG_EMBEDDING_DIM")

    assert q_vec.size == int(settings.RAG_EMBEDDING_DIM)

    # Run retrieval (allow degraded manual similarity)
    from futur.rag.core.models import QueryBundle

    bundle = QueryBundle(vector=q_vec.tolist(), top_k=3, filters={"is_indexed": True}, text_query=query_sentence)
    results = provider.query(bundle)
    top_ids = [str(r.id) for r in results[:3]]

    assert gold_id in top_ids, f"Gold document {gold_id} not in top 3 retrieval results: {top_ids}"
