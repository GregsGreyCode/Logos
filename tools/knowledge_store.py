"""
KnowledgeStore — per-agent persistent knowledge base with semantic search.

Each agent instance gets its own knowledge directory under $HERMES_HOME/knowledge/.
Documents are chunked, embedded via an OpenAI-compatible endpoint (Ollama by
default), and stored as JSONL.  Search uses in-process numpy cosine similarity
— no external vector DB required.

Storage layout::

    $HERMES_HOME/knowledge/
        index.json          # source metadata
        chunks.jsonl        # {id, source_id, text, embedding, chunk_idx}

Design constraints:
  - Self-contained: no Pinecone, ChromaDB, or other services
  - Local-first: default embedding via Ollama (nomic-embed-text)
  - Fast: numpy cosine similarity, <10ms for 10k chunks on CPU
  - File-backed: easy to backup, migrate, or fork between agents
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_KNOWLEDGE_DIR = _HERMES_HOME / "knowledge"


# ── Chunking ────────────────────────────────────────────────────────────────

def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> List[str]:
    """Split *text* into overlapping chunks by character count.

    Returns a list of non-empty strings.  Empty input yields an empty list.
    """
    if not text or not text.strip():
        return []
    # Normalise whitespace runs but preserve paragraph breaks
    chunks: List[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start += chunk_size - chunk_overlap
    return chunks


# ── Embedding ───────────────────────────────────────────────────────────────

def _get_embedding_client(
    endpoint: str | None = None,
    api_key: str | None = None,
):
    """Return an OpenAI client configured for the embedding endpoint."""
    from openai import OpenAI

    base_url = endpoint or os.getenv(
        "KNOWLEDGE_EMBEDDING_ENDPOINT",
        "http://localhost:11434/v1",
    )
    key = api_key or os.getenv("KNOWLEDGE_EMBEDDING_API_KEY", "not-needed")
    return OpenAI(base_url=base_url, api_key=key)


def embed_texts(
    texts: List[str],
    model: str = "nomic-embed-text",
    endpoint: str | None = None,
    api_key: str | None = None,
) -> List[List[float]]:
    """Return embedding vectors for each text via an OpenAI-compatible endpoint.

    Batches texts in a single request.  Raises on failure so callers can
    surface the error to the user.
    """
    if not texts:
        return []
    client = _get_embedding_client(endpoint, api_key)
    response = client.embeddings.create(input=texts, model=model)
    # Sort by index to guarantee order matches input
    sorted_data = sorted(response.data, key=lambda d: d.index)
    return [d.embedding for d in sorted_data]


# ── Vector search ───────────────────────────────────────────────────────────

def cosine_similarity_search(
    query_embedding: List[float],
    chunk_embeddings: List[List[float]],
    top_k: int = 5,
    threshold: float = 0.0,
) -> List[tuple[int, float]]:
    """Return the top-k (index, score) pairs by cosine similarity.

    Uses numpy for efficient batch computation.  Falls back to pure Python
    if numpy is unavailable (slower but functional).
    """
    if not chunk_embeddings:
        return []
    try:
        import numpy as np
        q = np.array(query_embedding, dtype=np.float32)
        M = np.array(chunk_embeddings, dtype=np.float32)
        # Normalise
        q_norm = q / (np.linalg.norm(q) + 1e-10)
        norms = np.linalg.norm(M, axis=1, keepdims=True) + 1e-10
        M_norm = M / norms
        scores = M_norm @ q_norm
        # Top-k
        if top_k >= len(scores):
            indices = np.argsort(-scores)
        else:
            indices = np.argpartition(-scores, top_k)[:top_k]
            indices = indices[np.argsort(-scores[indices])]
        results = [(int(i), float(scores[i])) for i in indices if scores[i] >= threshold]
        return results[:top_k]
    except ImportError:
        logger.warning("numpy not installed — falling back to pure-Python cosine similarity")
        return _cosine_similarity_pure(query_embedding, chunk_embeddings, top_k, threshold)


def _cosine_similarity_pure(
    query: List[float],
    embeddings: List[List[float]],
    top_k: int,
    threshold: float,
) -> List[tuple[int, float]]:
    """Pure-Python fallback for cosine similarity."""
    import math
    q_norm = math.sqrt(sum(x * x for x in query)) or 1e-10
    results = []
    for idx, emb in enumerate(embeddings):
        dot = sum(a * b for a, b in zip(query, emb))
        e_norm = math.sqrt(sum(x * x for x in emb)) or 1e-10
        score = dot / (q_norm * e_norm)
        if score >= threshold:
            results.append((idx, score))
    results.sort(key=lambda x: -x[1])
    return results[:top_k]


# ── KnowledgeStore ──────────────────────────────────────────────────────────

class KnowledgeStore:
    """Per-agent knowledge base with semantic search.

    All state is file-backed under ``knowledge_dir``.  Thread-safe for
    single-process use (agents are single-threaded).
    """

    def __init__(
        self,
        knowledge_dir: Path | str | None = None,
        embedding_model: str = "nomic-embed-text",
        embedding_endpoint: str | None = None,
        embedding_api_key: str | None = None,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        max_chunks: int = 10_000,
    ):
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self.embedding_model = embedding_model
        self.embedding_endpoint = embedding_endpoint
        self.embedding_api_key = embedding_api_key
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chunks = max_chunks

        self._index_path = self.knowledge_dir / "index.json"
        self._chunks_path = self.knowledge_dir / "chunks.jsonl"

        # In-memory cache (loaded lazily)
        self._index: List[Dict[str, Any]] | None = None
        self._chunks: List[Dict[str, Any]] | None = None
        self._embeddings_matrix: List[List[float]] | None = None

    # ── Persistence ─────────────────────────────────────────────────────

    def _ensure_dir(self) -> None:
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def _load_index(self) -> List[Dict[str, Any]]:
        if self._index is not None:
            return self._index
        try:
            if self._index_path.exists():
                self._index = json.loads(self._index_path.read_text(encoding="utf-8"))
            else:
                self._index = []
        except Exception:
            self._index = []
        return self._index

    def _save_index(self) -> None:
        self._ensure_dir()
        self._index_path.write_text(
            json.dumps(self._index or [], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_chunks(self) -> List[Dict[str, Any]]:
        if self._chunks is not None:
            return self._chunks
        self._chunks = []
        self._embeddings_matrix = []
        try:
            if self._chunks_path.exists():
                for line in self._chunks_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        record = json.loads(line)
                        self._chunks.append(record)
                        self._embeddings_matrix.append(record["embedding"])
        except Exception as exc:
            logger.warning("Failed to load chunks: %s", exc)
            self._chunks = []
            self._embeddings_matrix = []
        return self._chunks

    def _save_chunks(self) -> None:
        self._ensure_dir()
        lines = []
        for chunk in (self._chunks or []):
            lines.append(json.dumps(chunk, ensure_ascii=False))
        self._chunks_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

    def _invalidate_cache(self) -> None:
        """Force reload from disk on next access."""
        self._index = None
        self._chunks = None
        self._embeddings_matrix = None

    # ── Public API ──────────────────────────────────────────────────────

    def ingest(
        self,
        text: str,
        source_name: str,
        source_type: str = "text",
    ) -> Dict[str, Any]:
        """Chunk and embed *text*, storing results in the knowledge base.

        Returns a summary dict with source_id, chunk_count, and status.
        """
        if not text or not text.strip():
            return {"success": False, "error": "No text content to ingest."}

        index = self._load_index()
        chunks = self._load_chunks()

        # Check for duplicate source
        for src in index:
            if src["source_name"] == source_name:
                return {
                    "success": False,
                    "error": f"Source '{source_name}' already exists. Remove it first to re-ingest.",
                }

        # Chunk
        text_chunks = chunk_text(text, self.chunk_size, self.chunk_overlap)
        if not text_chunks:
            return {"success": False, "error": "Text produced no chunks after processing."}

        # Check capacity
        current_count = len(chunks)
        if current_count + len(text_chunks) > self.max_chunks:
            return {
                "success": False,
                "error": (
                    f"Ingesting {len(text_chunks)} chunks would exceed the "
                    f"{self.max_chunks:,} chunk limit (current: {current_count:,}). "
                    f"Remove some sources first."
                ),
            }

        # Embed
        try:
            embeddings = embed_texts(
                text_chunks,
                model=self.embedding_model,
                endpoint=self.embedding_endpoint,
                api_key=self.embedding_api_key,
            )
        except Exception as exc:
            return {
                "success": False,
                "error": f"Embedding failed: {exc}. Is the embedding model '{self.embedding_model}' available?",
            }

        # Generate source ID
        source_id = hashlib.sha256(
            f"{source_name}:{time.time()}".encode()
        ).hexdigest()[:12]

        # Store source metadata
        source_entry = {
            "source_id": source_id,
            "source_name": source_name,
            "source_type": source_type,
            "ingested_at": time.time(),
            "chunk_count": len(text_chunks),
            "char_count": len(text),
        }
        index.append(source_entry)

        # Store chunks
        for i, (chunk_text_str, embedding) in enumerate(zip(text_chunks, embeddings)):
            chunks.append({
                "id": f"{source_id}:{i}",
                "source_id": source_id,
                "source_name": source_name,
                "chunk_idx": i,
                "text": chunk_text_str,
                "embedding": embedding,
            })
            self._embeddings_matrix.append(embedding)

        # Persist
        self._save_index()
        self._save_chunks()

        return {
            "success": True,
            "source_id": source_id,
            "source_name": source_name,
            "chunk_count": len(text_chunks),
            "total_chunks": len(chunks),
        }

    def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.3,
    ) -> Dict[str, Any]:
        """Semantic search over the knowledge base.

        Returns top-k matching chunks with source attribution and scores.
        """
        chunks = self._load_chunks()
        if not chunks:
            return {
                "success": True,
                "results": [],
                "message": "Knowledge base is empty. Ingest documents first.",
            }

        # Embed the query
        try:
            query_emb = embed_texts(
                [query],
                model=self.embedding_model,
                endpoint=self.embedding_endpoint,
                api_key=self.embedding_api_key,
            )[0]
        except Exception as exc:
            return {"success": False, "error": f"Failed to embed query: {exc}"}

        # Search
        matches = cosine_similarity_search(
            query_emb,
            self._embeddings_matrix,
            top_k=top_k,
            threshold=threshold,
        )

        results = []
        for idx, score in matches:
            chunk = chunks[idx]
            results.append({
                "text": chunk["text"],
                "source": chunk["source_name"],
                "chunk_idx": chunk["chunk_idx"],
                "score": round(score, 4),
            })

        return {"success": True, "results": results, "query": query}

    def list_sources(self) -> Dict[str, Any]:
        """List all ingested sources with metadata."""
        index = self._load_index()
        return {
            "success": True,
            "sources": [
                {
                    "source_id": s["source_id"],
                    "source_name": s["source_name"],
                    "source_type": s["source_type"],
                    "chunk_count": s["chunk_count"],
                    "char_count": s["char_count"],
                    "ingested_at": s["ingested_at"],
                }
                for s in index
            ],
            "total_sources": len(index),
            "total_chunks": sum(s["chunk_count"] for s in index),
        }

    def remove_source(self, source_name: str) -> Dict[str, Any]:
        """Remove a source and all its chunks from the knowledge base."""
        index = self._load_index()
        chunks = self._load_chunks()

        # Find the source
        source_ids = {s["source_id"] for s in index if s["source_name"] == source_name}
        if not source_ids:
            return {"success": False, "error": f"Source '{source_name}' not found."}

        removed_chunks = sum(s["chunk_count"] for s in index if s["source_id"] in source_ids)

        # Remove from index
        self._index = [s for s in index if s["source_id"] not in source_ids]

        # Remove chunks and rebuild embeddings matrix
        self._chunks = [c for c in chunks if c["source_id"] not in source_ids]
        self._embeddings_matrix = [c["embedding"] for c in self._chunks]

        # Persist
        self._save_index()
        self._save_chunks()

        return {
            "success": True,
            "removed_source": source_name,
            "removed_chunks": removed_chunks,
            "remaining_sources": len(self._index),
            "remaining_chunks": len(self._chunks),
        }

    def stats(self) -> Dict[str, Any]:
        """Return knowledge base statistics."""
        index = self._load_index()
        chunks = self._load_chunks()
        total_chars = sum(len(c.get("text", "")) for c in chunks)
        return {
            "success": True,
            "total_sources": len(index),
            "total_chunks": len(chunks),
            "max_chunks": self.max_chunks,
            "usage_percent": round(len(chunks) / self.max_chunks * 100, 1) if self.max_chunks else 0,
            "total_chars": total_chars,
            "embedding_model": self.embedding_model,
        }
