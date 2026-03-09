import traceback
import sys
import numpy as np
from agriconnect.rag.redis_search_store import RedisSearchVectorStore


def try_variants(store, q_emb):
    client = store.client
    vec_blob = np.asarray(q_emb, dtype=np.float32).tobytes()
    variants = [f"@vec:[KNN 5 $vec]", f"*=>[KNN 5 @vec $vec]"]
    for v in variants:
        for as_bytes in (False, True):
            try:
                qarg = v.encode('utf-8') if as_bytes else v
                print('\nVariant:', v, 'as_bytes=', as_bytes)
                out = client.execute_command('FT.SEARCH', store.INDEX_NAME, qarg, 'PARAMS', '2', 'vec', vec_blob, 'RETURN', '2', 'text', 'meta', 'LIMIT', '0', '5')
                print('Returned:', out)
            except Exception as e:
                print('Failed:', repr(e))


def main():
    try:
        store = RedisSearchVectorStore('redis://localhost:6379', dim=384)
        print('Connected')
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    # simple embedding
    q_emb = np.random.rand(384).astype(np.float32).tolist()
    try_variants(store, q_emb)


if __name__ == '__main__':
    main()
