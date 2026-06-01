from __future__ import annotations
import json
from pathlib import Path
import sys
import unicodedata
import io
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.rag.components import get_vector_store, init_settings, get_embedding_model
from agriconnect.rag.config import DB_DIR

from llama_index.core import Document
from llama_index.core import StorageContext, VectorStoreIndex

EVAL_PATH = Path("tmp/eval_retrieval.json")


def normalize_text(s: str) -> str:
    s = s or ""
    s = " ".join(s.split())
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s


def read_document(uri: str) -> str:
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()

    def _text_from_pdf_bytes(b: bytes) -> str:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(b))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        return "\n".join(pages)

    if scheme in ("http", "https"):
        import requests
        resp = requests.get(uri, timeout=30)
        resp.raise_for_status()
        if "application/pdf" in (resp.headers.get("content-type") or "") or uri.lower().endswith(".pdf"):
            return _text_from_pdf_bytes(resp.content)
        if "application/json" in (resp.headers.get("content-type") or "") or uri.lower().endswith(".json"):
            data = resp.json()
            if isinstance(data, dict):
                for k in ("text","content","body","markdown"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v
            return json.dumps(data, ensure_ascii=False)
        return resp.text

    if scheme == "s3":
        import boto3
        s3 = boto3.client("s3")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        with io.BytesIO() as buf:
            s3.download_fileobj(bucket, key, buf)
            b = buf.getvalue()
        if key.lower().endswith(".pdf"):
            return _text_from_pdf_bytes(b)
        if key.lower().endswith(".json"):
            data = json.loads(b.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                for k in ("text","content","body","markdown"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v
            return json.dumps(data, ensure_ascii=False)
        return b.decode("utf-8", errors="ignore")

    p = Path(uri)
    suffix = p.suffix.lower()
    if suffix == ".txt":
        return p.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in ("text","content","body","markdown"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        return json.dumps(data, ensure_ascii=False)
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        return "\n".join(pages)
    return p.read_text(encoding="utf-8", errors="ignore")


def main():
    items = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    docs = []
    for it in items:
        src = str(it.get("source"))
        uri = str(it.get("doc_path"))
        try:
            text = read_document(uri)
        except Exception as e:
            print(f"Failed to read {uri}: {e}")
            text = ""
        if not text:
            continue
        # create a single document per source with metadata
        docs.append(Document(text=text, doc_id=src, extra_info={"source_id": src}))

    if not docs:
        print("No documents to index")
        return 1

    # Ensure DB_DIR exists
    DB_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize settings (embed model, chunk size)
    init_settings()

    # Build vector store and storage context, explicitly using DB_DIR as persist_dir
    vector_store = get_vector_store()
    storage_context = StorageContext.from_defaults(vector_store=vector_store, persist_dir=str(DB_DIR))

    # Create index from documents
    print(f"Creating VectorStoreIndex with {len(docs)} documents; persisting to {DB_DIR}")
    index = VectorStoreIndex.from_documents(docs, storage_context=storage_context)

    # Persist index and storage context to disk so AgileRetriever can load it
    try:
        # prefer using index.storage_context.persist if available
        sc = getattr(index, "storage_context", None) or storage_context
        persist_fn = getattr(sc, "persist", None)
        if callable(persist_fn):
            persist_fn(persist_dir=str(DB_DIR))
        else:
            # fallback: try storage_context.persist() without args
            try:
                sc.persist()
            except Exception:
                pass
    except Exception as e:
        print("Warning: failed to persist storage context:", e)

    print("Index created and persisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
