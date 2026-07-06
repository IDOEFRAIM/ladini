from typing import List

from llama_index.core.schema import Document as LlamaDocument
from llama_index.core.node_parser import MarkdownNodeParser

from agriconnect.core.schemas import RawDocument, DocumentChunk
from futur.ingestion.processors.base_processor import BaseProcessor


class PDFProcessor(BaseProcessor):
    """
    Fallback processor for PDF-derived text.

    Behaviour:
    - Expects `content_markdown` (Markdown) from the scraper.
    - Uses `MarkdownNodeParser` to respect heading hierarchy where present.
    - Generates deterministic content-based SHA-256 `chunk_id`s.
    """

    def __init__(self, config: dict | None = None):
        self.parser = MarkdownNodeParser()
        cfg = config or {}
        # Prefer explicit config dict, otherwise fall back to central settings
        try:
            from agriconnect.core.settings import settings
        except Exception:
            settings = None

        default_max = (
            int(cfg.get("MAX_CHUNK_CHARS", cfg.get("max_chunk_chars")))
            if (cfg.get("MAX_CHUNK_CHARS") or cfg.get("max_chunk_chars") is not None)
            else (getattr(settings, "PDF_MAX_CHUNK_CHARS", 1500) if settings is not None else 1500)
        )
        default_min = (
            int(cfg.get("MIN_MERGE_CHARS", cfg.get("min_merge_chars")))
            if (cfg.get("MIN_MERGE_CHARS") or cfg.get("min_merge_chars") is not None)
            else (getattr(settings, "PDF_MIN_MERGE_CHARS", 50) if settings is not None else 50)
        )

        self.MAX_CHUNK_CHARS = default_max
        self.MIN_MERGE_CHARS = default_min

    def process(self, document: RawDocument) -> List[DocumentChunk]:
        return super().process(document)

    def clean(self, document: RawDocument) -> RawDocument:
        text = document.content_markdown or ""
        # Minimal normalization only; extraction cleanup belongs to scraper stage.
        document.content_markdown = text.strip()
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        if not (document.content_markdown or "").strip():
            return []

        llama_doc = LlamaDocument(text=document.content_markdown, metadata={"parent_doc_id": document.id})
        nodes = self.parser.get_nodes_from_documents([llama_doc])

        chunks: List[DocumentChunk] = []
        # Use instance-configured maximum chunk size
        MAX_CHUNK_CHARS = int(self.MAX_CHUNK_CHARS)

        def split_text_to_chunks(text: str, max_chars: int):
            # First split by paragraphs (blank-line separated)
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            small_chunks: List[str] = []
            for para in paragraphs:
                if len(para) <= max_chars:
                    small_chunks.append(para)
                    continue
                # Fall back to sentence-level splitting when paragraph is too long
                # Simple sentence splitter based on punctuation.
                sentences = [s.strip() for s in __import__('re').split(r'(?<=[\.\!?])\s+', para) if s.strip()]
                # Group sentences into chunk-sized blocks
                cur = []
                cur_len = 0
                for s in sentences:
                    if cur_len + len(s) + 1 <= max_chars:
                        cur.append(s)
                        cur_len += len(s) + 1
                        continue
                    if cur:
                        small_chunks.append(" ".join(cur))
                    # If single sentence exceeds max_chars, hard-split it
                    if len(s) > max_chars:
                        for i in range(0, len(s), max_chars):
                            small_chunks.append(s[i : i + max_chars])
                        cur = []
                        cur_len = 0
                    else:
                        cur = [s]
                        cur_len = len(s) + 1
                if cur:
                    small_chunks.append(" ".join(cur))
            return small_chunks

        global_index = 0
        for i, node in enumerate(nodes):
            node_text = (getattr(node, "text", None) or "").strip()
            if not node_text:
                continue

            node_meta = getattr(node, "metadata", {}) or {}

            # If node is small enough, keep as-is; otherwise split
            parts = [node_text] if len(node_text) <= MAX_CHUNK_CHARS else split_text_to_chunks(node_text, MAX_CHUNK_CHARS)

            for part_idx, part in enumerate(parts):
                chunk = self._build_chunk(
                    document=document,
                    text_content=part,
                    chunk_index=global_index,
                    extra_metadata={
                        **node_meta,
                        "chunk_size": len(part),
                        "split_from_node": i,
                        "split_index": part_idx,
                    },
                )
                chunks.append(chunk)
                global_index += 1

        # Post-process: merge very small chunks using look-ahead strategy
        MIN_MERGE_CHARS = int(self.MIN_MERGE_CHARS)
        merged: List[DocumentChunk] = []
        i = 0
        while i < len(chunks):
            cur = chunks[i]
            cur_size = cur.metadata.get("chunk_size", len(cur.text_content))
            if cur_size >= MIN_MERGE_CHARS:
                merged.append(cur)
                i += 1
                continue

            # Try to merge with next chunk when possible
            if i + 1 < len(chunks):
                nxt = chunks[i + 1]
                # Merge current into next (prefer look-ahead)
                new_text = (cur.text_content + " " + nxt.text_content).strip()
                new_meta = {**(nxt.metadata or {}), **(cur.metadata or {})}
                new_meta["chunk_size"] = len(new_text)
                # Create new merged chunk; keep next's chunk_index spot
                merged_chunk = DocumentChunk(
                    chunk_id="",
                    parent_doc_id=cur.parent_doc_id,
                    chunk_index=nxt.chunk_index,
                    text_content=new_text,
                    content_hash="",
                    metadata=new_meta,
                )
                # Replace next with merged_chunk and skip current
                chunks[i + 1] = merged_chunk
                i += 1
                continue

            # No next: merge into previous if exists
            if merged:
                prev = merged.pop()
                new_text = (prev.text_content + " " + cur.text_content).strip()
                new_meta = {**(prev.metadata or {}), **(cur.metadata or {})}
                new_meta["chunk_size"] = len(new_text)
                merged_chunk = DocumentChunk(
                    chunk_id="",
                    parent_doc_id=cur.parent_doc_id,
                    chunk_index=prev.chunk_index,
                    text_content=new_text,
                    content_hash="",
                    metadata=new_meta,
                )
                merged.append(merged_chunk)
                i += 1
                continue

            # Single small chunk with no neighbors -> drop it
            i += 1

        # Recompute ids, indices and content_hashes deterministically
        final: List[DocumentChunk] = []
        for idx, ch in enumerate(merged):
            text = ch.text_content or ""
            final_chunk = self._build_chunk(
                document=document,
                text_content=text,
                chunk_index=idx,
                extra_metadata={**(ch.metadata or {}), "chunk_size": len(text)},
            )
            final.append(final_chunk)

        return final
        
