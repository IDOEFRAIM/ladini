#!/usr/bin/env python3
"""Import precomputed embeddings into agri_vector.document_chunks.

Supports JSONL or a JSON array with entries containing either:
- `chunk_id` and `embedding` OR
- `doc_ref`, `content_md5`, optional `chunk_version` (default 1), and `embedding`.

Each `embedding` should be a list of floats. The script updates the `embedding`
column using pgvector literal syntax (e.g. "[0.1,0.2,...]").
"""
import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import psycopg2

try:
    from agriconnect.core.settings import settings
    DATABASE_URL = getattr(settings, "DATABASE_URL", None)
except Exception:
    DATABASE_URL = None

if not DATABASE_URL:
    DATABASE_URL = os.environ.get("DATABASE_URL")


def _embedding_to_literal(emb: Any) -> Optional[str]:
    if not isinstance(emb, list) or not emb:
        return None
    try:
        vals = [float(x) for x in emb]
    except Exception:
        return None
    return "[" + ",".join(str(v) for v in vals) + "]"


def _read_embeddings_file(path: Path) -> Iterable[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    text = text.strip()
    # try JSON array first
    if text.startswith("["):
        data = json.loads(text)
        for obj in data:
            yield obj
        return
    # otherwise assume JSONL
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        yield json.loads(line)


def import_embeddings(file_path: Path, batch_size: int = 500, write_import_log: bool = True) -> Dict[str, int]:
    if not file_path.exists():
        raise FileNotFoundError(f"embeddings file not found: {file_path}")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured (set env or agriconnect settings)")

    conn = psycopg2.connect(DATABASE_URL)
    updated = 0
    skipped = 0

    update_by_chunk = """
        UPDATE agri_vector.document_chunks
        SET embedding = %(embedding_literal)s::vector
        WHERE chunk_id = %(chunk_id)s
        RETURNING chunk_id
    """

    update_by_doc = """
        UPDATE agri_vector.document_chunks
        SET embedding = %(embedding_literal)s::vector
        WHERE doc_ref = %(doc_ref)s AND content_md5 = %(content_md5)s AND chunk_version = %(chunk_version)s
        RETURNING chunk_id
    """

    try:
        with conn.cursor() as cur:
            batch: List[Dict[str, Any]] = []
            for obj in _read_embeddings_file(file_path):
                emb_literal = _embedding_to_literal(obj.get("embedding") or obj.get("vector") or obj.get("emb"))
                if not emb_literal:
                    print(json.dumps({"debug": "skip_invalid_embedding", "obj": {k: obj.get(k) for k in ("chunk_id","doc_ref","content_md5")}}))
                    skipped += 1
                    continue

                entry = {
                    "embedding_literal": emb_literal,
                    "chunk_id": obj.get("chunk_id"),
                    "doc_ref": obj.get("doc_ref"),
                    "content_md5": obj.get("content_md5"),
                    "chunk_version": int(obj.get("chunk_version") or 1),
                }
                batch.append(entry)

                if len(batch) >= batch_size:
                    for item in batch:
                        try:
                            if item.get("chunk_id"):
                                cur.execute(update_by_chunk, item)
                            else:
                                cur.execute(update_by_doc, item)
                            returned = cur.fetchone()
                            success = bool(returned)
                            print(json.dumps({"debug": "update_attempt", "chunk_id": item.get("chunk_id"), "doc_ref": item.get("doc_ref"), "content_md5": item.get("content_md5"), "updated": success}))
                            if success:
                                updated += 1
                            else:
                                skipped += 1
                        except Exception as e:
                            print(json.dumps({"debug": "update_error", "error": str(e), "doc_ref": item.get("doc_ref"), "content_md5": item.get("content_md5")}))
                            skipped += 1
                    conn.commit()
                    batch = []

            # flush remainder
            for item in batch:
                try:
                    if item.get("chunk_id"):
                        cur.execute(update_by_chunk, item)
                    else:
                        cur.execute(update_by_doc, item)
                    returned = cur.fetchone()
                    success = bool(returned)
                    print(json.dumps({"debug": "update_attempt", "chunk_id": item.get("chunk_id"), "doc_ref": item.get("doc_ref"), "content_md5": item.get("content_md5"), "updated": success}))
                    if success:
                        updated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(json.dumps({"debug": "update_error", "error": str(e), "doc_ref": item.get("doc_ref"), "content_md5": item.get("content_md5")}))
                    skipped += 1
            conn.commit()

        # optional: write a simple import log row
        if write_import_log:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agri_vector.legacy_rag_import_log (source_file, imported_rows, status) VALUES (%s, %s, %s)",
                    (str(file_path), updated, f"EMBeddings_IMPORTED (updated={updated}, skipped={skipped})"),
                )
                conn.commit()

    finally:
        conn.close()

    return {"updated": updated, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import precomputed embeddings into PGVector table")
    parser.add_argument("--embeddings", required=True, help="Path to embeddings file (JSONL or JSON array)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--no-log", action="store_true", help="Do not write an import log row")
    args = parser.parse_args()

    path = Path(args.embeddings)
    result = import_embeddings(path, batch_size=max(1, args.batch_size), write_import_log=not args.no_log)
    print(json.dumps({"status": "DONE", **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
