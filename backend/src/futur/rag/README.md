# AgriConnect RAG System

This directory contains the advanced RAG implementation designed for AgriConnect, combining static agronomic knowledge with dynamic weather data.

## Architecture

1.  **Ingestor (`ingestor.py`)**:
    - **Contextual Chunking**: Adds headers to chunks (e.g., "Document Type: Weather Bulletin") to prevent context loss.
    - **Graph/Metadata Extraction**: Extracts entities (Crops, Diseases) during ingestion to simulate GraphRAG connections.
    - **Vector Store**: Uses FAISS (via LlamaIndex) for persistence.

2.  **Retriever (`retriever.py`)**:
    - **HyDe (Hypothetical Document Embeddings)**: Generates a hypothetical expert answer to search for semantic matches, handling varied user language (farmers vs experts).
    - **Reranker**: Re-scores results using a Cross-Encoder (if available) to ensure the top results are relevant to the *current* query context.

3.  **Orchestrator (`orchestrator.py`)**:
    - **Agentic RAG**: Acts as a router.
    - **Weather Integration**: Dynamic check of weather conditions before consulting the knowledge base.
    - **Logic**: IF `needs_weather` THEN `check_weather()` -> `refine_rag_query(weather_context)` -> `synthesize_answer`.

## Usage

### 1. Ingestion (Run once or when documents change)
Place your PDFs in `backend/sources/raw_data/` and run:

```bash
python -m backend.rag.ingestor
```

### 2. Querying (Usage in API)
Use the `RAGOrchestrator` to handle user queries.

```python
from backend.rag.orchestrator import RAGOrchestrator

rag = RAGOrchestrator()
response = rag.process_query("Dois-je traiter mes tomates aujourd'hui ?")
print(response)
```

## Configuration
Settings are in `config.py` and `components.py`.
- Ensure `GROQ_API_KEY` or `AGRICONNECT_APIKEY` is set in `.env` for the LLM to work.

## Quick verification & maintenance

Run these scripts from the repository root to sanity-check RAG components and indexes:

```powershell
# run a lightweight RAG stress / integrity check (tmp/run_rag_stress.py uses RAW_DATA_DIR and embedding dim)
python tmp/run_rag_stress.py

# test connectivity to vector store (redis/pgvector)
python backend/scripts/test_vector_search.py

# trigger a gold index rebuild (if you change ingestion/indexing)
python backend/scripts/trigger_gold_rebuild.py
```

Notes:
- `RAW_DATA_DIR` and `RAG_EMBEDDING_DIM` (see `core/settings.py`) must match your index and embedding model.
- If no index is present, `retriever` will raise `No index found in storage` — initialize or populate the index before running queries.
