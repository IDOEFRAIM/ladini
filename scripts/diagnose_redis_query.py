import json
import traceback
import sys

from agriconnect.rag.redis_search_store import RedisSearchVectorStore
from sentence_transformers import SentenceTransformer


def main():
    try:
        store = RedisSearchVectorStore('redis://localhost:6379', dim=384)
        print('RedisSearchVectorStore connected')
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    try:
        embedder = SentenceTransformer('all-MiniLM-L6-v2')
        print('Embedder loaded')
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    query = 'physionomie du mil'
    q_emb = embedder.encode([query])[0].tolist()
    print('Computed embedding (len):', len(q_emb))

    try:
        res = store.query(q_emb, k=5)
        print('Found', len(res), 'results')
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    # Extra low-level checks: try raw FT.SEARCH variants
    try:
        client = store.client
        variants = [f"@vec:[KNN 5 $vec]", f"*=>[KNN 5 @vec $vec]"]
        import numpy as _np
        vec_blob = _np.asarray(q_emb, dtype=_np.float32).tobytes()
        for v in variants:
            try:
                print('\nTrying raw variant:', v)
                out = client.execute_command('FT.SEARCH', store.INDEX_NAME, v, 'PARAMS', '2', 'vec', vec_blob, 'RETURN', '2', 'text', 'meta', 'LIMIT', '0', '5')
                print('Raw returned:', out)
            except Exception as e:
                print('Raw variant failed:', e)
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    main()
