import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg2
from psycopg2.extras import Json

from agriconnect.core.settings import settings
import tempfile
from urllib.parse import urlparse


DOCSTORE_PATH = Path(__file__).resolve().parents[1] / "rag_db" / "docstore.json"


def _extract_node_payload(raw_node: Any) -> Dict[str, Any]:
    if isinstance(raw_node, dict) and "__data__" in raw_node and isinstance(raw_node["__data__"], dict):
        return raw_node["__data__"]
    if isinstance(raw_node, dict):
        return raw_node
    return {}


def _extract_text(node_data: Dict[str, Any]) -> str:
    text = node_data.get("text")
    if isinstance(text, str) and text.strip():
        return text

    text_resource = node_data.get("text_resource")
    if isinstance(text_resource, dict):
        tr = text_resource.get("text")
        if isinstance(tr, str) and tr.strip():
            return tr

    content = node_data.get("content") or node_data.get("excerpt") or node_data.get("body")
    if isinstance(content, str) and content.strip():
        return content

    return ""


def _safe_parse_date(raw: Any) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def _safe_parse_timestamp(raw: Any) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat()
    except Exception:
        return None


def _embedding_to_pgvector_literal(embedding: Any) -> Optional[str]:
    if not isinstance(embedding, list) or not embedding:
        return None
    try:
        vals = [float(x) for x in embedding]
    except Exception:
        return None
    return "[" + ",".join(str(v) for v in vals) + "]"


