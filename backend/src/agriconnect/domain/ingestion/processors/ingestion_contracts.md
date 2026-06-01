# Ingestion Contracts (Processors)

Version: 1.0
Last updated: 2026-04-12
Scope: `agriconnect.domain.ingestion.processors` and objects flowing to Worker/VectorDB.

## 1) Canonical Flow

1. Scraper/Orchestrator emits `RawDocument`.
2. `factory.py` resolves a processor using `source_type` or `source_engine`.
3. Worker calls `processor.process(raw_doc)`.
4. Processor returns `List[DocumentChunk]` normalized for embedding/upsert.
5. Worker adds embeddings and writes to Postgres/VectorDB.

## 2) RawDocument Contract (Input)

Canonical model (from `agriconnect.core.schemas.RawDocument`):

```json
{
  "id": "string, required, stable document id",
  "url": "string, required",
  "title": "string, required",
  "content_markdown": "string, required",
  "metadata": "object<string, any>, required",
  "language": "string|null, optional"
}
```

Input rules:
- `content_markdown` must be non-empty for processing.
- Content is markdown-first (HTML cleaning must not happen in processors).
- `metadata` must be a JSON object (never null).

## 3) DocumentChunk Contract (Output)

Canonical model (from `agriconnect.core.schemas.DocumentChunk`):

```json
{
  "chunk_id": "string, required, deterministic",
  "parent_doc_id": "string, required",
  "chunk_index": "integer, required, 0..N",
  "text_content": "string, required",
  "content_hash": "string, required, deterministic",
  "embedding_model": "string, default text-embedding-3-small",
  "embedding_dimensions": "integer, default 1536",
  "embedding": "array<number>|null, optional at processor stage",
  "metadata": "object<string, any>, required"
}
```

Normalized aliases (for docs/integration only):
- `content` => `text_content`
- `index` => `chunk_index`

Output invariants:
- `chunk_index` must be contiguous and start at 0.
- `text_content` must be non-empty after trim.
- `content_hash` must be deterministic and stable for same normalized content.
- `chunk_id` must be deterministic and derived from parent + hash (or stronger deterministic tuple).
- `metadata` must include inherited traceability fields (section 6).

## 4) BaseProcessor Interface Contract

Factory/Worker integration contract:

```python
class BaseProcessor(ABC):
    def process(self, document: RawDocument) -> List[DocumentChunk]:
        ...

    @abstractmethod
    def clean(self, document: RawDocument) -> RawDocument:
        ...

    @abstractmethod
    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        ...
```

Mandatory behavior:
- `process(raw_doc)` is the only public entrypoint called by Worker.
- `process()` must call `clean()` then `chunk()`.
- `clean()` must perform deterministic, lightweight normalization only.
- `chunk()` must return normalized `DocumentChunk` objects.
- Processors must not write to DB, fetch network resources, or embed vectors.

Recommended internal helper hooks (implementation detail, optional naming):
- `_split_text(text: str) -> list[str]`
- `_generate_metadata(raw: RawDocument, local: dict | None = None) -> dict`
- `_compute_content_hash(text: str, source_url: str) -> str`
- `_build_chunk_id(parent_doc_id: str, content_hash: str, chunk_index: int) -> str`

## 5) Idempotence Contract

### 5.1 Content hash

Normative algorithm (recommended for all processors):

1. Normalize URL:
- lower-case host
- strip trailing slash (except root)
- remove tracking query params if configured

2. Normalize text:
- trim
- collapse repeated whitespace to single spaces

3. Compute:
- `content_hash = sha256(normalized_source_url + "\n" + normalized_text)`

Why:
- avoids duplicate chunks from repeated runs
- prevents accidental collision between identical text coming from different source URLs
- aligns with DB dedupe using UNIQUE(`content_hash`)

### 5.2 Chunk id

Deterministic chunk identity:
- `chunk_id = sha256(parent_doc_id + ":" + content_hash + ":" + str(chunk_index))`

Notes:
- `chunk_id` is identity in vector stores.
- `content_hash` is dedupe lock in SQL upsert.

## 6) Mandatory Metadata Inheritance (RawDocument -> DocumentChunk)

Each produced chunk must include these keys in `chunk.metadata` when present on input:

Required traceability keys:
- `source_url` (default from `RawDocument.url` if missing)
- `source_id`
- `source_type`
- `source_engine`
- `scraper_name`
- `scraper_version`
- `collected_at` (scrape timestamp)
- `document_id` (copy of `RawDocument.id`)
- `document_title` (copy of `RawDocument.title`)

Strongly recommended operational keys:
- `language`
- `publication_date` or `date`
- `region` / `country` / `zone`
- `pipeline_run_id` (if available)
- `ingestion_contract_version` (example: `1.0`)

Processor-local enrichment examples:
- PDF: `chunk_size`, `split_from_node`, `split_index`
- News: `is_news: true`
- Weather: `is_weather: true`, normalized `region`, `date`

## 7) Processor-Specific Normalization Rules

### 7.1 PDF Processor
- Use markdown-aware splitting first.
- Re-split large chunks by paragraph/sentence if needed.
- Reindex final chunks after merge.
- Recompute `content_hash` and `chunk_id` after any merge/split.

### 7.2 News Processor
- Prefix title as markdown heading if not already present.
- Keep semantic context in each chunk (title-preserving split).
- Set `is_news: true`.

### 7.3 Weather Processor
- Prefix contextual header (`region`, `date`) into text payload.
- Keep short bulletins as single chunk.
- Set `is_weather: true`.

## 8) Worker/VectorDB Compatibility Requirements

To remain compatible with ingestion worker and storage:
- `DocumentChunk.embedding` may be `null` after processor stage.
- Worker is responsible for embedding generation and dimension checks.
- `content_hash` must be stable because SQL upsert deduplicates on it.
- `metadata` must stay JSON-serializable.

## 9) Minimal Compliance Example

```json
{
  "chunk_id": "9c58f...",
  "parent_doc_id": "a84be...",
  "chunk_index": 0,
  "text_content": "# Bulletin\n\nRain expected in northern zones...",
  "content_hash": "4f88a...",
  "embedding": null,
  "metadata": {
    "source_url": "https://example.org/bulletin.pdf",
    "source_id": "meteo_bulletin_ci",
    "source_type": "weather_bulletin",
    "source_engine": "weather_bulletin",
    "scraper_name": "WeatherScraper",
    "scraper_version": "2.0.0",
    "collected_at": "2026-04-12T10:00:00Z",
    "document_id": "a84be...",
    "document_title": "Weather Bulletin",
    "region": "north",
    "date": "2026-04-12",
    "is_weather": true,
    "ingestion_contract_version": "1.0"
  }
}
```

## 10) Factory Contract (Operational)

`factory.py` must:
- resolve processor key from `metadata.source_type` first
- fallback to `metadata.source_engine`
- reject unknown mappings with explicit exception

All processors registered in `PROCESSOR_REGISTRY` must satisfy this document.