def _build_chunk_rows(docstore: Dict[str, Any], source_file: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    nodes_map = docstore.get("docstore/data") or {}

    for node_id, raw_node in nodes_map.items():
        node_data = _extract_node_payload(raw_node)
        metadata = node_data.get("metadata") if isinstance(node_data.get("metadata"), dict) else {}

        content = _extract_text(node_data).strip()
        if not content:
            continue

        doc_ref = (
            metadata.get("file_path")
            or metadata.get("source")
            or metadata.get("filename")
            or metadata.get("file_name")
            or node_id
        )
        doc_type = str(metadata.get("doc_type") or "legacy_rag")
        category = metadata.get("category")
        zone_name = metadata.get("zone") or metadata.get("zone_name") or metadata.get("region") or metadata.get("région")
        source_uri = metadata.get("source") or metadata.get("file_path") or source_file

        forecast_date = _safe_parse_date(metadata.get("forecast_date") or metadata.get("date"))
        valid_until = _safe_parse_timestamp(metadata.get("valid_until"))

        content_md5 = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
        embedding_literal = _embedding_to_pgvector_literal(node_data.get("embedding"))

        rows.append(
            {
                "doc_ref": str(doc_ref),
                "doc_type": doc_type,
                "category": str(category) if category is not None else None,
                "zone_name": str(zone_name) if zone_name is not None else None,
                "content": content,
                "content_md5": content_md5,
                "embedding_literal": embedding_literal,
                "forecast_date": forecast_date,
                "valid_until": valid_until,
                "metadata": Json(metadata),
                "source_uri": str(source_uri) if source_uri is not None else None,
            }
        )

    return rows


def _compute_embeddings_for_rows(rows: List[Dict[str, Any]], batch_size: int = 64) -> None:
    """Compute embeddings for rows missing them and set `embedding_literal` in-place.

    Attempts to use project's embedding model (llama_index/HuggingFace) and
    falls back to `sentence_transformers` if available.
    """
    texts_to_embed = []
    indices = []
    for i, r in enumerate(rows):
        if not r.get("embedding_literal"):
            texts_to_embed.append(r.get("content") or "")
            indices.append(i)

    if not texts_to_embed:
        return

    def _try_llama_model(texts: List[str]):
        try:
            from agriconnect.rag.components import get_embedding_model

            model = get_embedding_model()
            if hasattr(model, "embed_documents"):
                return model.embed_documents(texts)
            if hasattr(model, "embed_query"):
                return [model.embed_query(t) for t in texts]
        except Exception:
            return None

    def _try_sentence_transformers(texts: List[str]):
        try:
            from sentence_transformers import SentenceTransformer
            from agriconnect.core.settings import settings as app_settings

            model_name = getattr(app_settings, "EMBEDDING_MODEL", None) or "sentence-transformers/all-MiniLM-L6-v2"
            st = SentenceTransformer(model_name)
            emb = st.encode(texts, show_progress_bar=False)
            return emb.tolist() if hasattr(emb, "tolist") else [list(map(float, e)) for e in emb]
        except Exception:
            return None

    start = 0
    while start < len(texts_to_embed):
        end = min(start + batch_size, len(texts_to_embed))
        batch_texts = texts_to_embed[start:end]
        emb = _try_llama_model(batch_texts)
        if emb is None:
            emb = _try_sentence_transformers(batch_texts)

        if emb is None:
            raise RuntimeError("No embedding backend available (install llama_index/huggingface or sentence-transformers)")

        for j, vector in enumerate(emb):
            row_idx = indices[start + j]
            rows[row_idx]["embedding_literal"] = _embedding_to_pgvector_literal(vector)

        start = end


def _insert_batches(conn, rows: Iterable[Dict[str, Any]], batch_size: int) -> (int, int):
    insert_no_embedding = """
        INSERT INTO agri_vector.document_chunks (
            doc_ref, doc_type, category, zone_name, content, content_md5,
            forecast_date, valid_until, metadata, source_uri, chunk_version, is_active
        ) VALUES (
            %(doc_ref)s, %(doc_type)s, %(category)s, %(zone_name)s, %(content)s, %(content_md5)s,
            %(forecast_date)s, %(valid_until)s, %(metadata)s, %(source_uri)s, 1, TRUE
        )
        ON CONFLICT (doc_ref, content_md5, chunk_version)
        DO NOTHING
        RETURNING chunk_id
    """
    insert_with_embedding = """
        INSERT INTO agri_vector.document_chunks (
            doc_ref, doc_type, category, zone_name, content, content_md5,
            embedding, forecast_date, valid_until, metadata, source_uri, chunk_version, is_active
        ) VALUES (
            %(doc_ref)s, %(doc_type)s, %(category)s, %(zone_name)s, %(content)s, %(content_md5)s,
            %(embedding_literal)s::vector, %(forecast_date)s, %(valid_until)s, %(metadata)s, %(source_uri)s, 1, TRUE
        )
        ON CONFLICT (doc_ref, content_md5, chunk_version)
        DO NOTHING
        RETURNING chunk_id
    """

    inserted = 0
    skipped = 0
    with conn.cursor() as cur:
        batch: List[Dict[str, Any]] = []
        for row in rows:
            batch.append(row)
            if len(batch) < batch_size:
                continue
            for item in batch:
                try:
                    if item.get("embedding_literal"):
                        cur.execute(insert_with_embedding, item)
                    else:
                        cur.execute(insert_no_embedding, item)

                    returned = cur.fetchone()
                    was_inserted = bool(returned)
                    # fallback: if RETURNING did not return but row exists, count it
                    if not was_inserted:
                        cur.execute(
                            "SELECT chunk_id FROM agri_vector.document_chunks WHERE doc_ref=%s AND content_md5=%s AND chunk_version=1",
                            (item.get("doc_ref"), item.get("content_md5")),
                        )
                        returned = cur.fetchone()
                        was_inserted = bool(returned)

                    debug_out = {
                        "doc_ref": item.get("doc_ref"),
                        "content_md5": item.get("content_md5"),
                        "has_embedding": bool(item.get("embedding_literal")),
                        "inserted": was_inserted,
                        "returned": returned,
                    }
                    print(json.dumps({"debug":"insert_attempt", **debug_out}, ensure_ascii=False))

                    if was_inserted:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    err_out = {
                        "doc_ref": item.get("doc_ref"),
                        "content_md5": item.get("content_md5"),
                        "error": str(e),
                    }
                    print(json.dumps({"debug":"insert_error", **err_out}, ensure_ascii=False))
                    skipped += 1
            batch = []

        for item in batch:
            try:
                if item.get("embedding_literal"):
                    cur.execute(insert_with_embedding, item)
                else:
                    cur.execute(insert_no_embedding, item)

                returned = cur.fetchone()
                was_inserted = bool(returned)
                if not was_inserted:
                    cur.execute(
                        "SELECT chunk_id FROM agri_vector.document_chunks WHERE doc_ref=%s AND content_md5=%s AND chunk_version=1",
                        (item.get("doc_ref"), item.get("content_md5")),
                    )
                    returned = cur.fetchone()
                    was_inserted = bool(returned)

                debug_out = {
                    "doc_ref": item.get("doc_ref"),
                    "content_md5": item.get("content_md5"),
                    "has_embedding": bool(item.get("embedding_literal")),
                    "inserted": was_inserted,
                    "returned": returned,
                }
                print(json.dumps({"debug":"insert_attempt", **debug_out}, ensure_ascii=False))

                if was_inserted:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                err_out = {
                    "doc_ref": item.get("doc_ref"),
                    "content_md5": item.get("content_md5"),
                    "error": str(e),
                }
                print(json.dumps({"debug":"insert_error", **err_out}, ensure_ascii=False))
                skipped += 1
    return inserted, skipped


def _write_import_log(conn, source_file: str, imported_rows: int, status: str, candidate_count: Optional[int] = None) -> None:
    status_msg = status if candidate_count is None else f"{status} (candidates={candidate_count})"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agri_vector.legacy_rag_import_log (source_file, imported_rows, status)
            VALUES (%s, %s, %s)
            """,
            (source_file, imported_rows, status_msg),
        )


def run_migration(docstore_path: Path = DOCSTORE_PATH, dry_run: bool = False, batch_size: int = 500, compute_embeddings: bool = False, limit: Optional[int] = None) -> Dict[str, Any]:
    if not docstore_path.exists():
        raise FileNotFoundError(f"docstore file not found: {docstore_path}")

    with docstore_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    rows = _build_chunk_rows(payload, source_file=str(docstore_path))

    # apply optional limit (useful for testing small batches)
    if limit is not None and isinstance(limit, int) and limit > 0:
        rows = rows[:limit]

    if dry_run:
        return {
            "status": "DRY_RUN",
            "docstore": str(docstore_path),
            "rows_candidate": len(rows),
            "rows_inserted": 0,
        }

    # option: compute missing embeddings before insertion
    if compute_embeddings:
        _compute_embeddings_for_rows(rows, batch_size=64)
        # diagnostic: how many rows now have embeddings
        computed = sum(1 for r in rows if r.get("embedding_literal"))
        print(json.dumps({"debug": "computed_embeddings", "count": computed}, ensure_ascii=False))

    db_url = settings.DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL not configured")

    conn = psycopg2.connect(db_url)
    try:
        conn.autocommit = False
        inserted, skipped = _insert_batches(conn, rows, batch_size=batch_size)
        # write import log with candidate count and commit
        _write_import_log(conn, str(docstore_path), inserted, "DONE", candidate_count=len(rows))
        # diagnostic summary
        print(json.dumps({"debug": "import_summary", "candidates": len(rows), "inserted": inserted, "skipped": skipped}, ensure_ascii=False))
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            conn.autocommit = True
            _write_import_log(conn, str(docstore_path), 0, "FAILED", candidate_count=len(rows))
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return {
        "status": "DONE",
        "docstore": str(docstore_path),
        "rows_candidate": len(rows),
        "rows_inserted": inserted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate rag_db/docstore.json into agri_vector.document_chunks")
    parser.add_argument("--docstore", type=str, default=str(DOCSTORE_PATH), help="Path to docstore.json")
    parser.add_argument("--dry-run", action="store_true", help="Only compute candidate rows")
    parser.add_argument("--batch-size", type=int, default=500, help="Insert batch size")
    parser.add_argument("--compute-embeddings", action="store_true", help="Compute embeddings for rows missing them before inserting")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows to process (for testing)")
    args = parser.parse_args()
    docstore_arg = args.docstore
    tmp_file_path = None
    # Support s3:// URI for docstore
    if isinstance(docstore_arg, str) and docstore_arg.startswith("s3://"):
        try:
            import boto3
            parsed = urlparse(docstore_arg)
            bucket = parsed.netloc
            key = parsed.path.lstrip('/')
            s3 = boto3.client('s3')
            tmpf = tempfile.NamedTemporaryFile(prefix='docstore_', suffix='.json', delete=False)
            s3.download_fileobj(bucket, key, tmpf)
            tmpf.flush()
            tmp_file_path = tmpf.name
            tmpf.close()
            docstore_path = Path(tmp_file_path)
        except Exception as e:
            print(json.dumps({"error": f"Failed to download docstore from S3: {e}"}, ensure_ascii=False))
            raise
    else:
        docstore_path = Path(docstore_arg)

    result = run_migration(
        docstore_path=docstore_path,
        dry_run=bool(args.dry_run),
        batch_size=max(1, int(args.batch_size)),
        compute_embeddings=bool(getattr(args, "compute_embeddings", False)),
        limit=getattr(args, "limit", None),
    )

    # Cleanup temp file if downloaded
    if tmp_file_path:
        try:
            Path(tmp_file_path).unlink()
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
